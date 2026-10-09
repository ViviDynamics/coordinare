"""OpenClawBackend — drives OpenClaw as a one-shot embedded agent.

OpenClaw (https://openclaw.ai) is a self-hosted assistant gateway from the
same "lobster" family as Hermes/Pi. This adapter mirrors the
``hermes``/``pi`` one-shot pattern: a single ``openclaw agent --local`` turn
per invocation (the ``--local`` flag runs the embedded agent directly,
skipping the Gateway), no persistent session. Relay feedback is queued and
replayed into the next invocation's prompt (FR-002 / R-004).

Model routing (077 US6, OpenAI-compatible via LiteLLM):
- When ``OPENCLAW_PROVIDER_BASE_URL`` is set, ``start()`` writes
  ``~/.openclaw/openclaw.json`` declaring a custom ``openai-completions``
  provider pointed at the proxy (``OPENCLAW_PROVIDER_NAME``, default
  ``litellm``) and allowlists ``<name>/<model>`` under
  ``agents.defaults.models`` (OpenClaw rejects non-allowlisted models). The
  API key is interpolated from ``OPENCLAW_PROVIDER_ENV_KEY`` via OpenClaw's
  ``${ENV}`` syntax — the secret never lands on disk. ``mode: "merge"`` keeps
  the bundled provider defaults intact.
- OpenClaw auto-forces ``compat.supportsDeveloperRole: false`` for non-native
  ``openai-completions`` endpoints, so (unlike Pi) no manual compat block is
  needed for qwen.

Terminal contract: ``openclaw agent --json`` emits a single JSON object
``{payloads: [{text}], meta: {finalAssistantVisibleText, stopReason,
executionTrace: {winnerProvider, winnerModel}, aborted, ...}}``. We treat
``meta.stopReason == "stop"`` (rc 0) as done and surface
``payloads[0].text`` / ``meta.finalAssistantVisibleText`` as the output.
Verified end-to-end against the LiteLLM proxy + local/qwen3.6:35b (R-07).

Timeout: bounded by ``settings.AGENT_TIMEOUT`` via performer.main's watchdog,
which calls ``stop()`` on expiry (FR-010a); no OpenClaw-specific timer.
"""
from __future__ import annotations

from performer.backends._clarifications import clarification_comment_lines

import asyncio
import json
import os
import shutil
import signal
import uuid
from collections import deque
from pathlib import Path

import psutil
import structlog

from performer.backends._env_policy import build_subprocess_env
from performer.backends.base import BackendStatus
from performer.backends.opencode import _build_task_prompt
from performer.models import BackendEvent, BackendEventType, Score, Stand

log = structlog.get_logger(__name__)

_MAX_TEXT = 200


