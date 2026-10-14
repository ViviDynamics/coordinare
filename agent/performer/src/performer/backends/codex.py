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

from performer.backends._relay_feedback import relay_feedback_prompt_section
from performer.backends._clarifications import clarification_comment_lines

import asyncio
import json
import os
import pathlib
import re
import signal
import socket
from collections import deque
from collections.abc import Mapping
from typing import Any

import aiohttp
import psutil
import structlog

from performer.backends._scope import scope_prompt_section
from performer.backends._card_docs import scanner_findings_prompt_section, repair_mandate_prompt_section, card_docs_prompt_section, qa_findings_prompt_section, brief_prompt_sections
from performer.backends._env_policy import build_subprocess_env
from performer import transcript
from performer.backends._tool_summary import primary_argument
from performer.backends.base import BackendStatus
from performer.io_utils import iter_lines_chunked
from performer.models import (
    clip_event_text,
    DIAGNOSTIC_ROLE, LOCAL_CAPTURE_RULE, BackendEvent, BackendEventType, Score, Stand, redact_command,
)

log = structlog.get_logger(__name__)

_MAX_TEXT = 200
_READY_TIMEOUT = 30.0
_RPC_TIMEOUT = 30.0

# Transcript (spec 129): ThreadItem types that run a tool and carry a tool-call
# id (``item.id``) on both item/started and item/completed.
_TRANSCRIPT_TOOL_ITEM_TYPES = frozenset({
    "commandExecution",
    "fileChange",
    "mcpToolCall",
    "dynamicToolCall",
    "webSearch",
})
_TRANSCRIPT_TOOL_NAMES: dict[str, str] = {
    "commandExecution": "shell",
    "fileChange": "file_change",
    "webSearch": "web_search",
}

# Approval decision sent in response to server approval requests.
_APPROVE = {"decision": "approved"}


_TOML_BARE_KEY_RE = re.compile(r"^[A-Za-z0-9_-]+$")


def _toml_quote(value: str) -> str:
    """Return ``value`` as a safe TOML basic-string literal.

    Rejects characters that would break out of the double-quoted literal
    (``"``, ``\\``, raw newline) so operator-supplied CODEX_PROVIDER_* env
    vars cannot inject extra TOML keys or invalidate the file.
    """
    if any(ch in value for ch in ('"', "\\", "\n", "\r")):
        raise ValueError(f"unsafe character in codex provider value: {value!r}")
    return f'"{value}"'


def _validate_toml_bare_key(value: str) -> None:
    """Reject values that are unsafe as a TOML bare key or `-c key=val` arg."""
    if not _TOML_BARE_KEY_RE.match(value):
        raise ValueError(f"unsafe codex provider name (must match [A-Za-z0-9_-]+): {value!r}")


