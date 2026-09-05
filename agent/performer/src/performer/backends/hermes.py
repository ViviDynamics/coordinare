"""HermesBackend — drives Hermes as a one-shot CLI agent.

Mirrors the ``junie`` / ``claude_code`` one-shot pattern: a single
``hermes chat`` subprocess per invocation, no persistent chat session.
Relay feedback is queued and replayed into the next invocation's prompt
(FR-002 / R-004).

Isolation:
- ``HERMES_HOME`` is forced to a job-scoped ``tempfile.mkdtemp`` and
  removed on every terminal outcome (FR-005, FR-011).
- The operator's ``~/.hermes`` is never read or written.
- Forbidden env vars (gateway/messaging/cron/user-memory) are stripped
  from the subprocess env before launch (FR-006).
- The CLI is invoked with a strict ``--toolsets`` allow-list and the
  written profile config additionally carries ``disabled_toolsets`` as
  belt-and-suspenders.

Timeout:
- Bounded by ``settings.AGENT_TIMEOUT`` from ``performer.main``; no
  Hermes-specific timer (FR-010a). The shared watchdog calls
  ``stop()`` on expiry.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import tempfile
import time
import uuid
from collections import deque
from pathlib import Path

import psutil
import structlog

from performer.backends._card_docs import card_doc_folder, card_docs_prompt_section, qa_findings_prompt_section
from performer.backends._env_policy import build_subprocess_env
from performer.backends.base import BackendStatus
from performer.models import DIAGNOSTIC_ROLE, BackendEvent, BackendEventType, Score, Stand

log = structlog.get_logger(__name__)

_MAX_TEXT = 200
# Cap for inlined architecture-plan content (chars). Plans larger than this
# are truncated with a marker so the prompt stays inside model context.
_MAX_INLINED_PLAN_CHARS = 20_000

ALLOWED_TOOLSETS: tuple[str, ...] = (
    "terminal",
    "file",
    "search",
    "browser",
    "todo",
)
DISABLED_TOOLSETS: tuple[str, ...] = (
    "gateway",
    "messaging",
    "cron",
    "clarify",
    "user_memory",
)
# Env var prefixes that route Hermes to forbidden capability surfaces.
# Any var starting with one of these is stripped from the subprocess env.
_FORBIDDEN_ENV_PREFIXES: tuple[str, ...] = (
    "HERMES_GATEWAY",
    "HERMES_MESSAGING",
    "HERMES_CRON",
    "HERMES_USER_MEMORY",
)

_JSON_ONLY_ROLES = {
    "assessing", "assessor",
    "reviewing", "reviewer",
    "closing_review", "closer",
    "security",
    "qa",
    "documenting", "tech_writer",
}

# Roles that coordinare dispatches — used by the role-parity test (FR-001a).
# Listed here so the prompt builder emits a role-appropriate output block
# for each of them.
SUPPORTED_ROLES: tuple[str, ...] = (
    "assessor", "architect", "implementer",
    "reviewer", "qa", "security", "closer",
    "tech_writer",
)


class HermesBackend:
    """One-shot CLI adapter for Hermes."""

    def __init__(self) -> None:
        self._executable = os.environ.get("HERMES_EXECUTABLE", "hermes")
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
        self._max_tokens: int | None = None
        self._profile_dir: Path | None = None
        self._json_output_path: Path | None = None
        self._feedback_queue: list[str] = []
        self._stop_requested: bool = False
        self._terminal: bool = False
        self._api_key: str = ""
        self._job_log_id: str = ""
        self._last_prompt: str = ""

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
        self._model = model or os.environ.get("HERMES_MODEL", "") or None
        self._max_tokens = max_tokens

        provider = os.environ.get("HERMES_PROVIDER", "").strip()
        api_key = os.environ.get("HERMES_API_KEY", "").strip()
        base_url = os.environ.get("HERMES_BASE_URL", "").strip()
        resolved_model = (self._model or "").strip()

        for env_name, value in (
            ("HERMES_PROVIDER", provider),
            ("HERMES_API_KEY", api_key),
            ("HERMES_MODEL", resolved_model),
        ):
            if not value:
                self._status = BackendStatus(
                    state="error",
                    error_reason=f"missing_env:{env_name}",
                )
                self._finalize()
                return

        self._api_key = api_key
        self._profile_dir = Path(tempfile.mkdtemp(prefix="hermes-job-"))
        # 124: give the model an adequate output + input budget. max_tokens flows
        # from the dispatched Score (config performers.<role>.max_tokens); the
        # context window mirrors openclaw's OPENCLAW_CONTEXT_WINDOW via
        # HERMES_CONTEXT_WINDOW (unset → hermes-agent's default applies).
        self._write_profile_config(
            self._profile_dir, provider=provider, base_url=base_url,
            model=resolved_model, max_tokens=self._max_tokens,
            context_length=_env_int("HERMES_CONTEXT_WINDOW"),
        )
        # FR-015: route persona to hermes-agent's canonical identity slot.
        # When persona_instructions is empty we leave SOUL.md absent so
        # hermes-agent's own starter behaviour applies.
        if score.persona_instructions:
            (self._profile_dir / "SOUL.md").write_text(score.persona_instructions)

        # 077: materialise the card context as CARD.md in the checkout so a
        # persona that looks for "card documentation files" (same family as the
        # openclaw reviewer) can read it. Hermes already runs in cwd=stand.path
        # so no symlink is needed — but unlike openclaw it COMMITS, so the file
        # is git-excluded to keep it out of the PR.
        card_md = self._write_card_docs(Path(stand.path), score)

        # Build prompt; drain feedback queue into it.
        prompt = _build_task_prompt(
            score, list(self._feedback_queue), stand_path=stand.path,
        )
        if card_md is not None:
            prompt += (
                "\n\n## Card Documentation\n\n"
                "The card title, description, acceptance criteria, and any "
                "clarifications are in `CARD.md` at the root of the repo checkout. "
                "Read it before acting — it IS the card documentation."
            )
        self._feedback_queue.clear()
        self._last_prompt = prompt
        self._job_log_id = f"{time.strftime('%Y%m%dT%H%M%S')}_{(score.role or 'job')}_{uuid.uuid4().hex[:8]}"
        self._persist_job_artifact("prompt", prompt)

        # If the prompt exceeds the Linux per-argument limit (MAX_ARG_STRLEN ≈
        # 128 KB), create_subprocess_exec raises OSError E2BIG.  Offload to
        # TASK.md in the workspace and pass a short -q reference instead.
        _MAX_INLINE_BYTES = 100_000  # 100 KB — safe below 131 072 B limit
        prompt_bytes = len(prompt.encode("utf-8"))
        if prompt_bytes > _MAX_INLINE_BYTES:
            task_md = stand.path / "TASK.md"
            try:
                task_md.write_text(prompt, encoding="utf-8")
                prompt = (
                    "Your complete task briefing is in `TASK.md` at the root of "
                    "the repo checkout.  Read `TASK.md` carefully and follow ALL "
                    "instructions in it exactly before taking any action."
                )
                log.info("hermes.prompt_offloaded_to_task_md", original_bytes=prompt_bytes)
            except OSError as exc:
                log.warning("hermes.task_md_write_failed", error=str(exc))
                # proceed with the original oversized prompt — subprocess will
                # raise E2BIG just as before, surfacing a clear error rather
                # than silently losing the task description

        # The installed hermes-agent CLI exposes neither --output-file nor
        # --base-url on `chat`, and -z is a top-level shortcut rather than a
        # chat-subcommand flag. We capture the agent's final reply from stdout
        # (--quiet suppresses banner / spinner / tool previews so what remains
        # is the response plus minimal session-info chrome) and route the
        # endpoint through HERMES_BASE_URL in the subprocess env.
        # --yolo: bypass hermes-agent's dangerous-command approval gate.
        # In a non-interactive subprocess there is no TTY to prompt on, so
        # the default `approvals.mode: manual` fails closed with
        # "BLOCKED: User denied" and the agent loops without ever shelling
        # out. The container itself is the security boundary here, paired
        # with our --toolsets allow-list. `approvals.mode: off` is also
        # written into the per-job config below as belt-and-suspenders.
        # 077: when routed through an OpenAI-compatible base_url, the provider is
        # hermes-agent's built-in ``custom`` (matching the ``model.provider``
        # written to config.yaml). The HERMES_PROVIDER name (e.g. "litellm") is
        # only a config label, not a provider hermes 0.15.2 recognises on --provider.
        cli_provider = "custom" if base_url else provider
        args: list[str] = [
            self._executable, "chat",
            "-q", prompt,
            "--quiet",
            "--yolo",
            "--toolsets", ",".join(ALLOWED_TOOLSETS),
            "--provider", cli_provider,
            "--model", resolved_model,
        ]

        env = self._build_subprocess_env(
            profile_dir=self._profile_dir, api_key=api_key, base_url=base_url,
        )

        log.info(
            "hermes starting",
            provider=provider,
            model=resolved_model,
            base_url=base_url or None,
            hermes_home=str(self._profile_dir),
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
            log.exception("hermes subprocess spawn failed", executable=self._executable)
            self._finalize()
            raise

        self._status = BackendStatus(state="working")
        self._reader_task = asyncio.create_task(
            self._wait_and_parse(), name="hermes-reader"
        )
        log.info("hermes started", pid=self._proc.pid, model=resolved_model)

    def get_status(self) -> BackendStatus:
        return self._status

    def drain_events(self) -> list[BackendEvent]:
        events = list(self._event_buffer)
        self._event_buffer.clear()
        if self._api_key:
            for ev in events:
                if self._api_key in ev.detail:
                    ev.detail = ev.detail.replace(self._api_key, "***")
                if self._api_key in ev.text:
                    ev.text = ev.text.replace(self._api_key, "***")
        return events

    async def relay_feedback(self, feedback: str) -> None:
        """Queue feedback; replayed on the next ``start()`` invocation.

        One-shot pattern (FR-002 / R-004): no live session to interrupt.
        """
        self._feedback_queue.append(feedback)

    async def stop(self) -> None:
        # FR-010a: timeout enforcement lives in performer.main's watchdog,
        # which calls into this stop(). Hermes carries no internal timer.
        self._stop_requested = True
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                # CancelledError is expected (we just cancelled); other
                # exceptions are already logged by the reader itself.
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
            self._status = BackendStatus(
                state="error", error_reason="stopped"
            )
        self._finalize()
        log.info("hermes stopped")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _write_card_docs(self, workspace: Path, score: Score) -> Path | None:
        """077: materialise the card context as ``CARD.md`` for a persona that
        reads card-doc files (mirrors the openclaw reviewer fix).

        Hermes runs in ``cwd=stand.path`` so no workspace symlink is needed — but
        unlike the read-only reviewer, the tech_writer COMMITS, so the file is
        added to ``.git/info/exclude`` (a local, never-committed ignore list) to
        keep the scratch file out of the PR. Best-effort: failures are logged.
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
                    for q in entry.get("questions") or []:
                        parts.append(f"- Q: {q}")
                    answer = str(entry.get("answer", "")).strip()
                    if answer:
                        parts += [f"  A: {answer}", ""]
            card_md = workspace / "CARD.md"
            card_md.write_text("\n".join(parts) + "\n")
            # Keep the scratch file out of any commit the tech_writer makes.
            try:
                exclude = workspace / ".git" / "info" / "exclude"
                exclude.parent.mkdir(parents=True, exist_ok=True)
                existing = exclude.read_text() if exclude.is_file() else ""
                if "CARD.md" not in existing.split():
                    with exclude.open("a") as fh:
                        fh.write(("" if existing.endswith("\n") or not existing else "\n") + "CARD.md\n")
            except OSError as exc:
                log.warning("hermes.card_docs_exclude_failed", error=str(exc))
            log.info("hermes.card_docs_written", path=str(card_md), chars=len("\n".join(parts)))
            return card_md
        except OSError as exc:
            log.warning("hermes.card_docs_write_failed", error=str(exc))
            return None

    def _build_subprocess_env(
        self, *, profile_dir: Path, api_key: str, base_url: str
    ) -> dict[str, str]:
        base = {
            k: v for k, v in os.environ.items()
            if not any(k.startswith(p) for p in _FORBIDDEN_ENV_PREFIXES)
        }
        # 088 B1 shared env policy: hermes-agent is a Node CLI — it must
        # launch on the IMAGE's node, never the env-cache's project-pinned
        # node (e.g. 18.12.1 → startup crash). Image PATH first, cache
        # toolchain dirs appended; every other cache var flows through.
        env = build_subprocess_env(
            cache_env=self._cache_env,
            git_env=self._git_env,
            tool_env=self._tool_env,
            base_env=base,
        )
        # FR-005: operator's HERMES_HOME is never honoured. The pop is
        # belt-and-suspenders — the assignment below overwrites either way,
        # but the explicit drop makes the intent survive future refactors.
        env.pop("HERMES_HOME", None)
        env["HERMES_HOME"] = str(profile_dir)
        env["HERMES_API_KEY"] = api_key
        # Avoid silent inheritance: only forward HERMES_BASE_URL when this
        # job explicitly sets one. Otherwise strip any operator-level value
        # so misroutes can't hide off-argv.
        if base_url:
            env["HERMES_BASE_URL"] = base_url
        else:
            env.pop("HERMES_BASE_URL", None)
        return env

    def _write_profile_config(
        self, profile_dir: Path, *, provider: str = "", base_url: str = "",
        model: str = "", max_tokens: int | None = None,
        context_length: int | None = None,
    ) -> None:
        """Write ``$HERMES_HOME/config.yaml``.

        Always emits the ``disabled_toolsets`` deny-list (belt-and-suspenders
        for the CLI's ``--toolsets`` allow-list).

        077: hermes-agent 0.15.2 reads an OpenAI-compatible endpoint from the
        ``model:`` block (``provider: custom`` + ``base_url`` + ``default``), NOT
        the older ``providers: <name>: {base_url, key_env, api_mode}`` schema —
        which 0.15.2 silently ignores, leaving the agent with "no API keys or
        providers found" (verified against the installed CLI; would crash the
        tech_writer stage before it could connect). The credential is referenced
        via ``api_key_env`` so the secret never lands on disk: ``HERMES_API_KEY``
        is injected into the subprocess env by ``_build_subprocess_env``.
        ``provider`` is retained in the signature for back-compat but the
        OpenAI-compat path always uses the built-in ``custom`` provider.
        """
        cfg_lines: list[str] = [
            "approvals:",
            # Quote "off" so YAML doesn't coerce it to the boolean False.
            # hermes-agent's config layer expects the literal string "off"
            # (alongside "manual"/"smart") and silently falls back to manual
            # if it sees a bool here — defeating the whole point of the gate
            # disable.
            '  mode: "off"',
            "disabled_toolsets:",
        ]
        cfg_lines += [f"  - {name}" for name in DISABLED_TOOLSETS]
        if base_url:
            cfg_lines += [
                "model:",
                "  provider: custom",
                f"  base_url: {base_url}",
                "  api_key_env: HERMES_API_KEY",
            ]
            if model:
                cfg_lines.append(f"  default: {model}")
            # 124: hermes-agent reads model.max_tokens (output cap) and
            # model.context_length (agent_init.py:1455-1484). Left unset it uses
            # the server default (often small), so context-heavy roles like the
            # documenter truncate/derail. Emit both when configured so the model
            # gets an adequate output + input budget.
            if max_tokens:
                cfg_lines.append(f"  max_tokens: {int(max_tokens)}")
            if context_length:
                cfg_lines.append(f"  context_length: {int(context_length)}")
        cfg_lines.append("")
        (profile_dir / "config.yaml").write_text("\n".join(cfg_lines))

    def _persist_job_artifact(self, suffix: str, content: str) -> None:
        """Write a per-job artifact to ``$PERFORMER_LOG_DIR`` if configured.

        ``PERFORMER_LOG_DIR`` is set by the coordinare only when
        ``performer_log_dir`` is configured in ``config.yaml`` (see
        ``coordinare.__main__._build_http_performer_services``). When
        unset, this method is a no-op — artifacts are still surfaced to
        the coordinare through ``BackendEvent`` payloads regardless.

        Best-effort: silently no-ops when the env var is unset or the
        dir is not writable, so a misconfigured log mount can never
        block a job. API keys are redacted before writing.
        """
        log_dir = os.environ.get("PERFORMER_LOG_DIR", "").strip()
        if not log_dir or not self._job_log_id:
            return
        try:
            path = Path(log_dir)
            path.mkdir(parents=True, exist_ok=True)
            target = path / f"{self._job_log_id}.{suffix}"
            redacted = content
            if self._api_key and self._api_key in redacted:
                redacted = redacted.replace(self._api_key, "***")
            target.write_text(redacted)
        except OSError as exc:
            log.warning(
                "hermes persist_artifact failed",
                suffix=suffix, error=str(exc), dir=log_dir,
            )

    def _emit(self, type: BackendEventType, text: str, detail: str = "") -> None:
        self._event_buffer.append(
            BackendEvent(type=type, text=text[:_MAX_TEXT], detail=detail)
        )

    def _finalize(self) -> None:
        """Idempotent cleanup of job-scoped state. Safe on every terminal outcome."""
        self._terminal = True
        if self._json_output_path is not None:
            try:
                self._json_output_path.unlink(missing_ok=True)
            except OSError:
                pass
            self._json_output_path = None
        if self._profile_dir is not None:
            removed = str(self._profile_dir)
            try:
                shutil.rmtree(self._profile_dir, ignore_errors=False)
                log.info("hermes finalized", removed=removed)
            except FileNotFoundError:
                pass
            except OSError as exc:
                log.warning("hermes finalize failed", path=removed, error=str(exc))
            self._profile_dir = None
        self._proc = None

    async def _wait_and_parse(self) -> None:
        if self._proc is None:
            return
        try:
            stdout_b, stderr_b = await self._proc.communicate()
            rc = self._proc.returncode
            stdout_text = stdout_b.decode(errors="replace") if stdout_b else ""
            stderr_text = stderr_b.decode(errors="replace") if stderr_b else ""
            self._persist_job_artifact("stdout", stdout_text)
            self._persist_job_artifact("stderr", stderr_text)

            if self._stop_requested:
                # stop() owns the terminal transition.
                return

            parsed = _extract_json_object(stdout_text)

            if rc != 0:
                reason = f"subprocess_exit:{rc}"
                self._status = BackendStatus(
                    state="error",
                    error_reason=reason,
                )
                snippet = (stderr_text.strip() or stdout_text.strip())[-_MAX_TEXT:]
                log.warning(
                    "hermes failed",
                    rc=rc,
                    stderr=stderr_text[-1000:],
                    stdout=stdout_text[-500:],
                )
                if snippet:
                    self._emit(BackendEventType.error, snippet)
                self._finalize()
                return

            # rc == 0: JSON output is only contractually required for roles
            # in _JSON_ONLY_ROLES (their prompts ask for "ONLY a valid JSON
            # object"). For prose-contract roles (implementer, architect,
            # env_bootstrap, ...) the wrapper consumes side effects (commits,
            # cache state), so accept any rc==0 outcome as done and surface
            # the tail of stdout as the summary.
            role = (self._score.role or "implementer").lower() if self._score else "implementer"
            if role in _JSON_ONLY_ROLES and (not isinstance(parsed, dict) or not parsed):
                self._status = BackendStatus(
                    state="error",
                    error_reason="malformed_output",
                )
                self._emit(BackendEventType.error, "malformed_output")
                self._finalize()
                return

            # `output` must carry the full stdout so JSON-role consumers in
            # main.py can re-extract their schema (their JSON rarely contains
            # summary/result/message keys).
            summary: str | None = None
            tokens: int | None = None
            if isinstance(parsed, dict) and parsed:
                maybe_summary = (
                    parsed.get("summary")
                    or parsed.get("result")
                    or parsed.get("message")
                )
                if isinstance(maybe_summary, str):
                    summary = maybe_summary
                maybe_tokens = parsed.get("tokens") or parsed.get("tokens_processed")
                if isinstance(maybe_tokens, int):
                    tokens = maybe_tokens

            output_text = stdout_text.strip() or None

            self._status = BackendStatus(
                state="done",
                tokens_processed=tokens,
                output=output_text,
            )
            event_text = summary if summary else (output_text or "")
            if event_text:
                self._emit(BackendEventType.progress, event_text[:_MAX_TEXT])
            self._finalize()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("hermes reader error", error=str(exc))
            self._status = BackendStatus(state="error", error_reason=str(exc))
            self._finalize()


