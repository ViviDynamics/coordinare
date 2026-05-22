"""OpenCodeAdapter — integrates OpenCode-compatible CLIs via HTTP server API.

Lifecycle:
    start()          → launches ``<executable> serve``, waits for /global/health,
                       creates a session, sends initial task via prompt_async,
                       starts background SSE event reader
    get_status()     → returns current BackendStatus (non-blocking)
    drain_events()   → return and clear buffered BackendEvent list (non-blocking)
    relay_feedback() → POSTs a follow-up message to the running session
    stop()           → kills process group; cancels background tasks
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
from collections import deque

import httpx
import psutil
import structlog
from pathlib import Path

from performer.backends._card_docs import card_docs_prompt_section
from performer.backends.base import BackendStatus
from performer.models import BackendEvent, BackendEventType, Score, Stand

log = structlog.get_logger(__name__)

_MAX_TEXT = 200  # max chars kept in BackendEvent.text
_READY_POLL_INTERVAL = 0.2  # seconds between health-check polls
_READY_TIMEOUT = 30.0  # seconds to wait for backend serve process to be ready
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


def _find_free_port() -> int:
    """Return a free TCP port on localhost."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class OpenCodeAdapter:
    """Backend adapter that drives an OpenCode-compatible ``serve`` HTTP API.

    The adapter starts a headless opencode server, creates a session,
    and streams events from the per-instance SSE endpoint.
    """

    def __init__(
        self,
        *,
        executable: str = "opencode",
        adapter_name: str | None = None,
    ) -> None:
        self._executable = executable
        self._adapter_name = adapter_name or executable
        self._proc: asyncio.subprocess.Process | None = None
        self._port: int | None = None
        self._session_id: str | None = None
        self._workspace_dir: str | None = None
        self._status: BackendStatus = BackendStatus(state="working")
        self._reader_task: asyncio.Task[None] | None = None
        self._log_drain_task: asyncio.Task[None] | None = None
        self._event_buffer: deque[BackendEvent] = deque(maxlen=200)
        self._log_buffer: deque[str] = deque(maxlen=200)
        self._client: httpx.AsyncClient | None = None

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
        """Launch ``<executable> serve`` in *stand.path* and send the initial task."""
        port = _find_free_port()
        self._port = port
        self._workspace_dir = str(stand.path)

        self._proc = await asyncio.create_subprocess_exec(
            self._executable, "serve",
            "--port", str(port),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(stand.path),
            start_new_session=True,
            env={**os.environ, **stand.cache_env, **stand.git_env, **score.tool_env},
        )

        # Drain server logs in background (prevents pipe buffer fill)
        self._log_drain_task = asyncio.create_task(
            self._drain_logs(), name=f"{self._adapter_name}-log-drain"
        )

        # Wait for HTTP server to be accepting requests
        await self._wait_for_ready(port)

        # Build HTTP client with workspace directory header
        self._client = httpx.AsyncClient(
            base_url=f"http://127.0.0.1:{port}",
            headers={"x-opencode-directory": str(stand.path)},
            timeout=httpx.Timeout(connect=10.0, read=None, write=10.0, pool=10.0),
        )

        # Create a new session for this task, optionally pinning model and token cap.
        session_body: dict = {}
        if model:
            session_body["modelID"] = model
        if max_tokens is not None:
            session_body["maxTokens"] = max_tokens
        resp = await self._client.post("/session", json=session_body if session_body else None)
        resp.raise_for_status()
        self._session_id = resp.json()["id"]
        log.info(
            "backend.session_created",
            backend=self._adapter_name,
            session_id=self._session_id,
            port=port,
        )

        # Send initial task message (fire-and-forget — server processes asynchronously)
        task_text = _build_task_prompt(score, stand_path=Path(stand.path))
        await self._client.post(
            f"/session/{self._session_id}/prompt_async",
            json={"parts": [{"type": "text", "text": task_text}]},
        )
        log.info(
            "backend.task_dispatched",
            backend=self._adapter_name,
            session_id=self._session_id,
        )

        # Start SSE event reader
        self._reader_task = asyncio.create_task(
            self._event_reader_loop(), name=f"{self._adapter_name}-event-reader"
        )

    def get_status(self) -> BackendStatus:
        """Return the current backend status (non-blocking)."""
        return self._status

    def drain_events(self) -> list[BackendEvent]:
        """Return all buffered events since last call and clear the buffer."""
        events = list(self._event_buffer)
        self._event_buffer.clear()
        return events

    async def relay_feedback(self, feedback: str) -> None:
        """Send a follow-up message to the running session."""
        if self._client is None or self._session_id is None:
            log.warning("relay_feedback called but session not active")
            return
        await self._client.post(
            f"/session/{self._session_id}/prompt_async",
            json={"parts": [{"type": "text", "text": feedback}]},
        )
        self._status = BackendStatus(state="working")
        # Restart the SSE reader if it exited after the previous turn completed
        if self._reader_task is None or self._reader_task.done():
            self._reader_task = asyncio.create_task(
                self._event_reader_loop(), name=f"{self._adapter_name}-event-reader"
            )

    async def stop(self) -> None:
        """Kill the opencode process group and wait for it to exit."""
        for task in (self._reader_task, self._log_drain_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

        if self._client is not None:
            await self._client.aclose()
            self._client = None

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
        log.info("backend.serve_stopped", backend=self._adapter_name)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _wait_for_ready(self, port: int) -> None:
        """Poll GET /global/health until the server responds."""
        deadline = asyncio.get_event_loop().time() + _READY_TIMEOUT
        async with httpx.AsyncClient(timeout=1.0) as probe:
            while asyncio.get_event_loop().time() < deadline:
                try:
                    resp = await probe.get(f"http://127.0.0.1:{port}/global/health")
                    if resp.status_code == 200:
                        log.debug("backend.serve_ready", backend=self._adapter_name, port=port)
                        return
                except Exception:
                    pass
                await asyncio.sleep(_READY_POLL_INTERVAL)
        raise RuntimeError(
            f"{self._adapter_name} serve did not become ready within {_READY_TIMEOUT}s"
        )

    async def _drain_logs(self) -> None:
        """Read opencode serve stdout continuously, buffering for the dashboard."""
        if self._proc is None or self._proc.stdout is None:
            return
        try:
            async for raw in self._proc.stdout:
                line = raw.decode(errors="replace").rstrip()
                if line:
                    self._log_buffer.append(line)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            log.debug("backend.log_drain_error", backend=self._adapter_name, error=str(exc))

    async def _event_reader_loop(self) -> None:
        """Stream SSE events from /event and update internal status."""
        if self._client is None:
            return
        try:
            async with self._client.stream("GET", "/event") as resp:
                async for line in resp.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    data_str = line[5:].strip()
                    if not data_str:
                        continue
                    try:
                        event = json.loads(data_str)
                    except json.JSONDecodeError:
                        log.debug(
                            "backend.non_json_sse_data",
                            backend=self._adapter_name,
                            raw=data_str[:120],
                        )
                        continue
                    log.debug(
                        "backend.event",
                        backend=self._adapter_name,
                        type=event.get("type", event.get("payload", {}).get("type", "?")),
                    )
                    self._handle_event(event)
                    if self._status.state in ("done", "error"):
                        break
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("event reader error", error=str(exc))
            self._status = BackendStatus(state="error", error_reason=str(exc))
        finally:
            if self._status.state == "working":
                await self._resolve_final_status()

    async def _resolve_final_status(self) -> None:
        """Query session state when the SSE stream ends unexpectedly."""
        if self._client is None or self._session_id is None:
            self._status = BackendStatus(state="done")
            return
        try:
            resp = await self._client.get(f"/session/{self._session_id}")
            if resp.status_code == 200:
                info = resp.json()
                # Session is idle when time.idle is set
                if info.get("time", {}).get("idle"):
                    self._status = BackendStatus(state="done")
                    return
        except Exception:
            pass
        self._status = BackendStatus(state="done")

    def _emit(self, type: BackendEventType, text: str, detail: str = "") -> None:
        self._event_buffer.append(BackendEvent(type=type, text=text[:_MAX_TEXT], detail=detail))

    def _handle_event(self, event: dict) -> None:  # type: ignore[type-arg]
        # Events may be wrapped: {"directory": ..., "payload": {...}}
        # or unwrapped: {"type": ..., "properties": {...}}
        payload = event.get("payload", event)
        event_type = payload.get("type", "")
        props = payload.get("properties", {})

        if event_type == "session.updated":
            session = props.get("session", props)
            time_info = session.get("time", {})
            # Session is done when time.idle is set (opencode convention)
            if time_info.get("idle"):
                self._status = BackendStatus(state="done")
            elif session.get("error") or time_info.get("error"):
                reason = str(session.get("error", "unknown error"))
                self._status = BackendStatus(state="error", error_reason=reason)

        elif event_type == "session.idle":
            # Legacy / alternative event type
            self._status = BackendStatus(state="done")

        elif event_type == "session.error":
            reason = props.get("error", {}).get("message", str(props))
            self._status = BackendStatus(state="error", error_reason=reason)
            self._emit(BackendEventType.error, reason)

        elif event_type == "message.part.updated":
            part = props.get("part", {})
            part_type = part.get("type", "")
            if part_type == "text":
                text = part.get("text", "")[:_MAX_TEXT]
                if text:
                    self._status = BackendStatus(state="working", progress=text)
                    self._emit(BackendEventType.progress, text)
            elif part_type in ("tool-input", "tool_input"):
                tool = part.get("toolName", part.get("tool_name", "tool"))
                input_text = str(part.get("input", ""))[:_MAX_TEXT]
                self._emit(BackendEventType.tool_use, f"{tool}: {input_text}", detail=tool)
            elif part_type in ("tool-output", "tool_output"):
                tool = part.get("toolName", part.get("tool_name", "tool"))
                output_text = str(part.get("output", ""))[:_MAX_TEXT]
                self._emit(BackendEventType.tool_use, f"{tool} → {output_text}", detail=tool)

        elif event_type in ("session.message", "message.updated"):
            # Check for questions/need-for-input
            parts = props.get("parts", [])
            questions = [
                p.get("text", "")
                for p in parts
                if p.get("type") in ("question", "needsInput") and p.get("text")
            ]
            if questions:
                self._status = BackendStatus(state="blocked", questions=questions)

        # All other event types are no-ops


def _build_task_prompt(
    score: Score, *, stand_path: Path | None = None
) -> str:
    """Construct the task description sent to opencode as the initial message."""
    parts = []

    # Persona instructions (role-specific behavior)
    if score.persona_instructions:
        parts += ["## Role Instructions", "", score.persona_instructions, ""]

    parts += [f"# Task: {score.title}", ""]
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