def _build_provider_config_toml(env: Mapping[str, str]) -> str | None:
    """Build codex's ``config.toml`` model-provider block from ``CODEX_PROVIDER_*``.

    Returns ``None`` when ``CODEX_PROVIDER_BASE_URL`` is unset (no override active —
    the caller falls back to OpenAI/auth.json mode).

    ``wire_api`` handling (FR-011): codex 0.137.0 rejects every chat-flavoured
    ``wire_api`` string ("chat", "chat_completions", "completions", …) as an invalid
    enum variant — it swallows the config error, falls back to a default config that
    lacks this provider, and dies with a misleading "Model provider not found". The
    only accepted explicit value is ``responses``. So when ``CODEX_PROVIDER_WIRE_API``
    is unset we OMIT the ``wire_api`` line entirely, letting codex fall back to its
    chat-completions default, instead of writing the invalid literal ``"chat"`` the
    old default produced.
    """
    provider_base_url = env.get("CODEX_PROVIDER_BASE_URL", "")
    if not provider_base_url:
        return None
    provider_name = env.get("CODEX_PROVIDER_NAME", "custom")
    provider_env_key = env.get("CODEX_PROVIDER_ENV_KEY", "OPENAI_API_KEY")
    provider_wire_api = env.get("CODEX_PROVIDER_WIRE_API")  # None → omit line (default)
    # provider_name is a TOML bare key and a `-c model_provider=<name>` CLI arg, so it
    # needs the stricter bare-key set; the other values just need to survive quoting.
    _validate_toml_bare_key(provider_name)
    to_quote = [provider_base_url, provider_env_key]
    if provider_wire_api is not None:
        to_quote.append(provider_wire_api)
    for _val in to_quote:
        _toml_quote(_val)  # raises ValueError on unsafe chars
    lines = [
        f"model_provider = {_toml_quote(provider_name)}",
        "",
        f"[model_providers.{provider_name}]",
        f"name = {_toml_quote(provider_name)}",
        f"base_url = {_toml_quote(provider_base_url)}",
        f"env_key = {_toml_quote(provider_env_key)}",
    ]
    if provider_wire_api is not None:
        lines.append(f"wire_api = {_toml_quote(provider_wire_api)}")
    # A wedged response must fail in minutes, not at the performer's 7200 s
    # ceiling: a live architect round waited on one in-flight call for over an
    # hour while the gateway answered fresh probes in a second. codex applies no
    # idle timeout to a custom provider unless told to. These live inside the
    # provider table (codex ignores them at top level).
    lines.append(
        f"stream_idle_timeout_ms = {_positive_int_env(env, 'CODEX_STREAM_IDLE_TIMEOUT_MS', 300_000)}",
    )
    lines.append(
        f"request_max_retries = {_positive_int_env(env, 'CODEX_REQUEST_MAX_RETRIES', 4, minimum=0)}",
    )
    lines.append(
        f"stream_max_retries = {_positive_int_env(env, 'CODEX_STREAM_MAX_RETRIES', 5, minimum=0)}",
    )
    return "\n".join(lines) + "\n"