def _env_int(name: str) -> int | None:
    """Read a positive int from env *name*; None when unset/blank/invalid.

    Used for HERMES_CONTEXT_WINDOW (the model context budget), mirroring
    openclaw's OPENCLAW_CONTEXT_WINDOW pattern.
    """
    raw = os.environ.get(name, "").strip()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if value > 0 else None


def _extract_json_object(text: str) -> dict | None:
    """Best-effort extraction of a JSON object from hermes stdout.

    ``--quiet`` still emits a session-info footer alongside the agent's
    reply, so a bare ``json.loads`` on the full text usually fails. We:
    1. Try parsing the whole stripped output.
    2. Try the outermost ``{...}`` span (catches "<reply>{...}<footer>").
    3. Walk every balanced ``{...}`` span (largest first) and return the
       first that parses to a dict — defends against an unrelated brace
       elsewhere in the footer poisoning step 2.
    """
    if not text:
        return None
    stripped = text.strip()
    try:
        obj = json.loads(stripped)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass
    outer_start = stripped.find("{")
    outer_end = stripped.rfind("}")
    if outer_start == -1 or outer_end <= outer_start:
        return None
    try:
        obj = json.loads(stripped[outer_start : outer_end + 1])
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass

    # Step 3: enumerate balanced spans and try each, largest first.
    spans: list[tuple[int, int]] = []
    stack: list[int] = []
    for i, ch in enumerate(stripped):
        if ch == "{":
            stack.append(i)
        elif ch == "}" and stack:
            start = stack.pop()
            spans.append((start, i))
    spans.sort(key=lambda s: s[1] - s[0], reverse=True)
    for start, end in spans:
        try:
            obj = json.loads(stripped[start : end + 1])
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


