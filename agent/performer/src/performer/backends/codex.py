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

import aiohttp
import psutil
import structlog

from performer.backends.base import BackendStatus
from performer.models import BackendEvent, BackendEventType, Score, Stand

log = structlog.get_logger(__name__)

_MAX_TEXT = 200
_READY_TIMEOUT = 30.0
_RPC_TIMEOUT = 30.0

# Approval decision sent in response to server approval requests.
_APPROVE = {"decision": "approved"}
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
        self._ws: Any = None  # aiohttp.ClientWebSocketResponse
        self._ws_session: aiohttp.ClientSession | None = None
        self._port: int | None = None
        self._thread_id: str | None = None
        self._current_turn_id: str | None = None
        self._status: BackendStatus = BackendStatus(state="working")
        self._recv_task: asyncio.Task[None] | None = None
        self._log_drain_task: asyncio.Task[None] | None = None
        self._event_buffer: deque[BackendEvent] = deque(maxlen=200)
        self._log_buffer: deque[str] = deque(maxlen=200)
        self._output_accumulator: list[str] = []  # accumulate agent message text
        # Pending RPC response futures keyed by request id
        self._pending: dict[int, asyncio.Future[Any]] = {}
        self._next_id: int = 1

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
        """Launch codex app-server and dispatch the initial task."""
        port = _find_free_port()
        self._port = port

        env = {**os.environ, **stand.cache_env, **stand.git_env, **score.tool_env}
        cmd: list[str] = ["codex", "app-server", "--listen", f"ws://127.0.0.1:{port}"]
        # Codex stores auth state in ~/.codex/auth.json.  In a fresh container
        # there is no auth.json, so codex defaults to ChatGPT-OAuth mode and
        # ignores OPENAI_API_KEY.  Write auth.json to force "api_key" mode.
        api_key = env.get("OPENAI_API_KEY", "")
        if api_key:
            import json as _json
            import pathlib
            codex_dir = pathlib.Path.home() / ".codex"
            codex_dir.mkdir(parents=True, exist_ok=True)
            auth = {
                "auth_mode": "apikey",
                "OPENAI_API_KEY": api_key,
                "tokens": None,
                "last_refresh": None,
            }
            (codex_dir / "auth.json").write_text(_json.dumps(auth))

        self._proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(stand.path),
            start_new_session=True,
            env=env,
        )

        # Read stdout until the server announces it's ready, then drain the rest.
        await self._wait_for_ready()
        self._log_drain_task = asyncio.create_task(
            self._drain_logs(), name="codex-log-drain"
        )

        # Open WebSocket connection via aiohttp (compatible with codex app-server)
        self._ws_session = aiohttp.ClientSession()
        self._ws = await self._ws_session.ws_connect(f"http://127.0.0.1:{port}")

        # Start background message router before any RPC calls
        self._recv_task = asyncio.create_task(
            self._recv_loop(), name="codex-recv"
        )

        # JSON-RPC handshake
        await self._rpc("initialize", {
            "clientInfo": {"name": "performer", "version": "0.1.0"},
        })
        await self._notify("initialized")

        # Create thread in the workspace (v2 protocol)
        thread_params: dict[str, Any] = {
            "cwd": str(stand.path),
            "approvalPolicy": "never",
            "sandbox": "danger-full-access",
        }
        # Pass persona instructions as developer instructions
        if score.persona_instructions:
            thread_params["developerInstructions"] = score.persona_instructions
        # Pass model override if specified
        if model:
            thread_params["model"] = model
        if temperature is not None:
            thread_params["temperature"] = temperature
        if max_tokens is not None:
            thread_params["maxTokens"] = max_tokens
        if effort is not None:
            thread_params["effort"] = effort
        thread_resp = await self._rpc("thread/start", thread_params)
        self._thread_id = thread_resp["thread"]["id"]
        log.info("codex thread started", thread_id=self._thread_id, port=port)

        # Start the first turn with the task prompt
        task_text = _build_task_prompt(score)
        turn_resp = await self._rpc("turn/start", {
            "threadId": self._thread_id,
            "input": [{"type": "text", "text": task_text, "text_elements": []}],
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

        if self._ws_session is not None:
            try:
                await self._ws_session.close()
            except Exception:
                pass
            self._ws_session = None

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
            async for ws_msg in self._ws:
                if ws_msg.type == aiohttp.WSMsgType.TEXT:
                    raw = ws_msg.data
                elif ws_msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    break
                else:
                    continue
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
        await self._ws.send_str(json.dumps({"id": req_id, "method": method, "params": params}))
        return await asyncio.wait_for(fut, timeout=_RPC_TIMEOUT)

    async def _notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        """Send a JSON-RPC notification (no response expected)."""
        if self._ws is None:
            return
        msg: dict[str, Any] = {"method": method}
        if params is not None:
            msg["params"] = params
        await self._ws.send_str(json.dumps(msg))

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
            await self._ws.send_str(json.dumps(response))
        except Exception as exc:
            log.debug("codex approval send error", error=str(exc))

    def _handle_notification(self, method: str, params: dict[str, Any]) -> None:
        """Update status and emit events based on server notifications."""
        if method == "turn/completed":
            turn = params.get("turn", {})
            status = turn.get("status", "")
            if status == "completed":
                # Capture the final assistant message as output for role-specific handling
                output_text = ""
                for item in turn.get("items", []):
                    if item.get("type") == "message" and item.get("role") == "assistant":
                        for part in item.get("content", []):
                            if part.get("type") == "text":
                                output_text += part.get("text", "")
                # Fallback: accumulated agent message deltas
                if not output_text and self._output_accumulator:
                    output_text = "".join(self._output_accumulator)
                # Fallback: turn summary
                if not output_text:
                    output_text = turn.get("summary", "")
                self._output_accumulator.clear()
                tokens = turn.get("usage", {}).get("totalTokens", 0)
                self._status = BackendStatus(
                    state="done",
                    output=output_text or None,
                    tokens_processed=tokens or None,
                )
            elif status in ("interrupted", "failed"):
                error = (turn.get("error") or {}).get("message", status)
                self._status = BackendStatus(state="error", error_reason=error)
                self._emit(BackendEventType.error, error)

        elif method == "item/agentMessage/delta":
            delta = params.get("delta", "")
            if delta:
                self._output_accumulator.append(delta)
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
                # Include inline review comments with file/line references
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
