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
        self._model: str | None = None  # 037: per-role model selection
        self._max_tokens: int | None = None  # 055: output token cap

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
        self._stand = stand
        self._git_env = stand.git_env
        self._cache_env = stand.cache_env
        self._tool_env = score.tool_env
        self._model = model
        self._max_tokens = max_tokens
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
        if self._max_tokens is not None:
            args += ["--max-tokens", str(self._max_tokens)]
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
            env={**os.environ, **self._cache_env, **self._git_env, **self._tool_env},
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
                    self._status = BackendStatus(
                        state="done",
                        stop_reason=stop_reason,
                        tokens_processed=tokens,
                    )
            elif subtype in ("error", "interrupted"):
                reason = event.get("error", subtype)
                self._emit(BackendEventType.error, str(reason)[:_MAX_TEXT])
                self._status = BackendStatus(state="error", error_reason=str(reason))

        elif event_type == "system":  # pragma: no cover
            pass  # init handled above; other system events are no-ops


def _build_task_prompt(score: Score) -> str:
    """Construct the task description sent to Claude Code as the initial prompt."""
    parts = []

    # Persona instructions (role-specific behavior)
    if score.persona_instructions:
        parts += ["## Role Instructions", "", score.persona_instructions, ""]

    parts += [f"# Task: {score.title}", ""]
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

    # Relay feedback (human review comments from previous cycle)
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
    if score.role in _JSON_ONLY_ROLES:
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