def _conventional_plan_path(score: Score) -> str | None:
    """Re-derive ``docs/cards/{issue}-{slug}/plan.md`` from the score.

    Delegates to the shared ``card_doc_folder`` helper so the convention
    stays in one place. Used as a fallback when
    ``score.architecture_plan_path`` is unset — e.g. after a coordinare
    restart that lost ``card["plan_path"]`` from its snapshot but the
    architect's plan commit is still on the branch.
    """
    folder = card_doc_folder(score)
    return f"{folder}/plan.md" if folder else None


def _read_plan_inline(plan_path: str, stand_path: Path | None) -> str | None:
    """Best-effort read of the architecture plan for prompt inlining.

    Resolves ``plan_path`` against ``stand_path`` (the repo checkout) when
    relative, caps content at ``_MAX_INLINED_PLAN_CHARS``, and returns
    ``None`` on any failure so the caller can fall back to a path-only
    reference. Hermes' own file toolset has been observed to skip reading
    referenced paths and ask for clarification instead; inlining removes
    that hop.
    """
    if not plan_path:
        return None
    try:
        candidate = Path(plan_path)
        if not candidate.is_absolute() and stand_path is not None:
            candidate = stand_path / plan_path
        text = candidate.read_text()
    except OSError:
        return None
    if not text.strip():
        return None
    if len(text) > _MAX_INLINED_PLAN_CHARS:
        text = (
            text[:_MAX_INLINED_PLAN_CHARS]
            + f"\n\n…[truncated at {_MAX_INLINED_PLAN_CHARS} chars; read full plan from `{plan_path}`]"
        )
    return text


