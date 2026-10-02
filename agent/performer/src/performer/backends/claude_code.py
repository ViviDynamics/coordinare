"""ClaudeCodeBackend — integrates Claude Code via ``--output-format stream-json``.

Lifecycle:
    start()          → launches ``claude --print --output-format stream-json
                       --include-partial-messages -p <prompt>``,
                       captures session_id from the init event,
                       begins reader task
    get_status()     → returns current BackendStatus (non-blocking)
    drain_events()   → return and clear buffered BackendEvent list
    relay_feedback() → resumes the captured session with feedback as a new prompt
                       (``claude --print ... --resume <session_id> -p <feedback>``)
    stop()           → kills process group; waits for subprocess to exit
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import signal
import shlex
import sys
import tempfile
import time
from collections import deque
from pathlib import Path
from typing import IO

import psutil
import structlog

from performer.backends._scope import scope_prompt_section
from performer.backends._card_docs import scanner_findings_prompt_section, repair_mandate_prompt_section, card_docs_prompt_section, qa_findings_prompt_section, brief_prompt_sections
from performer.backends._env_policy import build_subprocess_env
from performer.backends.base import BackendStatus
from performer.backends.claude_code_shim import ClaudeCodeShim
from performer.config import Settings, get_settings
from performer.io_utils import iter_lines_chunked
from performer.models import DIAGNOSTIC_ROLE, LOCAL_CAPTURE_RULE, BackendEvent, BackendEventType, Score, Stand

log = structlog.get_logger(__name__)

_MAX_TEXT = 200
_STDERR_TAIL_LINES = 50
_STDERR_ERROR_MAX_CHARS = 4000
# Max consecutive system:api_retry events tolerated before we stop trusting the
# CLI's internal retry loop and surface failure ourselves. The CLI itself caps
# at max_retries=10 with backoff, but observed wedge: upstream returns 504, CLI
# emits attempt=1, then the SSE stream stalls forever. Tripping at 3 keeps
# transient flakes recoverable while still catching the stall pattern fast.
_API_RETRY_MAX = 3
# FR-011 / FR-004: scrub Bearer tokens and ANTHROPIC_AUTH_TOKEN values from any
# stderr surfaced into error_reason / logs.
_BEARER_RE = re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._\-]+")
_JSON_ONLY_ROLES = {
    "assessing",
    "assessor",
    "reviewing",
    "reviewer",
    "closing_review",
    "closer",
    "security",
    "qa",
    "documenting",
    "tech_writer",
}


class ClaudeCodeBackend:
    """Backend adapter that drives ``claude --print --output-format stream-json``.

    The initial task is passed directly via ``-p``.  After the session completes,
    ``relay_feedback`` resumes it with ``--resume <session_id>`` so the full
    conversation context (edits, tool calls, previous messages) is preserved.
    """

    def __init__(self) -> None:
        self._proc: asyncio.subprocess.Process | None = None
        self._status: BackendStatus = BackendStatus(state="working")
        self._reader_task: asyncio.Task[None] | None = None
        self._event_buffer: deque[BackendEvent] = deque(maxlen=200)
        self._stand: Stand | None = None
        self._session_id: str | None = None  # captured from system/init event
        self._git_env: dict[str, str] = {}
        self._cache_env: dict[str, str] = {}
        self._tool_env: dict[str, str] = {}
        # 073 — LiteLLM proxy env injection (claude_code only).  Populated at
        # construction from boot-time Settings and treated as DEFAULTS only:
        # per-job ANTHROPIC_* values injected into os.environ by _perform_job
        # (from JobInitPayload.secrets) take precedence at _launch so operators
        # can override proxy base URL / auth per dispatch.
        self._proxy_env: dict[str, str] = self._load_proxy_env(get_settings())
        # 073 — response-translation shim, lifecycle bound to _launch / stop.
        # ``None`` when no proxy is configured (preserves SC-002 baseline).
        self._shim: ClaudeCodeShim | None = None
        self._model: str | None = None  # 037: per-role model selection
        self._max_tokens: int | None = None  # 055: output token cap
        self._tool_budget_dir = None
        self._tool_budget_settings: str | None = None
        self._persona: str | None = None  # FR-016: routed via --append-system-prompt
        # Accumulates assistant text blocks across the stream; flushed into
        # BackendStatus.output on result.success.  Reset at every _launch so
        # relay_feedback / resume runs don't bleed prior session text.
        self._output_accumulator: list[str] = []
        # Bounded tail of claude's stderr; surfaced (token-redacted) into
        # error_reason on non-zero exit so opaque CLI failures aren't silent.
        self._stderr_tail: deque[str] = deque(maxlen=_STDERR_TAIL_LINES)
        self._stderr_task: asyncio.Task[None] | None = None
        # Raw CLI stdout capture (stream-json lines).  Enabled when
        # LITELLM_PROXY_CAPTURE_DIR is set so a hung reader loop can be
        # diagnosed against the actual bytes the CLI emitted.  None when
        # capture is disabled or the file couldn't be opened.
        self._stdout_capture: IO[bytes] | None = None
        # Last time _event_reader_loop saw a *progress* stream-json line. Read
        # by the idle-timeout branch of the reader loop; updated only when
        # _handle_event returns True (assistant/tool/result/init), NOT for
        # api_retry — a retry event is the inverse of progress.
        self._last_event_at: float = 0.0
        # Consecutive system:api_retry events without an intervening progress
        # event. Reset when a progress event arrives. When this reaches
        # _API_RETRY_MAX the backend surfaces an error rather than waiting on
        # the CLI to give up.
        self._api_retry_count: int = 0
        # 509: progress events (init/assistant/tool/result) parsed from the
        # CLI since the last _launch. A run whose process exits 0 with this
        # still at 0 never ran the agent — surfaced as an error by
        # get_status so the coordinare bounces instead of reading success.
        self._events_seen: int = 0
        # Additional directories to add to Claude Code's working-dir allowlist
        # via --add-dir. Populated from ``score.env_cache_path`` in start() so
        # env_bootstrap roles can write to ``/devenv/<symphony>-<hash>`` (which
        # lives outside the stand cwd and would otherwise be sandbox-blocked).
        self._extra_dirs: list[str] = []

    # ------------------------------------------------------------------
    # 073 — LiteLLM proxy env injection
    # ------------------------------------------------------------------

    @staticmethod
    def _load_proxy_env(settings: Settings) -> dict[str, str]:
        """Return env vars to inject into the claude CLI subprocess.

        Returns an empty dict (preserving the pre-feature byte-identical env
        baseline per FR-003 / SC-002) when ``LITELLM_PROXY_BASE_URL`` is
        empty.  When set, ``ANTHROPIC_BASE_URL`` is always emitted; the auth
        token is only emitted when ``LITELLM_PROXY_AUTH_TOKEN`` is non-empty.

        The auth token MUST NOT be logged here (FR-004 / SC-004).
        """
        base_url = settings.LITELLM_PROXY_BASE_URL.strip()
        if not base_url:
            return {}
        env: dict[str, str] = {"ANTHROPIC_BASE_URL": base_url}
        token = settings.LITELLM_PROXY_AUTH_TOKEN
        if token:
            env["ANTHROPIC_AUTH_TOKEN"] = token
        return env

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
        """Build the prompt and launch ``claude --print --output-format stream-json``."""
        if self._tool_budget_dir is not None:
            self._tool_budget_dir.cleanup()
        self._tool_budget_dir = None
        self._tool_budget_settings = None
        if score.max_tool_calls is not None:
            scope_prompt_section(score, "claude_code")  # reject unsupported platforms
            self._tool_budget_dir = tempfile.TemporaryDirectory(prefix="coordinare-tool-budget-")
            command = shlex.join([sys.executable, str(Path(__file__).with_name("_tool_budget_hook.py")),
                                  self._tool_budget_dir.name, str(score.max_tool_calls)])
            self._tool_budget_settings = json.dumps({"hooks": {"PreToolUse": [{
                "matcher": "", "hooks": [{"type": "command", "command": command}],
            }]}})
        self._stand = stand
        self._git_env = stand.git_env
        self._cache_env = stand.cache_env
        self._tool_env = score.tool_env
        self._model = model
        self._max_tokens = max_tokens
        self._persona = (score.persona_instructions or "").strip() or None
        # Allow writes to the per-job devenv target (and its parent) so
        # env_bootstrap can mkdir ``/devenv/<symphony>-<hash>``. Empty for
        # non-bootstrap roles, which leaves the allowlist at the stand cwd.
        self._extra_dirs = []
        env_cache_path = (score.env_cache_path or "").strip()
        if env_cache_path:
            parent = str(Path(env_cache_path).parent)
            self._extra_dirs = [parent, env_cache_path]
        prompt = _build_task_prompt(score, stand_path=Path(stand.path))
        # 509: record the delivery of relayed review feedback as an event so
        # the session record shows whether the comments reached the prompt —
        # the incident left no trace of this and could not be diagnosed.
        if score.relay_feedback:
            reviews = [r for r in score.relay_feedback if isinstance(r, dict)]
            inline = sum(len(r.get("comments") or []) for r in reviews)
            self._emit(
                BackendEventType.progress,
                f"relay feedback in prompt: {len(reviews)} review(s), "
                f"{inline} inline comment(s)",
            )
        await self._launch(prompt)

    def get_status(self) -> BackendStatus:
        if self._tool_budget_dir and (Path(self._tool_budget_dir.name) / "exhausted").exists():
            return BackendStatus(state="error", error_reason="TOOL_BUDGET_EXHAUSTED: max_tool_calls reached")
        # Liveness fallback: if the subprocess has exited but the reader task
        # never produced a terminal stream-json event (e.g. CLI crashed before
        # emitting result.success, or the reader_task is wedged on a half-open
        # pipe), force a terminal BackendStatus so JobRunner doesn't poll
        # forever and the coordinare's serialize_env_bootstrap gate releases.
        if (
            self._status.state == "working"
            and self._proc is not None
            and self._proc.returncode is not None
        ):
            rc = self._proc.returncode
            if rc == 0:
                # 509: rc=0 is only a healthy completion when the CLI actually
                # produced stream-json events (init at minimum). A silent rc=0
                # exit means the agent never ran; reporting done here masked
                # that as a success (the #509 re-dispatch incident).
                if self._events_seen == 0:
                    reader = self._reader_task
                    if reader is not None and not reader.done():
                        # 509 round 2: the process has exited but the reader
                        # task may still be consuming buffered stdout — a
                        # healthy run must not be declared dead while it is
                        # active. The reader's own idle timeout bounds a
                        # wedged pipe, and its finally applies this same
                        # verdict once it finishes.
                        return self._status
                    # "subprocess_exit" is the coordinare's transient marker
                    # (monitor/errors.py): the session bounces for a fresh
                    # dispatch instead of parking the card in Blocked.
                    tail = self._stderr_tail_text()
                    reason = (
                        "subprocess_exit: claude exited with code 0 "
                        "without producing any events"
                    )
                    if tail:
                        reason = f"{reason}: {tail}"
                    self._status = BackendStatus(state="error", error_reason=reason)
                else:
                    self._status = BackendStatus(state="done")
            else:
                tail = self._stderr_tail_text()
                reason = f"claude exited with code {rc} without terminal event"
                if tail:
                    reason = f"{reason}: {tail}"
                self._status = BackendStatus(state="error", error_reason=reason)
        return self._status

    def drain_events(self) -> list[BackendEvent]:
        events = list(self._event_buffer)
        self._event_buffer.clear()
        return events

    async def relay_feedback(self, feedback: str) -> None:
        """Resume the session with feedback as the next user prompt.

        Stops any currently running process first, then relaunches with
        ``--resume <session_id>`` so the session history is preserved.
        """
        await self.stop()
        if self._stand is None:
            return
        await self._launch(feedback, resume_session_id=self._session_id)
        self._status = BackendStatus(state="working")

    async def stop(self) -> None:
        """Kill the claude process group and wait for it to exit."""
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
            try:
                await self._reader_task
            except (asyncio.CancelledError, Exception):
                pass

        if self._stderr_task and not self._stderr_task.done():
            self._stderr_task.cancel()
            try:
                await self._stderr_task
            except (asyncio.CancelledError, Exception):
                pass

        if self._proc is not None and self._proc.returncode is None:
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
                await asyncio.wait_for(self._proc.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                pass

        # 073 — tear down the response-translation shim after the CLI exits.
        # ``stop()`` is also called by ``relay_feedback`` before relaunch, so
        # the next ``_launch`` will spin up a fresh shim with a fresh port.
        if self._shim is not None:
            try:
                await self._shim.stop()
            except Exception as exc:  # pragma: no cover — best-effort teardown
                log.warning("claude_code shim stop error", error=str(exc))
            self._shim = None
        log.info("claude code stopped")

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _launch(
        self,
        prompt: str,
        *,
        resume_session_id: str | None = None,
    ) -> None:
        """Start a claude subprocess with the given prompt."""
        self._events_seen = 0
        args = [
            "claude",
            "--print",
            "--output-format", "stream-json",
            "--include-partial-messages",
            "--verbose",
            # The performer container is itself the trust boundary; the CLI's
            # interactive permission gate has no human to approve prompts and
            # defaults to denying them, which traps the model in self-repair
            # loops on safe operations (cp -a, compound bash, source). The
            # two flags are layered: --dangerously-skip-permissions disables
            # interactive approval prompts; --permission-mode bypassPermissions
            # additionally turns off the CLI's hard-coded Bash heuristics that
            # would otherwise still reject compound commands and cp -a.
            "--dangerously-skip-permissions",
            "--permission-mode", "bypassPermissions",
        ]
        if self._tool_budget_settings is not None:
            args += ["--settings", self._tool_budget_settings]
        if self._model:
            args += ["--model", self._model]
        # 077: the Claude Code CLI has no `--max-tokens` flag (passing it aborts
        # with "error: unknown option '--max-tokens'", exit 1 — it crashed the qa
        # stage). The output-token cap is configured via the
        # CLAUDE_CODE_MAX_OUTPUT_TOKENS env var instead (set in subproc_env below).
        # FR-016: route persona to Claude Code's native system-prompt slot
        # instead of embedding it in the task prompt body.
        if self._persona:
            args += ["--append-system-prompt", self._persona]
        if resume_session_id:
            args += ["--resume", resume_session_id]
        for extra_dir in self._extra_dirs:
            args += ["--add-dir", extra_dir]
        # Pass the prompt via stdin rather than as a positional argv element so
        # that large diffs (hundreds of KiB) don't exceed the OS ARG_MAX limit.
        # Claude Code in --print mode reads from stdin when no positional prompt
        # is supplied.
        args += ["--print"]
        prompt_bytes = prompt.encode()

        self._output_accumulator = []
        self._stderr_tail.clear()

        # _proxy_env (ANTHROPIC_BASE_URL / ANTHROPIC_AUTH_TOKEN derived from
        # boot-time Settings) provides defaults only — per-job values injected
        # into os.environ by _perform_job from JobInitPayload.secrets must win,
        # so operators can override the proxy auth/base URL per dispatch.
        # 088 B1 shared env policy (originated here as 087's append fix): the
        # CLI must launch on the IMAGE's node — the env-cache prepends the
        # project's .nvmrc node (e.g. 18.12.1), which crashes modern Node CLIs
        # at startup. But the cache PATH must stay REACHABLE: Claude Code
        # snapshots the launch PATH for its Bash tool and does NOT re-source
        # activate.sh per command, so stripping it left qa without
        # ruby/bundle. Image PATH first, cache toolchain dirs appended.
        subproc_env = build_subprocess_env(
            cache_env=self._cache_env,
            git_env=self._git_env,
            tool_env=self._tool_env,
            # Claude Code refuses --dangerously-skip-permissions when running as
            # root unless IS_SANDBOX=1 is set. The performer container is the
            # sandbox boundary, so opt into the documented escape hatch.
            extra={"IS_SANDBOX": "1"},
        )
        # 077: cap output tokens via the env var the CLI actually honours
        # (there is no --max-tokens flag). Caller-provided value wins over any
        # ambient setting.
        if self._max_tokens is not None:
            subproc_env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(self._max_tokens)
        for _k, _v in self._proxy_env.items():
            subproc_env.setdefault(_k, _v)

        # 073 — when LiteLLM proxy is configured at boot, route the CLI through
        # the in-container response-translation shim.  ``self._proxy_env`` is
        # empty when ``LITELLM_PROXY_BASE_URL`` is unset (FR-003 / SC-002), so
        # this branch is a no-op for the default path.
        #
        # Fail-closed (FR-010): if ``shim.start()`` raises, propagate — no
        # subprocess spawn, no silent fallback to direct-Anthropic.
        #
        # 084 guard: the 078/084 self-hosted layer (_suppress_double_proxy in
        # launch.py) removes LITELLM_PROXY_BASE_URL from os.environ BEFORE
        # _launch is called so that the 084 translate shim (SelfHostedShim) can
        # own ANTHROPIC_BASE_URL without ClaudeCodeShim double-proxying on top.
        # ``self._proxy_env`` was loaded at __init__ time and remains non-empty
        # even after the suppression, so we re-check the live subprocess env
        # rather than relying on the stale snapshot.
        if self._proxy_env and subproc_env.get("LITELLM_PROXY_BASE_URL"):
            upstream_url = subproc_env.get("ANTHROPIC_BASE_URL", "").strip()
            upstream_token = subproc_env.get("ANTHROPIC_AUTH_TOKEN", "")
            self._shim = ClaudeCodeShim(
                upstream_url,
                upstream_token,
                capture_dir=get_settings().LITELLM_PROXY_CAPTURE_DIR,
            )
            try:
                loopback = await self._shim.start()
            except Exception as exc:
                log.error("claude_code shim start failed", error=str(exc))
                self._shim = None
                self._status = BackendStatus(
                    state="error",
                    error_reason=f"litellm shim failed to start: {exc}",
                )
                return
            subproc_env["ANTHROPIC_BASE_URL"] = loopback

        self._proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(self._stand.path) if self._stand else None,
            start_new_session=True,
            env=subproc_env,
        )
        # Write the prompt and close stdin so the CLI doesn't wait for more input.
        assert self._proc.stdin is not None
        self._proc.stdin.write(prompt_bytes)
        await self._proc.stdin.drain()
        self._proc.stdin.close()
        self._open_stdout_capture()
        self._last_event_at = time.monotonic()
        self._reader_task = asyncio.create_task(
            self._event_reader_loop(), name="claude-code-reader",
        )
        # Drain stderr concurrently so the OS pipe buffer never fills (which
        # would block claude's writes) and so we have diagnostic context for
        # non-zero exits.  Auth tokens are redacted before storage (FR-011).
        self._stderr_task = asyncio.create_task(
            self._stderr_reader_loop(), name="claude-code-stderr",
        )
        log.info("claude code started", pid=self._proc.pid, resume=bool(resume_session_id))

    @staticmethod
    def _redact(line: str) -> str:
        """Scrub Bearer tokens and known auth-token env values from stderr."""
        scrubbed = _BEARER_RE.sub(r"\1[REDACTED]", line)
        for env_var in ("ANTHROPIC_AUTH_TOKEN", "LITELLM_PROXY_AUTH_TOKEN", "ANTHROPIC_API_KEY"):
            token = os.environ.get(env_var, "").strip()
            if token and len(token) >= 8:
                scrubbed = scrubbed.replace(token, "[REDACTED]")
        return scrubbed

    async def _stderr_reader_loop(self) -> None:
        """Read stderr line-by-line into a bounded, token-redacted tail."""
        if self._proc is None or self._proc.stderr is None:
            return
        try:
            async for raw_line in self._proc.stderr:
                line = raw_line.decode(errors="replace").rstrip("\n")
                if not line:
                    continue
                self._stderr_tail.append(self._redact(line))
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover — best-effort drain
            log.warning("claude code stderr drain error", error=str(exc))

    def _stderr_tail_text(self) -> str:
        if not self._stderr_tail:
            return ""
        joined = "\n".join(self._stderr_tail)
        if len(joined) > _STDERR_ERROR_MAX_CHARS:
            joined = "..." + joined[-_STDERR_ERROR_MAX_CHARS:]
        return joined

    def _emit(self, type: BackendEventType, text: str, detail: str = "") -> None:
        self._event_buffer.append(BackendEvent(type=type, text=text[:_MAX_TEXT], detail=detail))

    def _open_stdout_capture(self) -> None:
        """Open a per-launch stdout-tee file if capture_dir is configured.

        Failures are non-fatal — diagnostic capture must never crash the
        backend itself.  ``self._stdout_capture`` is None when disabled or on
        open error, in which case ``_event_reader_loop`` skips the mirror.
        """
        self._stdout_capture = None
        capture_dir = (get_settings().LITELLM_PROXY_CAPTURE_DIR or "").strip()
        if not capture_dir or self._proc is None:
            return
        try:
            os.makedirs(capture_dir, exist_ok=True)
            # Captured stdout may contain raw model output including Bearer
            # tokens or any other secrets the CLI prints on error paths. Lock
            # the dir to 0700 so other users on a shared host can't read it.
            # (Debug-only feature per FR-011; operators enable explicitly.)
            try:
                os.chmod(capture_dir, 0o700)
            except OSError:  # pragma: no cover — best-effort hardening
                pass
            ts = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
            path = os.path.join(
                capture_dir, f"cli-stdout-{ts}-{self._proc.pid}.log",
            )
            self._stdout_capture = open(path, "wb")
        except OSError as exc:  # pragma: no cover — best-effort diagnostic
            log.warning("claude code stdout capture open failed", error=str(exc))
            self._stdout_capture = None

    def _close_stdout_capture(self) -> None:
        if self._stdout_capture is None:
            return
        try:
            self._stdout_capture.close()
        except OSError:  # pragma: no cover
            pass
        self._stdout_capture = None

    async def _event_reader_loop(self) -> None:
        """Read stream-json lines from stdout and update internal status.

        Uses an idle/no-progress timeout (``CLAUDE_CODE_IDLE_TIMEOUT``) so a
        CLI that receives a terminal upstream event but never emits its own
        ``result`` line can't wedge the backend.  Resets on every parsed event.
        Mirrors raw stdout bytes to ``self._stdout_capture`` when enabled.
        """
        if self._proc is None or self._proc.stdout is None:
            return
        stdout = self._proc.stdout
        idle_timeout = float(get_settings().CLAUDE_CODE_IDLE_TIMEOUT)
        # Anchor the watchdog at loop entry so the first chunk-read budget
        # is the full idle_timeout (not 0). Each parsed event below resets
        # _last_event_at, which _remaining() consults — making the timeout
        # a true per-event watchdog rather than a static chunk-read cap.
        self._last_event_at = time.monotonic()

        def _capture(chunk: bytes) -> None:
            if self._stdout_capture is None:
                return
            try:
                self._stdout_capture.write(chunk)
                self._stdout_capture.flush()
            except OSError:  # pragma: no cover
                self._stdout_capture = None

        def _remaining() -> float:
            elapsed = time.monotonic() - self._last_event_at
            return max(0.0, idle_timeout - elapsed)

        try:
            async for raw in iter_lines_chunked(
                stdout, timeout=_remaining, on_chunk=_capture,
            ):
                line = raw.decode(errors="replace").strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if self._handle_event(event):
                    self._last_event_at = time.monotonic()
                    self._events_seen += 1
                if self._status.state in ("done", "error"):
                    break
        except asyncio.TimeoutError:
            output_text = "".join(self._output_accumulator) or None
            log.warning(
                "claude code reader idle timeout",
                idle_seconds=idle_timeout,
                had_output=bool(output_text),
            )
            if output_text:
                self._status = BackendStatus(
                    state="done",
                    stop_reason="idle_timeout",
                    output=output_text,
                )
            else:
                self._status = BackendStatus(
                    state="error",
                    error_reason=(
                        f"claude CLI idle for {int(idle_timeout)}s "
                        "with no terminal event"
                    ),
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("claude code reader error", error=str(exc))
            self._status = BackendStatus(state="error", error_reason=str(exc))
        finally:
            self._close_stdout_capture()
            if self._status.state == "working" and self._proc is not None:
                try:
                    if self._proc.returncode is None:
                        await self._proc.wait()
                    rc = self._proc.returncode
                    if rc == 0:
                        # 509: rc=0 with zero parsed events means the CLI never
                        # ran the agent (silent exit). Report done only when
                        # the run actually produced stream-json events.
                        if self._events_seen == 0:
                            tail = self._stderr_tail_text()
                            reason = (
                                "subprocess_exit: claude exited with code 0 "
                                "without producing any events"
                            )
                            if tail:
                                reason = f"{reason}: {tail}"
                            self._status = BackendStatus(
                                state="error", error_reason=reason,
                            )
                        else:
                            self._status = BackendStatus(state="done")
                    else:
                        # Wait briefly for stderr drain to flush remaining
                        # lines, then surface the redacted tail so the failure
                        # is no longer opaque.
                        if self._stderr_task and not self._stderr_task.done():
                            try:
                                await asyncio.wait_for(self._stderr_task, timeout=1.0)
                            except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
                                pass
                        tail = self._stderr_tail_text()
                        reason = f"claude exited with code {rc}"
                        if tail:
                            reason = f"{reason}: {tail}"
                        self._status = BackendStatus(
                            state="error",
                            error_reason=reason,
                        )
                except Exception as exc:  # pragma: no cover
                    self._status = BackendStatus(state="error", error_reason=str(exc))

    def _handle_event(self, event: dict) -> bool:  # type: ignore[type-arg]
        """Translate a Claude Code stream-json event into BackendStatus + BackendEvent.

        Returns True when the event represents forward progress (the watchdog
        in _event_reader_loop will reset its idle clock). Returns False for
        events that are explicitly *not* progress — notably system:api_retry —
        so a stalled CLI stuck in retries can't keep the idle watchdog alive
        indefinitely.
        """
        event_type = event.get("type", "")

        if event_type == "system":
            subtype = event.get("subtype", "")
            if subtype == "init":
                # Capture session_id for --resume in relay_feedback
                session_id = event.get("session_id")
                if session_id:
                    self._session_id = session_id
                    log.debug("claude code session captured", session_id=session_id)
            elif subtype == "api_retry":
                # Upstream returned an error and the CLI is retrying. NOT
                # progress: surface as a backend error event so operators can
                # see it, increment the consecutive-retry counter, and trip
                # state=error after _API_RETRY_MAX consecutive retries.
                status_code = event.get("error_status")
                err = str(event.get("error", "")) or "api_retry"
                attempt = event.get("attempt")
                self._api_retry_count += 1
                detail = f"status={status_code} attempt={attempt}" if status_code else err
                self._emit(
                    BackendEventType.error,
                    f"upstream api_retry ({err})",
                    detail=detail,
                )
                if self._api_retry_count >= _API_RETRY_MAX:
                    reason = (
                        f"upstream returned {status_code or err} on "
                        f"{self._api_retry_count} consecutive retry attempts "
                        "without recovery"
                    )
                    self._status = BackendStatus(state="error", error_reason=reason)
                return False

        elif event_type == "assistant":
            message = event.get("message", {})
            for block in message.get("content", []):
                block_type = block.get("type", "")
                if block_type == "text":
                    full_text = str(block.get("text", ""))
                    if full_text:
                        self._output_accumulator.append(full_text)
                    text = full_text[:_MAX_TEXT]
                    self._status = BackendStatus(state="working", progress=text)
                    self._emit(BackendEventType.progress, text)
                elif block_type == "tool_use":
                    tool = str(block.get("name", "tool"))
                    self._emit(BackendEventType.tool_use, tool, detail=tool)
                elif block_type == "thinking":
                    thinking = str(block.get("thinking", ""))[:_MAX_TEXT]
                    self._emit(BackendEventType.thinking, thinking)

        elif event_type == "tool_result":
            tool_id = str(event.get("tool_use_id", ""))
            content = event.get("content", "")
            if isinstance(content, list):
                text = " ".join(
                    str(c.get("text", "")) for c in content if isinstance(c, dict)
                )[:_MAX_TEXT]
            else:
                text = str(content)[:_MAX_TEXT]
            self._emit(BackendEventType.tool_use, f"result: {text}", detail=tool_id)

        elif event_type == "result":
            subtype = event.get("subtype", "")
            if subtype == "success":
                # Also capture session_id from result event (available here too)
                session_id = event.get("session_id")
                if session_id:
                    self._session_id = session_id
                # The current CLI emits `total_cost_usd`; `cost_usd` is the
                # older shape. Reading only the old key dropped the dollar
                # figure from every real run's cost event.
                cost = event.get("cost_usd")
                if not isinstance(cost, int | float) or isinstance(cost, bool):
                    cost = event.get("total_cost_usd")
                if not isinstance(cost, int | float) or isinstance(cost, bool):
                    cost = None
                tokens = _result_tokens(event)
                cost_str = f" · ${cost:.4f}" if cost is not None else ""
                tokens_str = "usage unknown" if tokens is None else f"{tokens:,} tokens"
                self._emit(BackendEventType.cost, f"{tokens_str}{cost_str}")
                stop_reason = event.get("stop_reason") or None
                if stop_reason == "max_tokens":
                    self._emit(BackendEventType.error, "output token limit reached")
                    self._status = BackendStatus(
                        state="error",
                        stop_reason="max_tokens",
                        error_reason="Output token limit reached (max_tokens)",
                        tokens_processed=tokens,
                    )
                else:
                    output_text = "".join(self._output_accumulator)
                    if not output_text:
                        log.warning(
                            "claude_code.result.empty_output",
                            stop_reason=stop_reason,
                            tokens=tokens,
                        )
                    self._status = BackendStatus(
                        state="done",
                        stop_reason=stop_reason,
                        tokens_processed=tokens,
                        output=output_text or None,
                    )
            elif subtype in ("error", "interrupted"):
                reason = event.get("error", subtype)
                self._emit(BackendEventType.error, str(reason)[:_MAX_TEXT])
                self._status = BackendStatus(state="error", error_reason=str(reason))

        # A progress event arrived (anything except the early-return api_retry
        # branch above). Clear the consecutive-retry counter so a single
        # transient 504 followed by recovery doesn't trip the cap later.
        self._api_retry_count = 0
        return True


# 305: every token field a Claude Code `result` event can carry. The two cache
# fields are input tokens the model actually processed, and on a real turn they
# are nearly all of the input: a cache-heavy blueprint step reports
# input_tokens=4 against cache_read_input_tokens=88_000. Summing only
# input+output under-reports such a turn by orders of magnitude.
_RESULT_TOKEN_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
)


def _is_token_count(value: object) -> bool:
    """An int that is not a bool. ``isinstance(True, int)`` is True in Python,
    and a stray boolean must not be summed as 1 token."""
    return isinstance(value, int) and not isinstance(value, bool)


def _result_tokens(event: dict) -> int | None:  # type: ignore[type-arg]
    """Total tokens for a `result` event, or ``None`` when it reports no usage.

    Absence of usage is UNKNOWN, not zero. A proxy that strips `usage` from the
    upstream response leaves the CLI with nothing to report, and a 0 there is
    indistinguishable from a measured zero downstream: it sums into the
    benchmark artifact, prices at $0.00, and leaves `cost_component_missing`
    false, so an unmeasured run reads as a free one. Present-but-zero usage is
    still evidence and is preserved as 0.

    Each field is read from the nested ``usage`` block (the current CLI shape)
    and falls back to the event top level (the older shape). That order matters:
    reading the top level first lets a legacy ``input_tokens: 0`` shadow a
    populated ``usage`` and silently discard the real count.
    """
    usage = event.get("usage")
    if not isinstance(usage, dict):
        usage = {}
    total = 0
    measured = False
    for field in _RESULT_TOKEN_FIELDS:
        value = usage.get(field)
        if not _is_token_count(value):
            value = event.get(field)
        if _is_token_count(value):
            total += value
            measured = True
    return total if measured else None


def _build_task_prompt(
    score: Score, *, stand_path: Path | None = None,
) -> str:
    """Construct the task description sent to Claude Code as the initial prompt."""
    # FR-016: persona_instructions is routed via `--append-system-prompt` in
    # _launch(), not embedded in the prompt body, to avoid double-delivery.
    parts: list[str] = [f"# Task: {score.title}", ""]
    if score.description:
        parts += [score.description, ""]
    parts += card_docs_prompt_section(score, stand_path)
    parts += qa_findings_prompt_section(score)
    parts += repair_mandate_prompt_section(score)
    parts += scanner_findings_prompt_section(score)
    parts += scope_prompt_section(score, "claude_code")
    parts += brief_prompt_sections(score)
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

    # PR diff under review (injected for review roles so the model never
    # concludes "no code changes were supplied").
    if score.pr_diff:
        parts += [
            "", "## PR Diff Under Review", "",
            "The unified diff below is the complete set of changes on this PR. "
            "Review it directly — do NOT report that no changes were supplied.",
            "", "```diff", score.pr_diff.rstrip("\n"), "```", "",
        ]

    # Relay feedback (human review comments from previous cycle)
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
                    f"{_d.get('reason', '')}",
                )

    if score.relay_feedback:
        parts += [
            "", "## Human Feedback (address ALL of these issues)", "",
            "IMPORTANT: These comments may only tag a few examples. Search the entire "
            "codebase for ALL similar occurrences of the same pattern and fix them all.",
            "",
        ]
        for item in score.relay_feedback:
            if isinstance(item, dict):
                body = item.get("body", "")
                if body:
                    parts.append(f"- {body}")
                inline = item.get("comments", [])
                if isinstance(inline, list):
                    for c in inline:
                        if isinstance(c, dict):
                            c_body = c.get("body", "")
                            c_path = c.get("path", "")
                            c_line = c.get("line")
                            if c_body:
                                loc = f"`{c_path}:{c_line}`" if c_path and c_line else (f"`{c_path}`" if c_path else "")
                                parts.append(f"  - {loc} — {c_body}" if loc else f"  - {c_body}")
            elif isinstance(item, str):
                parts.append(f"- {item}")

    parts += ["", "---"]
    if score.role == DIAGNOSTIC_ROLE:
        parts += [
            "This is a one-off diagnostic/benchmark task. Use any tools at your "
            "disposal to complete it. You do NOT need to commit, push, or open a "
            "pull request — just perform the task and report what you did.",
        ]
    elif score.role in _JSON_ONLY_ROLES:
        parts += [
            "Return ONLY a valid JSON object for your role contract.",
            "Do not include markdown, prose, or code fences.",
        ]
        if score.role == "qa":
            parts += [
                "QA contract reminder: include a non-empty `verification_steps` array.",
                "For bug fixes, label steps as verification (not reproduction) unless explicitly asked.",
                "Set `visual_validation_required=true` for UI/UX/visual changes and capture at least one artifact in `visual_evidence` for those tasks.",
                "Include `visual_evidence` entries when screenshots/GIFs/videos/artifacts are available.",
                "Include exact capture attempts in `visual_capture_commands` (commands/scripts you ran).",
                LOCAL_CAPTURE_RULE,
                "If visual evidence cannot be captured, include `demo_setup_steps` and `visual_capture_blockers` with concrete details.",
                (
                    "Screenshot uploads: after capturing a screenshot to disk, run "
                    "`performer-upload-screenshot <path>` (available on $PATH). It "
                    "uploads the file to GitHub's user-attachments CDN and prints "
                    "an https://github.com/user-attachments/assets/... URL on stdout. "
                    "Use that URL as `path_or_url` in your `visual_evidence` entry so "
                    "the image renders inline in the PR comment. If the upload command "
                    "is unavailable or fails, keep the local path — the performer "
                    "wrapper will retry the upload before posting."
                ),
            ]
    else:
        parts += [
            "Complete the task above. Commit your changes with a clear, descriptive commit message.",
            "Do not push or open a pull request — this will be handled automatically after you finish.",
        ]
    return "\n".join(parts)