def _env_int(name: str, default: int) -> int:
    """Read a positive int from the environment, falling back on absent/invalid.

    A blank, non-numeric, or non-positive value yields ``default`` (and logs a
    warning) so a typo in the performer env never declares a 0/negative budget
    that would itself trip OpenClaw's context-overflow guard.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        log.warning("openclaw.invalid_env_int", name=name, value=raw, fallback=default)
        return default
    if value <= 0:
        log.warning("openclaw.invalid_env_int", name=name, value=raw, fallback=default)
        return default
    return value


def _extract_final_text(parsed: dict) -> str:
    """Pull the assistant's final reply from an ``openclaw agent --json`` object.

    Prefers the delivered ``payloads[0].text`` (what a channel would receive),
    falling back to ``meta.finalAssistantVisibleText`` / ``finalAssistantRawText``.
    Returns ``""`` when nothing usable is present.
    """
    if not isinstance(parsed, dict):
        return ""
    payloads = parsed.get("payloads")
    if isinstance(payloads, list):
        for item in payloads:
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str) and text.strip():
                    return text
    meta = parsed.get("meta")
    if isinstance(meta, dict):
        for key in ("finalAssistantVisibleText", "finalAssistantRawText"):
            text = meta.get(key)
            if isinstance(text, str) and text.strip():
                return text
    return ""


class OpenClawBackend:
    """One-shot embedded-agent adapter for OpenClaw."""

    def __init__(self) -> None:
        self._executable = os.environ.get("OPENCLAW_EXECUTABLE", "openclaw")
        self._proc: asyncio.subprocess.Process | None = None
        self._status: BackendStatus = BackendStatus(state="working")
        self._reader_task: asyncio.Task[None] | None = None
        self._event_buffer: deque[BackendEvent] = deque(maxlen=200)
        self._stand: Stand | None = None
        self._score: Score | None = None
        self._git_env: dict[str, str] = {}
        self._cache_env: dict[str, str] = {}
        self._tool_env: dict[str, str] = {}
        self._model: str | None = None
        self._feedback_queue: list[str] = []
        self._stop_requested: bool = False
        self._terminal: bool = False

    # ------------------------------------------------------------------
    # BackendAdapter protocol
    # ------------------------------------------------------------------

    async def start(
        self,
        stand: Stand,
        score: Score,
        *,
        model: str | None = None,
        effort: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> None:
        self._stand = stand
        self._score = score
        self._git_env = stand.git_env
        self._cache_env = stand.cache_env
        self._tool_env = score.tool_env
        self._model = (model or os.environ.get("OPENCLAW_MODEL", "")).strip() or None

        # 088 B1 shared env policy: the openclaw CLI is a Node app requiring
        # Node >=22.19 — it must launch under the IMAGE's node, never the
        # env-cache's project-pinned node (e.g. 18.12.1 → exits 1 at startup).
        # Image PATH first, cache toolchain dirs appended (reachable, never
        # shadowing); every other cache var flows through.
        env = build_subprocess_env(
            cache_env=self._cache_env,
            git_env=self._git_env,
            tool_env=self._tool_env,
        )

        # 077 US6: opt-in custom OpenAI-compatible provider → LiteLLM. When
        # OPENCLAW_PROVIDER_BASE_URL is set, write ~/.openclaw/openclaw.json and
        # route the model as <provider>/<model>. Unset → rely on a
        # mounted/bundled config (vendor models), leaving the embedded path
        # otherwise untouched.
        effective_model = self._model
        provider_base_url = env.get("OPENCLAW_PROVIDER_BASE_URL", "").strip()
        if provider_base_url:
            provider_name = env.get("OPENCLAW_PROVIDER_NAME", "litellm").strip() or "litellm"
            provider_env_key = (
                env.get("OPENCLAW_PROVIDER_ENV_KEY", "OPENAI_API_KEY").strip()
                or "OPENAI_API_KEY"
            )
            home = Path(env.get("HOME", "/root"))
            self._write_provider_config(
                home, provider_name, provider_base_url, provider_env_key, self._model,
            )
            if self._model and not self._model.startswith(f"{provider_name}/"):
                effective_model = f"{provider_name}/{self._model}"

        # CRITICAL: openclaw's `--local` embedded agent operates in its managed
        # workspace (``$HOME/.openclaw/workspace``), NOT the process cwd — so
        # setting cwd alone leaves the agent looking at an empty default
        # workspace (verified: the reviewer reported "workspace essentially
        # empty" and could not see the PR diff). Point that workspace at the
        # job's checkout via a symlink so the agent sees the real repo + diff.
        self._point_workspace_at(Path(env.get("HOME", "/root")), Path(stand.path))

        if self._feedback_queue:
            queued = [{"body": fb} for fb in self._feedback_queue if fb]
            score.relay_feedback = list(score.relay_feedback or []) + queued
            self._feedback_queue.clear()

        # Route persona to openclaw's native identity slot (SOUL.md), mirroring
        # hermes (068 FR-015). openclaw is from the same family and reads its
        # agent identity from <workspace>/SOUL.md; delivering the role contract
        # as identity (rather than a one-off prompt-body instruction it
        # under-weights) makes it actually adopt the role — e.g. the reviewer's
        # binary "approve when genuinely ready" contract instead of defaulting
        # to generic-assistant nitpicking. We write SOUL.md and drop persona
        # from the prompt body to avoid delivering it twice.
        persona = score.persona_instructions
        if persona:
            try:
                (Path(stand.path) / "SOUL.md").write_text(persona)
                log.info("openclaw.persona_to_soul", chars=len(persona))
            except OSError as exc:
                log.warning("openclaw.soul_write_failed", error=str(exc))
            score.persona_instructions = ""  # avoid duplicate persona in the body
        # 077: materialise the card context as CARD.md in the workspace so the
        # reviewer persona (which looks for "card documentation files" rather
        # than trusting the inline prompt body) can actually read it — otherwise
        # openclaw posts CHANGES REQUESTED ("card documentation files are not
        # available to read") every round and the review loop never converges.
        card_md = self._write_card_docs(Path(stand.path), score)
        try:
            prompt = _build_task_prompt(score, stand_path=Path(stand.path))
            if card_md is not None:
                prompt += (
                    "\n\n## Card Documentation\n\n"
                    "The card title, description, acceptance criteria, and any "
                    "clarifications are in `CARD.md` at the root of your workspace. "
                    "Read it before acting — it IS the card documentation."
                )
        finally:
            if persona:
                score.persona_instructions = persona  # restore for callers/relay

        # If the prompt exceeds the Linux per-argument limit (MAX_ARG_STRLEN ≈
        # 128 KB), create_subprocess_exec raises OSError E2BIG.  Offload to
        # TASK.md in the workspace (same pattern as SOUL.md / CARD.md) and
        # pass a short --message reference instead.
        _MAX_INLINE_BYTES = 100_000  # 100 KB — safe below 131 072 B limit
        prompt_bytes = len(prompt.encode("utf-8"))
        if prompt_bytes > _MAX_INLINE_BYTES:
            task_md = stand.path / "TASK.md"
            try:
                task_md.write_text(prompt, encoding="utf-8")
                prompt = (
                    "Your complete task briefing is in `TASK.md` at the root of "
                    "your workspace.  Read `TASK.md` carefully and follow ALL "
                    "instructions in it exactly before taking any action."
                )
                log.info("openclaw.prompt_offloaded_to_task_md", original_bytes=prompt_bytes)
            except OSError as exc:
                log.warning("openclaw.task_md_write_failed", error=str(exc))
                # proceed with the original oversized prompt — subprocess will
                # raise E2BIG just as before, surfacing a clear error rather
                # than silently losing the task description

        role = (score.role or "job").lower()
        session_key = f"coordinare-{role}-{uuid.uuid4().hex[:8]}"

        args: list[str] = [
            self._executable, "agent",
            "--local",
            "--json",
            "--session-key", session_key,
            "--message", prompt,
            "--thinking", "off",
        ]
        if effective_model:
            args += ["--model", effective_model]

        log.info(
            "openclaw starting",
            model=effective_model,
            provider_routed=bool(provider_base_url),
            session_key=session_key,
        )

        try:
            self._proc = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(stand.path),
                start_new_session=True,
                env=env,
            )
        except Exception:
            log.exception("openclaw subprocess spawn failed", executable=self._executable)
            self._terminal = True
            raise

        self._status = BackendStatus(state="working")
        self._reader_task = asyncio.create_task(
            self._wait_and_parse(), name="openclaw-reader",
        )
        log.info("openclaw started", pid=self._proc.pid, model=effective_model)

    def get_status(self) -> BackendStatus:
        return self._status

    def drain_events(self) -> list[BackendEvent]:
        events = list(self._event_buffer)
        self._event_buffer.clear()
        return events

    async def relay_feedback(self, feedback: str) -> None:
        """Queue feedback; replayed on the next ``start()`` (one-shot, R-004)."""
        self._feedback_queue.append(feedback)

    async def stop(self) -> None:
        self._stop_requested = True
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass

        if self._proc is not None and self._proc.returncode is None:
            try:
                pgid = os.getpgid(self._proc.pid)
                os.killpg(pgid, signal.SIGTERM)
            except (ProcessLookupError, OSError):
                pass
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                try:
                    pgid = os.getpgid(self._proc.pid)
                    os.killpg(pgid, signal.SIGKILL)
                except (ProcessLookupError, OSError):
                    try:
                        parent = psutil.Process(self._proc.pid)
                        for child in parent.children(recursive=True):
                            try:
                                child.kill()
                            except psutil.NoSuchProcess:
                                pass
                        parent.kill()
                    except psutil.NoSuchProcess:
                        pass
                try:
                    await asyncio.wait_for(self._proc.wait(), timeout=2.0)
                except asyncio.TimeoutError:
                    pass

        if not self._terminal:
            self._status = BackendStatus(state="error", error_reason="stopped")
        self._terminal = True
        self._proc = None
        log.info("openclaw stopped")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _write_card_docs(self, workspace: Path, score: Score) -> Path | None:
        """Materialise the card context as a file openclaw's agent can read.

        077: openclaw's reviewer persona is told to "read the card documentation
        files"; unlike the other backends it does not treat the inline prompt
        body as sufficient and reports "Cannot perform review as the card
        documentation files are not available to read" — defaulting to CHANGES
        REQUESTED every round and never converging. Mirror the SOUL.md approach:
        write the title / description / acceptance criteria / clarifications to
        ``CARD.md`` at the workspace root so the agent can actually read them.
        Best-effort: a write failure is logged, not fatal. The per-job ephemeral
        workspace is discarded after the turn and reviewers do not push, so this
        file never reaches the PR.
        """
        try:
            parts: list[str] = [f"# Card: {score.title}".rstrip(), ""]
            if score.description:
                parts += ["## Description", "", score.description, ""]
            if score.acceptance_criteria:
                parts += ["## Acceptance Criteria", ""]
                parts += [f"- {c}" for c in score.acceptance_criteria]
                parts += [""]
            if score.clarifications:
                parts += ["## Clarification Q&A", ""]
                for entry in score.clarifications:
                    parts += clarification_comment_lines(entry)
                    for q in entry.get("questions") or []:
                        parts.append(f"- Q: {q}")
                    answer = str(entry.get("answer", "")).strip()
                    if answer:
                        parts += [f"  A: {answer}", ""]
            card_md = workspace / "CARD.md"
            card_md.write_text("\n".join(parts) + "\n")
            log.info("openclaw.card_docs_written", path=str(card_md), chars=len("\n".join(parts)))
            return card_md
        except OSError as exc:
            log.warning("openclaw.card_docs_write_failed", error=str(exc))
            return None

    def _point_workspace_at(self, home: Path, workspace: Path) -> None:
        """Symlink openclaw's managed agent workspace at the job checkout.

        openclaw's embedded agent reads/writes only within
        ``$HOME/.openclaw/workspace`` and ignores the process cwd, so without
        this the agent never sees the repo (the reviewer reported an "empty
        workspace"). Replacing that path with a symlink to ``stand.path`` makes
        the agent operate on the real checkout (and lets the reviewer read the
        PR diff). Best-effort: failures are logged, not fatal.
        """
        try:
            oc_ws = home / ".openclaw" / "workspace"
            oc_ws.parent.mkdir(parents=True, exist_ok=True)
            if oc_ws.is_symlink() or oc_ws.is_file():
                oc_ws.unlink()
            elif oc_ws.is_dir():
                shutil.rmtree(oc_ws, ignore_errors=True)
            oc_ws.symlink_to(workspace, target_is_directory=True)
            log.info("openclaw.workspace_linked", workspace=str(workspace))
        except OSError as exc:
            log.warning("openclaw.workspace_link_failed", error=str(exc))

    def _write_provider_config(
        self,
        home: Path,
        name: str,
        base_url: str,
        env_key: str,
        model: str | None,
    ) -> None:
        """Write ``~/.openclaw/openclaw.json`` with a custom OpenAI-compatible provider.

        Declares ``models.providers.<name>`` (``api: openai-completions``,
        ``apiKey: ${env_key}``) and allowlists ``<name>/<model>`` under
        ``agents.defaults.models`` (OpenClaw rejects non-allowlisted models).
        ``mode: "merge"`` preserves bundled provider defaults.
        """
        provider: dict = {
            "baseUrl": base_url,
            "apiKey": f"${{{env_key}}}",
            "api": "openai-completions",
            "timeoutSeconds": 300,
            "models": [],
        }
        allowlist: dict = {}
        if model:
            # OpenClaw enforces ``contextWindow`` client-side: when the prompt
            # (diff + accumulated review feedback) exceeds it, the embedded agent
            # refuses with "Context overflow: prompt too large for the model … use
            # a larger-context model" instead of returning a verdict — which then
            # records as CHANGES REQUESTED and exhausts the reviewer feedback-cycle
            # budget. The 32768/8192 defaults suit qwen on a self-hosted host, but a large model
            # (e.g. gpt-oss:120b served at 131072) must declare its true window or
            # it gets gated below its real capacity. Per-performer env overrides
            # keep the budget matched to whatever model the container routes.
            context_window = _env_int("OPENCLAW_CONTEXT_WINDOW", 32768)
            max_tokens = _env_int("OPENCLAW_MAX_TOKENS", 8192)
            provider["models"] = [
                {
                    "id": model,
                    "name": model,
                    "reasoning": False,
                    "input": ["text"],
                    "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
                    "contextWindow": context_window,
                    "maxTokens": max_tokens,
                },
            ]
            allowlist[f"{name}/{model}"] = {"alias": model}
        config = {
            "models": {"mode": "merge", "providers": {name: provider}},
            "agents": {"defaults": {"models": allowlist}},
        }
        config_dir = home / ".openclaw"
        config_dir.mkdir(parents=True, exist_ok=True)
        (config_dir / "openclaw.json").write_text(json.dumps(config, indent=2))
        log.info(
            "openclaw.provider_config_written",
            provider=name,
            base_url=base_url,
            path=str(config_dir / "openclaw.json"),
        )

    def _emit(self, type: BackendEventType, text: str, detail: str = "") -> None:
        self._event_buffer.append(
            BackendEvent(type=type, text=text[:_MAX_TEXT], detail=detail),
        )

    async def _wait_and_parse(self) -> None:
        if self._proc is None:
            return
        try:
            stdout_b, stderr_b = await self._proc.communicate()
            rc = self._proc.returncode
            stdout_text = stdout_b.decode(errors="replace") if stdout_b else ""
            stderr_text = stderr_b.decode(errors="replace") if stderr_b else ""

            if self._stop_requested:
                return  # stop() owns the terminal transition

            if rc != 0:
                self._status = BackendStatus(
                    state="error", error_reason=f"subprocess_exit:{rc}",
                )
                snippet = (stderr_text.strip() or stdout_text.strip())[-_MAX_TEXT:]
                log.warning("openclaw failed", rc=rc, stderr=stderr_text[-1000:])
                if snippet:
                    self._emit(BackendEventType.error, snippet)
                self._terminal = True
                return

            try:
                parsed = json.loads(stdout_text.strip())
            except json.JSONDecodeError:
                parsed = None

            if not isinstance(parsed, dict):
                self._status = BackendStatus(
                    state="error", error_reason="malformed_output",
                )
                self._emit(BackendEventType.error, "malformed_output")
                self._terminal = True
                return

            meta = parsed.get("meta") if isinstance(parsed.get("meta"), dict) else {}
            stop_reason = meta.get("stopReason")
            aborted = bool(meta.get("aborted"))
            output_text = _extract_final_text(parsed)

            if aborted or (stop_reason and stop_reason != "stop"):
                self._status = BackendStatus(
                    state="error",
                    error_reason=f"stop_reason:{stop_reason or 'aborted'}",
                    output=output_text or None,
                )
                self._emit(BackendEventType.error, str(stop_reason or "aborted"))
                self._terminal = True
                return

            self._status = BackendStatus(
                state="done",
                stop_reason=stop_reason,
                output=output_text or None,
            )
            if output_text:
                self._emit(BackendEventType.progress, output_text[:_MAX_TEXT])
            self._terminal = True
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("openclaw reader error", error=str(exc))
            self._status = BackendStatus(state="error", error_reason=str(exc))
            self._terminal = True