def _build_task_prompt(
    score: Score,
    queued_feedback: list[str],
    *,
    stand_path: Path | None = None,
) -> str:
    """Construct the prompt for ``hermes chat -q``.

    Mirrors the structure used by ``junie._build_task_prompt`` but folds
    queued relay feedback (one-shot replay) in front of any payload-side
    ``score.relay_feedback``.
    """
    # 124 (C): write-phase of the documenter plan->write decomposition. A single
    # focused page write, deliberately WITHOUT the PR diff / plan / card dump so
    # the model's context stays small (that is the whole point of decomposing).
    tgt = getattr(score, "doc_write_target", None)
    if isinstance(tgt, dict) and tgt.get("path"):
        wparts: list[str] = [
            f"# Task: Write ONE documentation page — `{tgt['path']}`", "",
            f"Intent for this page: {tgt.get('intent', 'update it to reflect the current code')}",
            "",
        ]
        current = str(tgt.get("current") or "")
        if current.strip():
            wparts += [
                "## Current content of this page (update it — keep what is still accurate)",
                "", "```markdown", current.rstrip("\n"), "```", "",
            ]
        else:
            wparts += ["This page does not exist yet — create it.", ""]
        wparts += ["", "---"]
        wparts += _role_output_block(score.role or "tech_writer", score)
        return "\n".join(wparts)

    # FR-015: persona_instructions is written to `$HERMES_HOME/SOUL.md` at
    # start() so hermes-agent loads it as agent identity. Keeping it out of
    # the task prompt body avoids delivering persona twice.
    parts: list[str] = [f"# Task: {score.title}", ""]
    if score.description:
        parts += [score.description, ""]
    parts += card_docs_prompt_section(score, stand_path)
    parts += qa_findings_prompt_section(score)
    if score.acceptance_criteria:
        parts += ["## Acceptance Criteria", ""]
        parts.extend(f"- {c}" for c in score.acceptance_criteria)
    if score.clarifications:
        parts += ["", "## Clarification Q&A", ""]
        for entry in score.clarifications:
            questions = entry.get("questions") or []
            answer = str(entry.get("answer", "")).strip()
            if questions:
                parts.append("**Questions asked:**")
                parts.extend(f"- {q}" for q in questions)
            if answer:
                parts += [f"**Answer:** {answer}", ""]

    if score.pr_diff:
        parts += [
            "", "## PR Diff Under Review", "",
            "The unified diff below is the complete set of changes on this PR. "
            "Review it directly — do NOT report that no changes were supplied.",
            "", "```diff", score.pr_diff.rstrip("\n"), "```", "",
        ]

    # 126: disputes of this stage's own prior findings — adjudicate, don't
    # treat as fresh work items.
    if getattr(score, "disputed_feedback", None):
        parts += [
            "", "## Disputed feedback to adjudicate", "",
            "The implementer disputed these items you previously raised. "
            "Re-examine each one: if the dispute is valid, do NOT re-raise it "
            "(pass if nothing else is wrong); if it is invalid, re-raise it "
            "explicitly in your verdict.",
            "",
        ]
        for _d in score.disputed_feedback:
            if isinstance(_d, dict) and _d.get("id"):
                parts.append(
                    f"- {_d['id']}: {_d.get('body', '')} — implementer says: "
                    f"{_d.get('reason', '')}"
                )

    if queued_feedback or score.relay_feedback:
        parts += [
            "", "## Human Feedback (address ALL of these issues)", "",
            "IMPORTANT: These comments may only tag a few examples. Search "
            "the entire codebase for ALL similar occurrences of the same "
            "pattern and fix them all.",
            "",
        ]
        for fb in queued_feedback:
            if fb:
                parts.append(f"- {fb}")
        for item in score.relay_feedback:
            if isinstance(item, dict):
                body = item.get("body", "")
                if body:
                    parts.append(f"- {body}")
            elif isinstance(item, str):
                parts.append(f"- {item}")

    plan_path = score.architecture_plan_path
    if not plan_path and stand_path is not None:
        candidate = _conventional_plan_path(score)
        if candidate and (stand_path / candidate).is_file():
            plan_path = candidate
    if plan_path:
        inlined = _read_plan_inline(plan_path, stand_path)
        if inlined is not None:
            parts += [
                "",
                "## Architecture Plan",
                "",
                f"_The plan below was committed at `{plan_path}` on this branch._",
                "",
                inlined,
                "",
            ]
        else:
            parts += [
                "",
                f"## Architecture Plan\n\nSee `{plan_path}` on this branch.",
                "",
            ]

    parts += ["", "---"]
    role = score.role or "implementer"
    parts += _role_output_block(role, score)
    return "\n".join(parts)


