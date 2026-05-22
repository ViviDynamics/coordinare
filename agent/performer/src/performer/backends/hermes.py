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

from performer.backends._card_docs import card_doc_folder, card_docs_prompt_section
from performer.backends.base import BackendStatus
from performer.models import BackendEvent, BackendEventType, Score, Stand

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
        self._write_profile_config(
            self._profile_dir, provider=provider, base_url=base_url,
        )
        # FR-015: route persona to hermes-agent's canonical identity slot.
        # When persona_instructions is empty we leave SOUL.md absent so
        # hermes-agent's own starter behaviour applies.
        if score.persona_instructions:
            (self._profile_dir / "SOUL.md").write_text(score.persona_instructions)

        # Build prompt; drain feedback queue into it.
        prompt = _build_task_prompt(
            score, list(self._feedback_queue), stand_path=stand.path,
        )
        self._feedback_queue.clear()
        self._last_prompt = prompt
        self._job_log_id = f"{time.strftime('%Y%m%dT%H%M%S')}_{(score.role or 'job')}_{uuid.uuid4().hex[:8]}"
        self._persist_job_artifact("prompt", prompt)

        # The installed hermes-agent CLI exposes neither --output-file nor
        # --base-url on `chat`, and -z is a top-level shortcut rather than a
        # chat-subcommand flag. We capture the agent's final reply from stdout
        # (--quiet suppresses banner / spinner / tool previews so what remains
        # is the response plus minimal session-info chrome) and route the
        # endpoint through HERMES_BASE_URL in the subprocess env.
        args: list[str] = [
            self._executable, "chat",
            "-q", prompt,
            "--quiet",
            "--toolsets", ",".join(ALLOWED_TOOLSETS),
            "--provider", provider,
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

    def _build_subprocess_env(
        self, *, profile_dir: Path, api_key: str, base_url: str
    ) -> dict[str, str]:
        env = {
            k: v for k, v in os.environ.items()
            if not any(k.startswith(p) for p in _FORBIDDEN_ENV_PREFIXES)
        }
        env.update(self._cache_env)
        env.update(self._git_env)
        env.update(self._tool_env)
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
    ) -> None:
        """Write ``hermes.config.yaml``.

        Always emits the ``disabled_toolsets`` deny-list (belt-and-suspenders
        for the CLI's ``--toolsets`` allow-list). When ``base_url`` is set, also
        registers ``provider`` as a user-defined OpenAI-compatible endpoint
        under ``providers:`` so ``hermes chat --provider <name>`` resolves to
        ``base_url`` with ``HERMES_API_KEY`` as the credential. We reference
        the key via ``key_env`` rather than embedding it so the secret never
        lands on disk.
        """
        cfg_lines: list[str] = ["disabled_toolsets:"]
        cfg_lines += [f"  - {name}" for name in DISABLED_TOOLSETS]
        if base_url and provider:
            cfg_lines += [
                "providers:",
                f"  {provider}:",
                f"    base_url: {base_url}",
                "    key_env: HERMES_API_KEY",
                "    api_mode: chat_completions",
            ]
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

            if isinstance(parsed, dict) and parsed:
                summary = (
                    parsed.get("summary")
                    or parsed.get("result")
                    or parsed.get("message")
                )
                tokens = parsed.get("tokens") or parsed.get("tokens_processed")
            else:
                summary = stdout_text.strip()[-_MAX_TEXT:] or None
                tokens = None

            self._status = BackendStatus(
                state="done",
                tokens_processed=tokens if isinstance(tokens, int) else None,
                output=summary if isinstance(summary, str) else None,
            )
            if isinstance(summary, str) and summary:
                self._emit(BackendEventType.progress, summary[:_MAX_TEXT])
            self._finalize()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("hermes reader error", error=str(exc))
            self._status = BackendStatus(state="error", error_reason=str(exc))
            self._finalize()


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
    # FR-015: persona_instructions is written to `$HERMES_HOME/SOUL.md` at
    # start() so hermes-agent loads it as agent identity. Keeping it out of
    # the task prompt body avoids delivering persona twice.
    parts: list[str] = [f"# Task: {score.title}", ""]
    if score.description:
        parts += [score.description, ""]
    parts += card_docs_prompt_section(score, stand_path)
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
    parts += _role_output_block(role)
    return "\n".join(parts)


def _role_output_block(role: str) -> list[str]:
    """Role-specific output requirements appended to the prompt (FR-001a)."""
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
