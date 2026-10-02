"""PrimeAgentBackend — integrates the Prime Agent coding CLI
(https://github.com/PrimeIntellect-ai/prime-agent) via its non-interactive
JSON event-stream mode.

Prime Agent is MIT-licensed and driven OpenAI-compatibly through the LiteLLM
proxy (the same "one shared model" invariant as codex/hermes/pi), NOT through
Prime Intellect-hosted models:

    prime-agent -p --mode json [--provider <name>] [--model <id>]
        [--thinking <level>] "<task>"

``--mode json`` emits one JSON object per line (see the project's
``docs/json.md``): session header (``{"type":"session","version":3,...}``) →
agent_start → turn_start → message_start/update/end → tool_execution_* →
turn_end → agent_end (carries the final ``messages`` array and signals
completion).

Relationship to ``pi.py``: the two CLIs share an event vocabulary and a
print-mode shape, so the task-prompt builder and final-text extractor are
imported from there rather than duplicated — the same arrangement
``openclaw`` has with ``opencode``. The differences Prime Agent does have are
all handled here:

* config directory is ``~/.prime/agent`` (Pi uses ``~/.pi/agent``), and is
  redirectable via ``PRIME_AGENT_CODING_AGENT_DIR``;
* ``models.json`` takes the API key as a bare environment variable NAME, where
  Pi takes ``${VAR}`` interpolation syntax;
* ``--thinking`` exposes a reasoning-depth control, so the ``effort`` argument
  is honored instead of ignored;
* the stream carries ``auto_retry_*`` and ``compaction_*`` events that Pi's
  does not, and ``agent_end`` is forwarded BEFORE the CLI decides whether to
  retry a failed model turn — so a terminal verdict waits on the last
  assistant message's ``stopReason`` rather than on ``agent_end`` alone.

Lifecycle:
    start()          → write provider config, launch ``prime-agent -p --mode
                       json ...``, spawn a background stdout JSON-lines reader
    get_status()     → non-blocking current BackendStatus
    drain_events()   → return and clear buffered BackendEvent list
    relay_feedback() → re-invoke prime-agent with the feedback as a fresh prompt
    stop()           → kill the process group
"""
from __future__ import annotations

import asyncio
import json
import os
import pathlib
import signal
from collections import deque
from typing import Any

import psutil
import structlog

from performer.backends._env_policy import build_subprocess_env
from performer.backends.base import BackendStatus
from performer.backends.pi import _build_task_prompt, _extract_final_assistant_text
from performer.io_utils import iter_lines_chunked
from performer.models import BackendEvent, BackendEventType, Score, Stand

log = structlog.get_logger(__name__)

_MAX_TEXT = 200

# Reasoning-depth levels accepted by ``prime-agent --thinking``. Coordinare's
# ``effort`` vocabulary overlaps but is not identical, so an unrecognised value
# is dropped with a warning rather than passed through — an invalid level makes
# the CLI exit non-zero before it does any work.
_THINKING_LEVELS: frozenset[str] = frozenset(
    {"off", "minimal", "low", "medium", "high", "xhigh", "max"}
)

# ``stopReason`` values Prime Agent puts on an assistant message that did not
# complete. Everything else is a normal turn end.
_FAILED_STOP_REASONS: frozenset[str] = frozenset({"error", "aborted"})