def _role_output_block(role: str, score: "Score | None" = None) -> list[str]:
    """Role-specific output requirements appended to the prompt (FR-001a)."""
    if role == DIAGNOSTIC_ROLE:
        return [
            "## Role Output Requirements (diagnostic)",
            "This is a one-off diagnostic/benchmark task. Use any tools at your "
            "disposal to complete it. You do NOT need to commit, push, or open a "
            "pull request — just perform the task and report what you did.",
        ]
    if role == "tech_writer":
        # 124 (B+C): the concrete contract lives IN THE TASK PROMPT (not only
        # SOUL.md, which hermes-agent doesn't reliably honor for gpt-oss). The
        # documenter runs as a plan->write decomposition (C): the FIRST call PLANS
        # which pages to touch (tiny output); each later call WRITES one page
        # (small output). Both explicitly forbid the observed edit-code/verify
        # drift.
        target = getattr(score, "doc_write_target", None) if score is not None else None
        if isinstance(target, dict) and target.get("path"):
            # WRITE phase: exactly one page.
            return [
                "## Role Output Requirements (tech_writer) — write one page",
                "You are the DOCUMENTER. Do NOT edit source code, run tests, or "
                "write a verification report.",
                "Return ONLY JSON with EXACTLY this one file and nothing else:",
                # json.dumps escapes the path so a quote/backslash can't produce a
                # malformed example (the model is shown this as the exact shape).
                '{"files": [{"path": ' + json.dumps(str(target["path"])) + ', '
                '"content": "<the FULL markdown content of this page>"}]}',
                '- "content" is the COMPLETE page — it REPLACES the file wholesale.',
                "- Emit ONLY this one file. Do not add other files or a deletions key.",
                "Do not include prose or code fences around the JSON.",
            ]
        # PLAN phase (default first call): decide pages, no content yet.
        return [
            "## Role Output Requirements (tech_writer) — plan",
            "You are the DOCUMENTER. FIRST, PLAN the wiki changes — do NOT write "
            "page content yet, do NOT edit source code, and do NOT write a "
            "verification report.",
            "Return ONLY this JSON plan and nothing else:",
            '{"pages": [{"path": "docs/wiki/<page>.md", "intent": "<one line: what '
            'to add/change on this page>"}], "deletions": ["docs/wiki/<dead-page>.md"]}',
            "- List ONLY pages THIS change actually warrants creating/updating. Be "
            'conservative: for a trivial/cosmetic change return {"pages": [], "deletions": []}.',
            "- If `AGENTS.md` or `CLAUDE.md` does not yet contain a `## Project Wiki` "
            "pointer to `docs/wiki/`, include that file as a page so agents can "
            "discover the wiki (preserve its existing content when you write it).",
            '- "deletions" (optional, docs/ only) retires dead or low-value pages.',
            "- You will then be asked to write each planned page one at a time.",
            "Do not include prose or code fences around the JSON.",
        ]
    if role in _JSON_ONLY_ROLES:
        return [
            f"## Role Output Requirements ({role})",
            "Return ONLY a valid JSON object for your role contract.",
            "Do not include markdown, prose, or code fences.",
        ]
    if role == "architect":
        return [
            "## Role Output Requirements (architect)",
            "Produce an architecture plan as a markdown file and commit it.",
            "Do not open a pull request — the performer wrapper handles that.",
        ]
    if role == "env_bootstrap":
        return [
            "## Role Output Requirements (env_bootstrap)",
            "Run the environment bootstrap commands described above against the "
            "mounted env-cache directory. Do not commit, push, or open a pull "
            "request — there is no PR for this job.",
            "When finished, print a short prose summary of what you installed, "
            "configured, or detected (package managers, language toolchains, "
            "service endpoints). The wrapper inspects the cache directory and "
            "running services directly; the summary is for the human log.",
        ]
    # implementer + everything else
    return [
        f"## Role Output Requirements ({role})",
        "Complete the task above. Commit your changes with a clear, descriptive commit message.",
        "Do not push or open a pull request — this will be handled automatically after you finish.",
    ]