def _positive_int_env(env: Mapping[str, str], key: str, default: int, *, minimum: int = 1) -> int:
    """Read an integer knob from the endpoint env, or fall back to *default*.

    Raises ``ValueError`` on a non-integer or a value below *minimum*: a bad
    override must fail the job at start, not silently produce a config codex
    rejects (which it reports as the unrelated "Model provider not found").
    """
    raw = env.get(key)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(raw.strip())
    except ValueError as exc:
        raise ValueError(f"{key} must be an integer, got {raw!r}") from exc
    if value < minimum:
        raise ValueError(f"{key} must be >= {minimum}, got {value}")
    return value


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
        # Transcript (spec 129) bookkeeping; never read by the BackendEvent path.
        self._transcript_started: set[str] = set()
        self._transcript_reasoning: dict[str, list[str]] = {}
        self._transcript_total: dict[str, Any] | None = None

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
        transcript.emit(
            "capability",
            text=(
                "Codex app-server notifications carry full item payloads (agent message text, "
                "command and aggregated output, file-change diffs, MCP and dynamic tool "
                "arguments and results) and cumulative token totals; reasoning text appears "
                "only when the model streams reasoning deltas, and usage only when the "
                "app-server sends thread/tokenUsage/updated."
            ),
            input_data={
                "unavailable": [],
                "provided": [
                    "system", "user", "assistant_text", "thinking", "tool_call",
                    "tool_result", "usage", "result", "error",
                ],
            },
        )

        # 088 B1 shared env policy: codex is itself a Node CLI (@openai/codex)
        # and must launch on the IMAGE's node, never the env-cache's
        # project-pinned node (e.g. 18.12.1 → crashes at startup). Image PATH
        # first, cache toolchain dirs appended; every other cache var flows
        # through.
        env = build_subprocess_env(
            cache_env=stand.cache_env,
            git_env=stand.git_env,
            tool_env=score.tool_env,
        )

        # Isolate codex config to the stand directory so the host's
        # ~/.codex/{config.toml,auth.json} are never touched. Codex CLI
        # honors CODEX_HOME natively as the config root.
        codex_dir = pathlib.Path(stand.path) / ".codex"
        codex_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(codex_dir, 0o700)
        except OSError:
            pass
        env["CODEX_HOME"] = str(codex_dir)

        cmd: list[str] = ["codex", "app-server", "--listen", f"ws://127.0.0.1:{port}"]

        # Generic codex model-provider override.
        #
        # Default: OpenAI via OPENAI_API_KEY written to ~/.codex/auth.json.
        # Override: when CODEX_PROVIDER_BASE_URL is set, write a config.toml
        # declaring a custom OpenAI-compatible model_provider and launch codex
        # with `-c model_provider=<name>`. Works for any compatible endpoint
        # (LiteLLM, OpenRouter, Azure, vLLM, etc.).
        #
        # Recognised env vars (all optional):
        #   CODEX_PROVIDER_BASE_URL   — provider base URL (presence = override active)
        #   CODEX_PROVIDER_NAME       — model_provider id (default: "custom")
        #   CODEX_PROVIDER_ENV_KEY    — env var holding the API key (default: "OPENAI_API_KEY")
        #   CODEX_PROVIDER_WIRE_API   — "responses" (or unset → codex's chat-completions
        #                               default; codex 0.137.0 rejects explicit "chat")
        config_toml = _build_provider_config_toml(env)
        if config_toml is not None:
            config_path = codex_dir / "config.toml"
            config_path.write_text(config_toml)
            try:
                os.chmod(config_path, 0o600)
            except OSError:
                pass
            provider_name = env.get("CODEX_PROVIDER_NAME", "custom")
            cmd += ["-c", f"model_provider={provider_name}"]
        else:
            # Codex stores auth state in ~/.codex/auth.json.  In a fresh
            # container there is no auth.json, so codex defaults to
            # ChatGPT-OAuth mode and ignores OPENAI_API_KEY.  Write auth.json
            # to force "api_key" mode.
            api_key = env.get("OPENAI_API_KEY", "")
            if api_key:
                auth = {
                    "auth_mode": "apikey",
                    "OPENAI_API_KEY": api_key,
                    "tokens": None,
                    "last_refresh": None,
                }
                auth_path = codex_dir / "auth.json"
                auth_path.write_text(json.dumps(auth))
                try:
                    os.chmod(auth_path, 0o600)
                except OSError:
                    pass

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
            self._drain_logs(), name="codex-log-drain",
        )

        # Open WebSocket connection via aiohttp (compatible with codex app-server)
        self._ws_session = aiohttp.ClientSession()
        self._ws = await self._ws_session.ws_connect(f"http://127.0.0.1:{port}")

        # Start background message router before any RPC calls
        self._recv_task = asyncio.create_task(
            self._recv_loop(), name="codex-recv",
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
        transcript.emit(
            "system",
            text=f"codex thread started (thread {self._thread_id})",
            input_data=thread_resp,
        )

        # Start the first turn with the task prompt
        task_text = _build_task_prompt(score, stand_path=pathlib.Path(stand.path))
        transcript.emit("user", text=task_text)
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
        transcript.emit("user", text=feedback)
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
                rc = self._proc.returncode
                tail = " | ".join(list(self._log_buffer)[-20:])
                log.error("codex.app_server_exited", returncode=rc, tail=tail)
                raise RuntimeError(
                    f"codex app-server exited before becoming ready (rc={rc}): {tail}",
                )
            line = raw.decode(errors="replace").strip()
            self._log_buffer.append(line)
            if "listening on:" in line:
                log.debug("codex app-server ready", port=self._port)
                return
        tail = " | ".join(list(self._log_buffer)[-20:])
        log.error("codex.app_server_timeout", tail=tail)
        raise RuntimeError(
            f"codex app-server not ready within {_READY_TIMEOUT}s: {tail}",
        )

    async def _drain_logs(self) -> None:
        """Drain remaining stdout lines into the log buffer."""
        if self._proc is None or self._proc.stdout is None:
            return
        try:
            async for raw in iter_lines_chunked(self._proc.stdout):
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
                                RuntimeError(msg["error"].get("message", "RPC error")),
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
            transcript.emit("error", text=str(exc))
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
        seq = self._transcribe_notification(method, params)
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
                # 065 Fix 19: diagnostic — if every extraction path came up empty,
                # dump the raw turn so we can see what the model actually returned
                # (no assistant message? reasoning-only? empty content array?).
                if not output_text:
                    try:
                        raw = json.dumps(turn, default=str)[:4000]
                    except Exception:
                        raw = repr(turn)[:4000]
                    log.warning(
                        "codex.turn_completed.empty_output",
                        item_types=[i.get("type") for i in turn.get("items", [])],
                        item_roles=[i.get("role") for i in turn.get("items", [])
                                    if i.get("type") == "message"],
                        accumulator_len=len(self._output_accumulator),
                        usage=turn.get("usage", {}),
                        raw_turn=raw,
                    )
                self._output_accumulator.clear()
                tokens = turn.get("usage", {}).get("totalTokens", 0)
                self._status = BackendStatus(
                    state="done",
                    output=output_text or None,
                    tokens_processed=tokens or None,
                )
                self._transcribe_turn_end(status, final_text=output_text or None)
            elif status in ("interrupted", "failed"):
                error = (turn.get("error") or {}).get("message", status)
                self._status = BackendStatus(state="error", error_reason=error)
                # The turn-end transcriber writes the error record and returns its seq (211).
                error_seq = self._transcribe_turn_end(
                    status, final_text=error, error_text=error,
                )
                self._event_buffer.append(BackendEvent(
                    type=BackendEventType.error, text=clip_event_text(error), transcript_seq=error_seq,
                ))

        elif method == "item/agentMessage/delta":
            delta = params.get("delta", "")
            if delta:
                self._output_accumulator.append(delta)
                self._status = BackendStatus(state="working", progress=delta[:_MAX_TEXT])
                self._emit(BackendEventType.progress, delta, is_delta=True,
                           stream_id=str(params.get("itemId", "")))

        elif method == "item/commandExecution/outputDelta":
            output = params.get("delta", params.get("output", ""))
            if output:
                self._emit(BackendEventType.tool_use, str(output), detail="shell",
                           is_delta=True, stream_id=str(params.get("itemId", "")))

        elif method == "item/completed":
            item = params.get("item", {})
            item_type = item.get("type", "")
            if item_type == "commandExecution":
                command = str(item.get("command", "shell"))
                # 148: a non-zero exit is the error state the feed shows on the row.
                exit_code = item.get("exitCode")
                self._event_buffer.append(BackendEvent(
                    type=BackendEventType.tool_use, text=redact_command(command)[:_MAX_TEXT],
                    detail="shell", tool="shell",
                    arg=primary_argument("Bash", {"command": command}),
                    status="ok" if exit_code in (0, None) else "error",
                    transcript_seq=seq,
                ))
            elif item_type == "fileChange":
                path = str(item.get("path", "file"))
                self._event_buffer.append(BackendEvent(
                    type=BackendEventType.tool_use, text=f"edit {redact_command(path)}"[:_MAX_TEXT],
                    detail=redact_command(path), tool="Edit",
                    arg=primary_argument("Edit", {"file_path": path}),
                    transcript_seq=seq,
                ))

        elif method == "thread/tokenUsage/updated":
            usage = params.get("tokenUsage", {})
            total = usage.get("total", {})
            tokens = total.get("totalTokens", 0)
            if tokens:
                self._emit(BackendEventType.cost, f"{tokens:,} tokens total")

        elif method == "error":
            error_msg = (params.get("error") or {}).get("message", str(params))
            # Non-fatal errors are emitted as events; fatal ones update status
            self._event_buffer.append(BackendEvent(
                type=BackendEventType.error, text=clip_event_text(error_msg), transcript_seq=seq,
            ))
            if not params.get("willRetry", False):
                self._status = BackendStatus(state="error", error_reason=error_msg)

        elif method == "item/reasoning/textDelta":
            delta = params.get("delta", "")
            if delta:
                self._emit(BackendEventType.thinking, delta, is_delta=True,
                           stream_id=str(params.get("itemId", "")))

        # All other notifications are no-ops (turn/started, thread/started, etc.)

    # ------------------------------------------------------------------
    # Internal: run transcript (spec 129). Full content, never the 200-char
    # BackendEvent summaries. Does not alter any BackendEvent or status.
    # ------------------------------------------------------------------

    @transcript.best_effort
    def _transcribe_notification(self, method: str, params: dict[str, Any]) -> int | None:
        """Returns the seq of the record that the notification's own BackendEvent describes.

        211: a completed tool item gives its tool_result seq, an error gives its error seq.
        None for every other notification.
        """
        if method == "item/started":
            item = params.get("item") or {}
            if str(item.get("type", "")) in _TRANSCRIPT_TOOL_ITEM_TYPES:
                item_id = str(item.get("id", ""))
                self._transcript_started.add(item_id)
                transcript.emit(
                    "tool_call",
                    tool_name=_transcript_tool_name(item),
                    call_id=item_id or None,
                    input_data=item,
                )
        elif method == "item/completed":
            return self._transcribe_item_completed(params.get("item") or {})
        elif method == "item/reasoning/textDelta":
            delta = str(params.get("delta", ""))
            if delta:
                item_id = str(params.get("itemId", ""))
                self._transcript_reasoning.setdefault(item_id, []).append(delta)
        elif method == "thread/tokenUsage/updated":
            total = (params.get("tokenUsage") or {}).get("total")
            if isinstance(total, dict):
                self._transcript_total = total
        elif method == "error":
            error_msg = (params.get("error") or {}).get("message", str(params))
            return transcript.emit("error", text=str(error_msg))
        return None

    @transcript.best_effort
    def _transcribe_item_completed(self, item: dict[str, Any]) -> int | None:
        """Returns the tool_result seq for a completed tool item, None otherwise (211)."""
        item_type = str(item.get("type", ""))
        if item_type == "agentMessage":
            text = str(item.get("text", ""))
            if text:
                transcript.emit("assistant_text", text=text)
        elif item_type == "reasoning":
            item_id = str(item.get("id", ""))
            deltas = self._transcript_reasoning.pop(item_id, [])
            content = [str(part) for part in item.get("content") or []]
            summary = [str(part) for part in item.get("summary") or []]
            text = "\n".join(content) or "".join(deltas) or "\n".join(summary)
            if text:
                transcript.emit("thinking", text=text)
        elif item_type in _TRANSCRIPT_TOOL_ITEM_TYPES:
            item_id = str(item.get("id", ""))
            if item_id not in self._transcript_started:
                # Completion without a prior item/started: still record the call.
                transcript.emit(
                    "tool_call",
                    tool_name=_transcript_tool_name(item),
                    call_id=item_id or None,
                    input_data=item,
                )
            self._transcript_started.discard(item_id)
            return transcript.emit(
                "tool_result",
                tool_name=_transcript_tool_name(item),
                call_id=item_id or None,
                output_data=item,
                is_error=_transcript_tool_error(item),
            )
        return None

    @transcript.best_effort
    def _transcribe_turn_end(
        self,
        status: str,
        *,
        final_text: str | None,
        error_text: str | None = None,
    ) -> int | None:
        """Returns the seq of the error record written for this turn, or None (211)."""
        # Reasoning whose item never completed is flushed so it is not lost.
        for item_id in list(self._transcript_reasoning):
            text = "".join(self._transcript_reasoning.pop(item_id))
            if text:
                transcript.emit("thinking", text=text)
        error_seq: int | None = None
        if error_text:
            error_seq = transcript.emit("error", text=error_text)
        if self._transcript_total is not None:
            # thread/tokenUsage/updated.total is cumulative for the thread, so
            # this record is a run total (cumulative to this turn), not a
            # per-call count. A relay turn emits a newer cumulative record.
            total = self._transcript_total
            transcript.emit(
                "usage",
                text=(
                    "cumulative thread token total as of the end of this turn "
                    f"(totalTokens={total.get('totalTokens')}, "
                    f"reasoningOutputTokens={total.get('reasoningOutputTokens')}); "
                    "a run total, not a per-call count"
                ),
                usage=_transcript_usage(total),
                scope="run",
            )
        transcript.emit(
            "result",
            text=final_text,
            input_data={"status": status},
            is_error=status != "completed",
        )
        return error_seq

    def _emit(self, type: BackendEventType, text: str, detail: str = "", *,
              is_delta: bool = False, stream_id: str = "") -> None:
        self._event_buffer.append(BackendEvent(type=type, text=clip_event_text(text), detail=detail,
                                               is_delta=is_delta, stream_id=stream_id))


def _transcript_tool_name(item: dict[str, Any]) -> str:
    item_type = str(item.get("type", ""))
    if item_type in ("mcpToolCall", "dynamicToolCall"):
        return str(item.get("tool", item_type))
    return _TRANSCRIPT_TOOL_NAMES.get(item_type, item_type)


def _transcript_tool_error(item: dict[str, Any]) -> bool | None:
    """Error flag from the protocol's own status fields; None when it has none."""
    item_type = str(item.get("type", ""))
    status = str(item.get("status", ""))
    if item_type in ("commandExecution", "fileChange"):
        return status in ("failed", "declined")
    if item_type == "mcpToolCall":
        return status == "failed" or item.get("error") is not None
    if item_type == "dynamicToolCall":
        return status == "failed" or item.get("success") is False
    return None


def _transcript_int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


@transcript.best_effort
def _transcript_usage(total: dict[str, Any]) -> transcript.TranscriptUsage:
    """Map the app-server token breakdown to the transcript's four kinds (134 review).

    ``inputTokens`` includes ``cachedInputTokens``: totalTokens is inputTokens plus
    outputTokens, and the cached part is a subset of the input. The fresh input is
    therefore the difference, so the cached tokens are not counted twice. Codex
    reports no cache-write count, so none is recorded.
    """
    input_total = _transcript_int(total.get("inputTokens"))
    cached = _transcript_int(total.get("cachedInputTokens"))
    fresh_input = input_total
    if input_total is not None and cached is not None:
        fresh_input = max(0, input_total - cached)
    return transcript.TranscriptUsage(
        input_tokens=fresh_input,
        output_tokens=_transcript_int(total.get("outputTokens")),
        cache_read=cached,
    )


def _build_task_prompt(
    score: Score, *, stand_path: pathlib.Path | None = None,
) -> str:
    """Construct the task description for the initial Codex turn."""
    # FR-017: persona_instructions is delivered via `developerInstructions` on
    # the Codex thread (see start()). Keeping it out of the prompt body avoids
    # double-delivery.
    parts: list[str] = [f"# Task: {score.title}", ""]
    if score.description:
        parts += [score.description, ""]
    parts += card_docs_prompt_section(score, stand_path)
    parts += qa_findings_prompt_section(score)
    parts += repair_mandate_prompt_section(score)
    parts += scanner_findings_prompt_section(score)
    parts += scope_prompt_section(score, "codex")
    parts += brief_prompt_sections(score)
    if score.acceptance_criteria:
        parts += ["## Acceptance Criteria", ""]
        parts.extend(f"- {c}" for c in score.acceptance_criteria)
    if score.clarifications:
        parts += ["", "## Clarification Q&A", ""]
        for entry in score.clarifications:
            parts += clarification_comment_lines(entry)
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

    # Relay feedback from prior continuation, review or QA cycles
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

    parts += relay_feedback_prompt_section(score.relay_feedback)
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