class PrimeAgentBackend:
    """Backend adapter that drives the Prime Agent CLI in JSON event-stream mode."""

    def __init__(self) -> None:
        self._proc: asyncio.subprocess.Process | None = None
        self._status: BackendStatus = BackendStatus(state="working")
        self._event_buffer: deque[BackendEvent] = deque(maxlen=200)
        self._log_buffer: deque[str] = deque(maxlen=200)
        self._output_accumulator: list[str] = []
        self._reader_task: asyncio.Task[None] | None = None
        self._env: dict[str, str] = {}
        self._cwd: str = "."
        self._model: str | None = None
        self._provider: str | None = None
        self._thinking: str | None = None
        self._saw_terminal: bool = False
        # Reason from an agent_end whose last assistant message failed/aborted,
        # held while the CLI decides whether to retry (see _handle_event).
        self._pending_failure: str | None = None

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
        """Write provider config and launch ``prime-agent -p --mode json``."""
        env = build_subprocess_env(
            cache_env=stand.cache_env,
            git_env=stand.git_env,
            tool_env=score.tool_env,
        )
        self._cwd = str(stand.path)
        self._model = model
        self._env = env
        self._thinking = _normalize_thinking(effort)

        cmd: list[str] = ["prime-agent", "-p", "--mode", "json"]

        # Custom OpenAI-compatible provider override → LiteLLM (presence of
        # PRIME_AGENT_PROVIDER_BASE_URL activates it; analogous to
        # PI_PROVIDER_BASE_URL / CODEX_PROVIDER_BASE_URL). Each performer runs
        # in its own ephemeral container, so the per-user config dir is already
        # isolated — no extra home redirect.
        provider_base_url = env.get("PRIME_AGENT_PROVIDER_BASE_URL", "")
        if provider_base_url:
            provider_name = env.get("PRIME_AGENT_PROVIDER_NAME", "litellm")
            provider_env_key = env.get("PRIME_AGENT_PROVIDER_ENV_KEY", "OPENAI_API_KEY")
            self._write_provider_config(
                _agent_dir(env), provider_name, provider_base_url, provider_env_key, model
            )
            self._provider = provider_name
            cmd += ["--provider", provider_name]
        if model:
            cmd += ["--model", model]
        if self._thinking:
            cmd += ["--thinking", self._thinking]

        task_text = _build_task_prompt(score, stand_path=pathlib.Path(stand.path))
        cmd.append(task_text)

        self._proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=self._cwd,
            start_new_session=True,
            env=env,
        )
        self._reader_task = asyncio.create_task(
            self._read_loop(), name="prime-agent-reader"
        )
        log.info(
            "prime-agent started",
            model=model,
            provider=self._provider,
            thinking=self._thinking,
        )

    def get_status(self) -> BackendStatus:
        return self._status

    def drain_events(self) -> list[BackendEvent]:
        events = list(self._event_buffer)
        self._event_buffer.clear()
        return events

    async def relay_feedback(self, feedback: str) -> None:
        """``-p`` is one-shot; deliver feedback as a fresh print-mode run."""
        if self._proc is not None and self._proc.returncode is None:
            await self.stop()
        cmd: list[str] = ["prime-agent", "-p", "--mode", "json"]
        if self._provider:
            cmd += ["--provider", self._provider]
        if self._model:
            cmd += ["--model", self._model]
        if self._thinking:
            cmd += ["--thinking", self._thinking]
        cmd.append(feedback)
        self._saw_terminal = False
        self._pending_failure = None
        self._output_accumulator.clear()
        self._status = BackendStatus(state="working")
        self._proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=self._cwd,
            start_new_session=True,
            env=self._env,
        )
        self._reader_task = asyncio.create_task(
            self._read_loop(), name="prime-agent-reader"
        )

    async def stop(self) -> None:
        """Cancel the reader and kill the process group."""
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
            try:
                await self._reader_task
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
        log.info("prime-agent stopped")

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _write_provider_config(
        self,
        agent_dir: pathlib.Path,
        name: str,
        base_url: str,
        env_key: str,
        model: str | None,
    ) -> None:
        """Register a custom OpenAI-compatible provider for Prime Agent → LiteLLM.

        Writes ``<agent_dir>/models.json``. The ``compat`` block is REQUIRED for
        OpenAI-compatible servers like LiteLLM→qwen that don't understand the
        ``developer`` role or ``reasoning_effort`` — without it the call errors.

        ``apiKey`` carries the environment variable NAME verbatim. This is the
        one place the config diverges from Pi's otherwise-identical schema: Pi
        interpolates ``${VAR}``, Prime Agent resolves a bare name at request
        time, so wrapping it in ``${...}`` here would look up an env var whose
        name literally includes the braces.
        """
        agent_dir.mkdir(parents=True, exist_ok=True)
        config: dict[str, Any] = {
            "providers": {
                name: {
                    "baseUrl": base_url,
                    "api": "openai-completions",
                    "apiKey": env_key,
                    "compat": {
                        "supportsDeveloperRole": False,
                        "supportsReasoningEffort": False,
                    },
                    "models": (
                        [{"id": model, "name": model, "contextWindow": 128000, "maxTokens": 8192}]
                        if model
                        else []
                    ),
                }
            }
        }
        path = agent_dir / "models.json"
        path.write_text(json.dumps(config, indent=2))
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass

    async def _read_loop(self) -> None:
        """Parse the ``--mode json`` event lines into status + events."""
        if self._proc is None or self._proc.stdout is None:
            return
        try:
            async for raw in iter_lines_chunked(self._proc.stdout):
                line = raw.decode(errors="replace").strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    self._log_buffer.append(line[:_MAX_TEXT])
                    continue
                if isinstance(event, dict):
                    self._handle_event(event)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # defensive; surface as error
            log.warning("prime-agent reader error", error=str(exc))
            if self._status.state == "working":
                self._status = BackendStatus(state="error", error_reason=str(exc))
            return

        # Stream ended. If the CLI never emitted a terminal (agent_end) event,
        # infer the outcome from the exit code + accumulated text.
        try:
            await asyncio.wait_for(self._proc.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            pass
        if not self._saw_terminal and self._status.state == "working":
            rc = self._proc.returncode
            output = "".join(self._output_accumulator) or None
            if self._pending_failure is not None:
                # A failed/aborted assistant turn that the CLI never retried to
                # a good outcome. The run ended on that failure regardless of
                # the exit code, so report it rather than the rc fallback.
                self._status = BackendStatus(
                    state="error", error_reason=self._pending_failure
                )
            elif rc == 0:
                self._status = BackendStatus(state="done", output=output)
            else:
                tail = " | ".join(list(self._log_buffer)[-10:])
                self._status = BackendStatus(
                    state="error",
                    error_reason=(
                        f"prime-agent exited rc={rc}: {tail}"
                        if tail
                        else f"prime-agent exited rc={rc}"
                    ),
                )

    def _handle_event(self, event: dict[str, Any]) -> None:
        etype = str(event.get("type", ""))

        if etype == "message_update":
            ame = event.get("assistantMessageEvent") or {}
            if isinstance(ame, dict) and ame.get("type") == "text_delta":
                delta = str(ame.get("delta", ""))
                if delta:
                    self._output_accumulator.append(delta)
                    self._status = BackendStatus(state="working", progress=delta[:_MAX_TEXT])
                    self._emit(BackendEventType.progress, delta)

        elif etype == "tool_execution_start":
            tool = str(event.get("toolName", "tool"))[:_MAX_TEXT]
            self._emit(BackendEventType.tool_use, tool, detail=tool)

        elif etype == "agent_end":
            # agent_end does NOT mean the run succeeded. Prime Agent forwards
            # the event to consumers *before* it decides whether to retry a
            # failed model turn (agent-session.ts emits it, then inspects the
            # last assistant message and may start a retry). Treating every
            # agent_end as completion lets the performer finalize — and stop —
            # a CLI that is still retrying.
            failure = _failed_assistant_reason(event.get("messages", []))
            if failure is not None:
                # Hold the failure instead of terminating: a retry that lands
                # clears it (auto_retry_end success / a later clean agent_end),
                # and EOF without that recovery turns it into the error below.
                self._pending_failure = failure
                self._status = BackendStatus(state="working", progress=failure[:_MAX_TEXT])
                self._emit(BackendEventType.error, failure[:_MAX_TEXT])
                return
            output = _extract_final_assistant_text(event.get("messages", []))
            if not output:
                output = "".join(self._output_accumulator)
            self._pending_failure = None
            self._saw_terminal = True
            self._status = BackendStatus(state="done", output=output or None)

        elif etype == "auto_retry_start":
            # A transient upstream failure the CLI is retrying itself. Surface
            # it for visibility but leave the state as working — the run has
            # not failed until the retries are exhausted.
            # Coalesce BEFORE stringifying: an explicit JSON null would render
            # as the truthy literal "None" and defeat the fallback.
            msg = str(event.get("errorMessage") or "auto retry")
            attempt = event.get("attempt")
            max_attempts = event.get("maxAttempts")
            self._emit(
                BackendEventType.error,
                f"retry {attempt}/{max_attempts}: {msg}"[:_MAX_TEXT],
            )

        elif etype == "auto_retry_end":
            # Retries exhausted without success is terminal — without this the
            # run would hang as "working" until the stream closed.
            if not event.get("success", False):
                final = str(event.get("finalError") or "auto retry exhausted")
                self._saw_terminal = True
                self._status = BackendStatus(state="error", error_reason=final)
                self._emit(BackendEventType.error, final[:_MAX_TEXT])
            else:
                # The CLI recovered — drop the failure held from the agent_end
                # that preceded the retry, and keep the run working.
                self._pending_failure = None

        elif etype == "compaction_end":
            # Context compaction is routine, but an aborted compaction that the
            # CLI will not retry leaves the session unable to continue.
            if event.get("aborted", False) and not event.get("willRetry", False):
                msg = str(event.get("errorMessage") or "compaction aborted")
                self._saw_terminal = True
                self._status = BackendStatus(state="error", error_reason=msg)
                self._emit(BackendEventType.error, msg[:_MAX_TEXT])

        elif etype == "error":
            msg = ""
            err = event.get("error")
            if isinstance(err, dict):
                msg = str(err.get("message") or "")
            msg = msg or str(event.get("message") or "") or etype
            self._emit(BackendEventType.error, msg[:_MAX_TEXT])
            if not event.get("willRetry", False):
                self._saw_terminal = True
                self._status = BackendStatus(state="error", error_reason=msg)

    def _emit(self, type: BackendEventType, text: str, detail: str = "") -> None:
        self._event_buffer.append(BackendEvent(type=type, text=text[:_MAX_TEXT], detail=detail))


def _failed_assistant_reason(messages: Any) -> str | None:
    """Return a reason when the run's last assistant message did not succeed.

    Prime Agent marks an unsuccessful model turn with ``stopReason`` set to
    ``"error"`` or ``"aborted"`` — the two values agent-session.ts itself
    treats as "not a completed turn" — and carries the detail in
    ``errorMessage``. Returns ``None`` for a normal completion, and for a
    ``messages`` array with no assistant message at all (nothing failed).
    """
    if not isinstance(messages, list):
        return None
    for msg in reversed(messages):
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        stop = str(msg.get("stopReason") or "")
        if stop not in _FAILED_STOP_REASONS:
            return None
        # Coalesce BEFORE stringifying: an explicit JSON null would render as
        # the truthy literal "None" and defeat the fallback.
        return str(msg.get("errorMessage") or f"assistant {stop}")
    return None


def _agent_dir(env: dict[str, str]) -> pathlib.Path:
    """Resolve Prime Agent's config directory.

    ``PRIME_AGENT_CODING_AGENT_DIR`` is the CLI's own override; honoring it
    keeps the provider config and the CLI pointed at the same place when a
    deployment redirects it.
    """
    override = env.get("PRIME_AGENT_CODING_AGENT_DIR", "")
    if override:
        return pathlib.Path(override)
    home = env.get("HOME") or os.path.expanduser("~")
    return pathlib.Path(home) / ".prime" / "agent"


def _normalize_thinking(effort: str | None) -> str | None:
    """Map coordinare's ``effort`` onto ``prime-agent --thinking``.

    Returns ``None`` when there is nothing safe to pass, leaving the CLI on its
    own default. An unrecognised level is dropped rather than forwarded — the
    CLI rejects an invalid value and exits before doing any work, which would
    turn a cosmetic config mismatch into a failed card.
    """
    if not effort:
        return None
    level = effort.strip().lower()
    if level in _THINKING_LEVELS:
        return level
    log.warning("prime-agent: unsupported thinking level ignored", effort=effort)
    return None
