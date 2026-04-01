"""CodexBackend — integrates Codex CLI via ``codex app-server`` WebSocket JSON-RPC.

Protocol reference: ``codex app-server generate-json-schema`` / ``generate-ts``

Lifecycle:
    start()          → launches ``codex app-server --listen ws://127.0.0.1:<port>``,
                       waits for "listening on:" in stdout, connects WebSocket,
                       sends initialize → initialized → thread/start → turn/start,
                       spawns background recv loop
    get_status()     → non-blocking current BackendStatus
    drain_events()   → return and clear buffered BackendEvent list
    relay_feedback() → starts a new turn in the existing thread
    stop()           → cancels tasks, closes WS, kills process group
"""
from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
from collections import deque
from typing import Any

import psutil
import structlog
from websockets.asyncio.client import connect as _ws_connect

from performer.backends.base import BackendStatus
from performer.models import BackendEvent, BackendEventType, Score, Stand

log = structlog.get_logger(__name__)

_MAX_TEXT = 200
_READY_TIMEOUT = 30.0
_RPC_TIMEOUT = 30.0

# Approval decision sent in response to server approval requests.
_APPROVE = {"decision": "approved"}


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class CodexBackend:
    """Backend adapter that drives ``codex app-server`` over WebSocket JSON-RPC.

    Maintains a single thread across turns so conversation context is preserved
    between the initial task dispatch and any subsequent relay_feedback calls.
    """

    def __init__(self) -> None:
        self._proc: asyncio.subprocess.Process | None = None
        self._ws: Any = None  # websockets.asyncio.client.ClientConnection
        self._port: int | None = None
        self._thread_id: str | None = None
        self._current_turn_id: str | None = None
        self._status: BackendStatus = BackendStatus(state="working")
        self._recv_task: asyncio.Task[None] | None = None
        self._log_drain_task: asyncio.Task[None] | None = None
        self._event_buffer: deque[BackendEvent] = deque(maxlen=200)
        self._log_buffer: deque[str] = deque(maxlen=200)
        # Pending RPC response futures keyed by request id
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._next_id: int = 1

    # ------------------------------------------------------------------
    # BackendAdapter protocol
    # ------------------------------------------------------------------

    async def start(self, stand: Stand, score: Score, *, model: str | None = None) -> None:
        """Launch codex app-server and dispatch the initial task."""
        port = _find_free_port()
        self._port = port

        self._proc = await asyncio.create_subprocess_exec(
            "codex", "app-server",
            "--listen", f"ws://127.0.0.1:{port}",
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(stand.path),
            start_new_session=True,
            env={**os.environ, **stand.git_env},
        )

        # Read stdout until the server announces it's ready, then drain the rest.
        await self._wait_for_ready()
        self._log_drain_task = asyncio.create_task(
            self._drain_logs(), name="codex-log-drain"
        )

        # Open WebSocket connection
        self._ws = await _ws_connect(f"ws://127.0.0.1:{port}")

        # Start background message router before any RPC calls
        self._recv_task = asyncio.create_task(
            self._recv_loop(), name="codex-recv"
        )

        # JSON-RPC handshake
        await self._rpc("initialize", {
            "clientInfo": {"name": "performer", "version": "0.1.0"},
        })
        await self._notify("initialized")

        # Create thread in the workspace
        thread_resp = await self._rpc("thread/start", {
            "cwd": str(stand.path),
            "approvalPolicy": "never",
            "sandbox": {"type": "dangerFullAccess"},
        })
        self._thread_id = thread_resp["thread"]["id"]
        log.info("codex thread started", thread_id=self._thread_id, port=port)

        # Start the first turn with the task prompt
        task_text = _build_task_prompt(score)
        turn_resp = await self._rpc("turn/start", {
            "threadId": self._thread_id,
            "input": [{"type": "text", "text": task_text, "text_elements": []}],
            "approvalPolicy": "never",
        })
        self._current_turn_id = turn_resp["turn"]["id"]
        log.info("codex turn started", turn_id=self._current_turn_id)

    def get_status(self) -> BackendStatus:
        return self._status

    def drain_events(self) -> list[BackendEvent]:
        events = list(self._event_buffer)
        self._event_buffer.clear()
        return events

    async def relay_feedback(self, feedback: str) -> None:
        """Start a new turn in the existing thread with the feedback text."""
        if self._ws is None or self._thread_id is None:
            log.warning("relay_feedback called but no active thread")
            return
        turn_resp = await self._rpc("turn/start", {
            "threadId": self._thread_id,
            "input": [{"type": "text", "text": feedback, "text_elements": []}],
            "approvalPolicy": "never",
        })
        self._current_turn_id = turn_resp["turn"]["id"]
        self._status = BackendStatus(state="working")

    async def stop(self) -> None:
        """Cancel tasks, close WebSocket, kill process group."""
        for task in (self._recv_task, self._log_drain_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

        # Reject any pending RPC futures
        for fut in self._pending.values():
            if not fut.done():
                fut.cancel()
        self._pending.clear()

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
        log.info("codex app-server stopped")

    # ------------------------------------------------------------------
    # Internal: startup
    # ------------------------------------------------------------------

    async def _wait_for_ready(self) -> None:
        """Read stdout lines until codex announces the WebSocket is listening."""
        if self._proc is None or self._proc.stdout is None:
            raise RuntimeError("codex app-server process not started")
        deadline = asyncio.get_event_loop().time() + _READY_TIMEOUT
        while asyncio.get_event_loop().time() < deadline:
            try:
                raw = await asyncio.wait_for(self._proc.stdout.readline(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            if not raw:
                raise RuntimeError("codex app-server exited before becoming ready")
            line = raw.decode(errors="replace").strip()
            self._log_buffer.append(line)
            if "listening on:" in line:
                log.debug("codex app-server ready", port=self._port)
                return
        raise RuntimeError(f"codex app-server not ready within {_READY_TIMEOUT}s")

    async def _drain_logs(self) -> None:
        """Drain remaining stdout lines into the log buffer."""
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
            log.debug("codex log drain error", error=str(exc))

    # ------------------------------------------------------------------
    # Internal: WebSocket message router
    # ------------------------------------------------------------------

    async def _recv_loop(self) -> None:
        """Route incoming WebSocket messages to RPC futures or notification handlers."""
        if self._ws is None:
            return
        try:
            async for raw in self._ws:
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    log.debug("codex non-json message", raw=str(raw)[:80])
                    continue

                msg_id = msg.get("id")
                method = msg.get("method")

                if msg_id is not None and method is not None:
                    # Server-initiated request — needs our response
                    asyncio.create_task(
                        self._handle_server_request(msg),
                        name="codex-approval",
                    )
                elif msg_id is not None and msg_id in self._pending:
                    # Response to one of our RPC calls
                    fut = self._pending.pop(msg_id)
                    if not fut.done():
                        if "error" in msg:
                            fut.set_exception(
                                RuntimeError(msg["error"].get("message", "RPC error"))
                            )
                        else:
                            fut.set_result(msg.get("result") or {})
                elif method is not None:
                    # Unsolicited notification
                    self._handle_notification(method, msg.get("params") or {})
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("codex recv loop error", error=str(exc))
            if self._status.state == "working":
                self._status = BackendStatus(state="error", error_reason=str(exc))
        finally:
            # Reject any still-pending futures
            for fut in self._pending.values():
                if not fut.done():
                    fut.cancel()

    async def _rpc(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Send a JSON-RPC request and await its response."""
        if self._ws is None:
            raise RuntimeError("WebSocket not connected")
        req_id = self._next_id
        self._next_id += 1
        loop = asyncio.get_event_loop()
        fut: asyncio.Future[Any] = loop.create_future()
        self._pending[req_id] = fut
        await self._ws.send(json.dumps({"id": req_id, "method": method, "params": params}))
        return await asyncio.wait_for(fut, timeout=_RPC_TIMEOUT)

    async def _notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        """Send a JSON-RPC notification (no response expected)."""
        if self._ws is None:
            return
        msg: dict[str, Any] = {"method": method}
        if params is not None:
            msg["params"] = params
        await self._ws.send(json.dumps(msg))

    # ------------------------------------------------------------------
    # Internal: message handlers
    # ------------------------------------------------------------------

    async def _handle_server_request(self, msg: dict[str, Any]) -> None:
        """Respond to approval / input requests from the server."""
        method = msg.get("method", "")
        req_id = msg.get("id")
        if req_id is None or self._ws is None:
            return

        if method in (
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
            "applyPatchApproval",
            "execCommandApproval",
        ):
            # Auto-approve all tool/file operations
            response = {"id": req_id, "result": _APPROVE}
        elif method == "item/tool/requestUserInput":
            # Return an empty string for any user-input prompt
            response = {"id": req_id, "result": {"input": ""}}
        else:
            # Unknown server request — acknowledge with empty result
            log.debug("codex unknown server request", method=method)
            response = {"id": req_id, "result": {}}

        try:
            await self._ws.send(json.dumps(response))
        except Exception as exc:
            log.debug("codex approval send error", error=str(exc))

    def _handle_notification(self, method: str, params: dict[str, Any]) -> None:
        """Update status and emit events based on server notifications."""
        if method == "turn/completed":
            turn = params.get("turn", {})
            status = turn.get("status", "")
            if status == "completed":
                self._status = BackendStatus(state="done")
            elif status in ("interrupted", "failed"):
                error = (turn.get("error") or {}).get("message", status)
                self._status = BackendStatus(state="error", error_reason=error)
                self._emit(BackendEventType.error, error)

        elif method == "item/agentMessage/delta":
            delta = params.get("delta", "")
            if delta:
                self._status = BackendStatus(state="working", progress=delta[:_MAX_TEXT])
                self._emit(BackendEventType.progress, delta)

        elif method == "item/commandExecution/outputDelta":
            output = params.get("delta", params.get("output", ""))
            if output:
                self._emit(BackendEventType.tool_use, str(output)[:_MAX_TEXT], detail="shell")

        elif method == "item/completed":
            item = params.get("item", {})
            item_type = item.get("type", "")
            if item_type == "commandExecution":
                cmd = str(item.get("command", "shell"))[:_MAX_TEXT]
                self._emit(BackendEventType.tool_use, cmd, detail="shell")
            elif item_type == "fileChange":
                path = str(item.get("path", "file"))[:_MAX_TEXT]
                self._emit(BackendEventType.tool_use, f"edit {path}", detail=path)

        elif method == "thread/tokenUsage/updated":
            usage = params.get("tokenUsage", {})
            total = usage.get("total", {})
            tokens = total.get("totalTokens", 0)
            if tokens:
                self._emit(BackendEventType.cost, f"{tokens:,} tokens total")

        elif method == "error":
            error_msg = (params.get("error") or {}).get("message", str(params))
            # Non-fatal errors are emitted as events; fatal ones update status
            self._emit(BackendEventType.error, error_msg[:_MAX_TEXT])
            if not params.get("willRetry", False):
                self._status = BackendStatus(state="error", error_reason=error_msg)

        elif method == "item/reasoning/textDelta":
            delta = params.get("delta", "")
            if delta:
                self._emit(BackendEventType.thinking, delta[:_MAX_TEXT])

        # All other notifications are no-ops (turn/started, thread/started, etc.)

    def _emit(self, type: BackendEventType, text: str, detail: str = "") -> None:
        self._event_buffer.append(BackendEvent(type=type, text=text[:_MAX_TEXT], detail=detail))


def _build_task_prompt(score: Score) -> str:
    """Construct the task description for the initial Codex turn."""
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
