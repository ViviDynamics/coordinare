"""PiBackend — integrates the Pi coding CLI (https://pi.dev) via its
non-interactive JSON event-stream mode.

Spec 077. Pi is driven OpenAI-compatibly through the LiteLLM proxy (the same
"one shared model" invariant as codex/hermes), NOT Pi-hosted models:

    pi -p --mode json [--provider <name>] [--model <id>] "<task>"

``--mode json`` emits one JSON object per line (see pi.dev/docs/latest/json):
session header → agent_start → turn_start → message_start/update/end →
tool_execution_* → turn_end → agent_end (carries the final ``messages`` array
and signals completion).

Custom provider routing: when ``PI_PROVIDER_BASE_URL`` is set we register an
OpenAI-compatible provider pointing at the proxy. Pi accepts custom providers
via ``models.json`` / an extension calling ``pi.registerProvider(...)`` with the
shared ``$ENV_VAR`` value syntax for the API key.

Lifecycle:
    start()          → write provider config, launch ``pi -p --mode json ...``,
                       spawn a background stdout JSON-lines reader
    get_status()     → non-blocking current BackendStatus
    drain_events()   → return and clear buffered BackendEvent list
    relay_feedback() → re-invoke pi with the feedback as a fresh prompt
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

from performer.backends._card_docs import card_docs_prompt_section
from performer.backends.base import BackendStatus
from performer.io_utils import iter_lines_chunked
from performer.models import DIAGNOSTIC_ROLE, BackendEvent, BackendEventType, Score, Stand

log = structlog.get_logger(__name__)

_MAX_TEXT = 200


class PiBackend:
    """Backend adapter that drives the Pi CLI in JSON event-stream mode."""

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
        self._saw_terminal: bool = False

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
        """Write provider config and launch ``pi -p --mode json``."""
        env = {**os.environ, **stand.cache_env, **stand.git_env, **score.tool_env}
        self._cwd = str(stand.path)
        self._model = model
        self._env = env

        cmd: list[str] = ["pi", "-p", "--mode", "json"]

        # Custom OpenAI-compatible provider override → LiteLLM (presence of
        # PI_PROVIDER_BASE_URL activates it; analogous to CODEX_PROVIDER_BASE_URL).
        # Each performer runs in its own ephemeral container, so the per-user
        # config dir (~/.pi/agent) is already isolated — no extra home redirect.
        provider_base_url = env.get("PI_PROVIDER_BASE_URL", "")
        if provider_base_url:
            provider_name = env.get("PI_PROVIDER_NAME", "litellm")
            provider_env_key = env.get("PI_PROVIDER_ENV_KEY", "OPENAI_API_KEY")
            home = env.get("HOME") or os.path.expanduser("~")
            agent_dir = pathlib.Path(home) / ".pi" / "agent"
            self._write_provider_config(
                agent_dir, provider_name, provider_base_url, provider_env_key, model
            )
            self._provider = provider_name
            cmd += ["--provider", provider_name]
        if model:
            cmd += ["--model", model]

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
        self._reader_task = asyncio.create_task(self._read_loop(), name="pi-reader")
        log.info("pi started", model=model, provider=self._provider)

    def get_status(self) -> BackendStatus:
        return self._status

    def drain_events(self) -> list[BackendEvent]:
        events = list(self._event_buffer)
        self._event_buffer.clear()
        return events

    async def relay_feedback(self, feedback: str) -> None:
        """Pi ``-p`` is one-shot; deliver feedback as a fresh print-mode run."""
        if self._proc is not None and self._proc.returncode is None:
            await self.stop()
        cmd: list[str] = ["pi", "-p", "--mode", "json"]
        if self._provider:
            cmd += ["--provider", self._provider]
        if self._model:
            cmd += ["--model", self._model]
        cmd.append(feedback)
        self._saw_terminal = False
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
        self._reader_task = asyncio.create_task(self._read_loop(), name="pi-reader")

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
        log.info("pi stopped")

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
        """Register a custom OpenAI-compatible provider for Pi → LiteLLM.

        Writes ``~/.pi/agent/models.json`` (Pi's default registry path, verified
        against pi 0.73.1 in the 077 POC). The ``compat`` block is REQUIRED for
        OpenAI-compatible servers like LiteLLM→qwen that don't understand the
        ``developer`` role or ``reasoning_effort`` — without it the call errors.
        ``apiKey`` uses Pi's ``${ENV_VAR}`` interpolation.
        """
        agent_dir.mkdir(parents=True, exist_ok=True)
        config: dict[str, Any] = {
            "providers": {
                name: {
                    "baseUrl": base_url,
                    "api": "openai-completions",
                    "apiKey": f"${{{env_key}}}",
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
        """Parse Pi's ``--mode json`` event lines into status + events."""
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
        except Exception as exc:  # noqa: BLE001 — defensive; surface as error
            log.warning("pi reader error", error=str(exc))
            if self._status.state == "working":
                self._status = BackendStatus(state="error", error_reason=str(exc))
            return

        # Stream ended. If Pi never emitted a terminal (agent_end) event, infer
        # the outcome from the exit code + accumulated text.
        try:
            await asyncio.wait_for(self._proc.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            pass
        if not self._saw_terminal and self._status.state == "working":
            rc = self._proc.returncode
            output = "".join(self._output_accumulator) or None
            if rc == 0:
                self._status = BackendStatus(state="done", output=output)
            else:
                tail = " | ".join(list(self._log_buffer)[-10:])
                self._status = BackendStatus(
                    state="error",
                    error_reason=f"pi exited rc={rc}: {tail}" if tail else f"pi exited rc={rc}",
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
            self._saw_terminal = True
            output = _extract_final_assistant_text(event.get("messages", []))
            if not output:
                output = "".join(self._output_accumulator)
            self._status = BackendStatus(state="done", output=output or None)

        elif etype in ("error", "auto_retry_start"):
            msg = ""
            err = event.get("error")
            if isinstance(err, dict):
                msg = str(err.get("message", ""))
            msg = msg or str(event.get("message", "")) or etype
            self._emit(BackendEventType.error, msg[:_MAX_TEXT])
            # Non-retry errors are terminal.
            if etype == "error" and not event.get("willRetry", False):
                self._saw_terminal = True
                self._status = BackendStatus(state="error", error_reason=msg)

    def _emit(self, type: BackendEventType, text: str, detail: str = "") -> None:
        self._event_buffer.append(BackendEvent(type=type, text=text[:_MAX_TEXT], detail=detail))


def _extract_final_assistant_text(messages: Any) -> str:
    """Return the last assistant message's text from Pi's ``messages`` array."""
    if not isinstance(messages, list):
        return ""
    for msg in reversed(messages):
        if not isinstance(msg, dict) or msg.get("role") != "assistant":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for part in content:
                if isinstance(part, dict) and part.get("type") in ("text", None):
                    parts.append(str(part.get("text", "")))
                elif isinstance(part, str):
                    parts.append(part)
            if parts:
                return "".join(parts)
    return ""


def _build_task_prompt(score: Score, *, stand_path: pathlib.Path | None = None) -> str:
    """Construct the task prompt for Pi.

    Pi has no separate developer/system-prompt slot in print mode, so the
    persona contract is delivered at the head of the prompt body.
    """
    parts: list[str] = []
    if score.persona_instructions:
        parts += [score.persona_instructions, "", "---", ""]
    parts += [f"# Task: {score.title}", ""]
    if score.description:
        parts += [score.description, ""]
    parts += card_docs_prompt_section(score, stand_path)
    if score.acceptance_criteria:
        parts += ["## Acceptance Criteria", ""]
        parts.extend(f"- {c}" for c in score.acceptance_criteria)
    if score.pr_diff:
        parts += [
            "", "## PR Diff Under Review", "",
            "The unified diff below is the complete set of changes on this PR. "
            "Review it directly — do NOT report that no changes were supplied.",
            "", "```diff", score.pr_diff.rstrip("\n"), "```", "",
        ]
    if score.relay_feedback:
        parts += ["", "## Human Feedback (address ALL of these)", ""]
        for item in score.relay_feedback:
            if isinstance(item, dict):
                body = item.get("body", "")
                if body:
                    parts.append(f"- {body}")
            elif isinstance(item, str):
                parts.append(f"- {item}")
    if score.role == DIAGNOSTIC_ROLE:
        parts += [
            "",
            "---",
            "This is a one-off diagnostic/benchmark task. Use any tools at your "
            "disposal to complete it. You do NOT need to commit, push, or open a "
            "pull request — just perform the task and report what you did.",
        ]
    else:
        parts += [
            "",
            "---",
            "Complete the task above. Commit your changes with a clear, descriptive commit message.",
            "Do not push or open a pull request — this will be handled automatically after you finish.",
        ]
    return "\n".join(parts)
