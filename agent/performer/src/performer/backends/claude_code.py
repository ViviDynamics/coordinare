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
import signal
from collections import deque

import psutil
import structlog

from performer.backends.base import BackendStatus
from performer.models import BackendEvent, BackendEventType, Score, Stand

log = structlog.get_logger(__name__)

_MAX_TEXT = 200


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
        self._model: str | None = None  # 037: per-role model selection

    # ------------------------------------------------------------------
    # BackendAdapter protocol
    # ------------------------------------------------------------------

    async def start(self, stand: Stand, score: Score, *, model: str | None = None) -> None:
        """Build the prompt and launch ``claude --print --output-format stream-json``."""
        self._stand = stand
        self._git_env = stand.git_env
        self._model = model
        prompt = _build_task_prompt(score)
        await self._launch(prompt)

    def get_status(self) -> BackendStatus:
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
        args = [
            "claude",
            "--print",
            "--output-format", "stream-json",
            "--include-partial-messages",
        ]
        if self._model:
            args += ["--model", self._model]
        if resume_session_id:
            args += ["--resume", resume_session_id]
        args += ["-p", prompt]

        self._proc = await asyncio.create_subprocess_exec(
            *args,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(self._stand.path) if self._stand else None,
            start_new_session=True,
            env={**os.environ, **self._git_env},
        )
        self._reader_task = asyncio.create_task(
            self._event_reader_loop(), name="claude-code-reader"
        )
        log.info("claude code started", pid=self._proc.pid, resume=bool(resume_session_id))

    def _emit(self, type: BackendEventType, text: str, detail: str = "") -> None:
        self._event_buffer.append(BackendEvent(type=type, text=text[:_MAX_TEXT], detail=detail))

    async def _event_reader_loop(self) -> None:
        """Read stream-json lines from stdout and update internal status."""
        if self._proc is None or self._proc.stdout is None:
            return
        try:
            async for raw_line in self._proc.stdout:
                line = raw_line.decode(errors="replace").strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                self._handle_event(event)
                if self._status.state in ("done", "error"):
                    break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("claude code reader error", error=str(exc))
            self._status = BackendStatus(state="error", error_reason=str(exc))
        finally:
            if self._status.state == "working" and self._proc is not None:
                try:
                    if self._proc.returncode is None:
                        await self._proc.wait()
                    rc = self._proc.returncode
                    if rc == 0:
                        self._status = BackendStatus(state="done")
                    else:
                        self._status = BackendStatus(
                            state="error",
                            error_reason=f"claude exited with code {rc}",
                        )
                except Exception as exc:  # pragma: no cover
                    self._status = BackendStatus(state="error", error_reason=str(exc))

    def _handle_event(self, event: dict) -> None:  # type: ignore[type-arg]
        """Translate a Claude Code stream-json event into BackendStatus + BackendEvent."""
        event_type = event.get("type", "")

        if event_type == "system":
            subtype = event.get("subtype", "")
            if subtype == "init":
                # Capture session_id for --resume in relay_feedback
                session_id = event.get("session_id")
                if session_id:
                    self._session_id = session_id
                    log.debug("claude code session captured", session_id=session_id)

        elif event_type == "assistant":
            message = event.get("message", {})
            for block in message.get("content", []):
                block_type = block.get("type", "")
                if block_type == "text":
                    text = str(block.get("text", ""))[:_MAX_TEXT]
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
                cost = event.get("cost_usd")
                usage = event.get("usage", {})
                input_t = event.get("input_tokens") or usage.get("input_tokens", 0)
                output_t = event.get("output_tokens") or usage.get("output_tokens", 0)
                tokens = (input_t or 0) + (output_t or 0)
                cost_str = f" · ${cost:.4f}" if cost is not None else ""
                self._emit(BackendEventType.cost, f"{tokens:,} tokens{cost_str}")
                self._status = BackendStatus(state="done", tokens_processed=tokens)
            elif subtype in ("error", "interrupted"):
                reason = event.get("error", subtype)
                self._emit(BackendEventType.error, str(reason)[:_MAX_TEXT])
                self._status = BackendStatus(state="error", error_reason=str(reason))

        elif event_type == "system":  # pragma: no cover
            pass  # init handled above; other system events are no-ops


def _build_task_prompt(score: Score) -> str:
    """Construct the task description sent to Claude Code as the initial prompt."""
    parts = [f"# Task: {score.title}", ""]
    if score.description:
        parts += [score.description, ""]
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
    parts += [
        "",
        "---",
        "Complete the task above. Commit your changes with a clear, descriptive commit message.",
        "Do not push or open a pull request — this will be handled automatically after you finish.",
    ]
    return "\n".join(parts)
