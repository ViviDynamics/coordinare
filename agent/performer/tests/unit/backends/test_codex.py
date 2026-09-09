"""Unit tests for CodexBackend (app-server WebSocket JSON-RPC implementation)."""
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from performer.backends.codex import (
    CodexBackend,
    _build_task_prompt,
    _find_free_port,
    _toml_quote,
    _validate_toml_bare_key,
)
from performer.models import BackendEventType, Score, Stand


def _score(**kwargs) -> Score:
    defaults = dict(
        title="Codex Task",
        repo_url="https://github.com/org/repo",
        branch="main",
        github_token="tok",
    )
    defaults.update(kwargs)
    return Score(**defaults)


def _stand(tmp_path: Path) -> Stand:
    return Stand(path=tmp_path, branch="main")


def _fake_proc(pid: int = 42) -> MagicMock:
    proc = MagicMock()
    proc.pid = pid
    proc.returncode = None

    async def _empty():
        return
        yield  # pragma: no cover

    proc.stdout = MagicMock()
    proc.stdout.__aiter__ = lambda self: _empty()
    proc.stdout.readline = AsyncMock(return_value=b"")
    # _drain_logs reads stdout.read(n); EOF (b"") exits the loop immediately.
    proc.stdout.read = AsyncMock(return_value=b"")
    proc.wait = AsyncMock(return_value=0)
    return proc


def _make_ws_msg(data: str) -> MagicMock:
    """Create a mock aiohttp WSMessage with TEXT type."""
    msg = MagicMock()
    msg.type = aiohttp.WSMsgType.TEXT
    msg.data = data
    return msg


def _make_ws(responses: list[dict] | None = None) -> MagicMock:
    """Create a mock aiohttp WebSocket that yields the given response dicts."""
    ws = MagicMock()

    ws.send_str = AsyncMock()
    ws.send_json = AsyncMock()
    ws.close = AsyncMock()

    if responses is not None:
        async def _iter():
            for r in responses:
                yield _make_ws_msg(json.dumps(r))

        ws.__aiter__ = lambda self: _iter()
    else:
        async def _empty_iter():
            return
            yield  # pragma: no cover

        ws.__aiter__ = lambda self: _empty_iter()

    return ws


# ---------------------------------------------------------------------------
# _find_free_port
# ---------------------------------------------------------------------------

class TestFindFreePort:
    def test_returns_positive_int(self) -> None:
        port = _find_free_port()
        assert isinstance(port, int)
        assert port > 0


# ---------------------------------------------------------------------------
# get_status() / drain_events()
# ---------------------------------------------------------------------------

class TestCodexBackendGetStatus:
    def test_initial_status_is_working(self) -> None:
        adapter = CodexBackend()
        assert adapter.get_status().state == "working"

    def test_drain_events_returns_and_clears(self) -> None:
        adapter = CodexBackend()
        from performer.models import BackendEvent
        from datetime import UTC, datetime
        adapter._event_buffer.append(
            BackendEvent(type=BackendEventType.progress, text="hi", timestamp=datetime.now(UTC))
        )
        first = adapter.drain_events()
        second = adapter.drain_events()
        assert len(first) == 1
        assert len(second) == 0


# ---------------------------------------------------------------------------
# _handle_notification
# ---------------------------------------------------------------------------

class TestHandleNotification:
    def test_turn_completed_sets_done(self) -> None:
        adapter = CodexBackend()
        adapter._handle_notification("turn/completed", {"turn": {"status": "completed"}})
        assert adapter.get_status().state == "done"

    def test_turn_completed_interrupted_sets_error(self) -> None:
        adapter = CodexBackend()
        adapter._handle_notification("turn/completed", {"turn": {"status": "interrupted"}})
        assert adapter.get_status().state == "error"

    def test_turn_completed_failed_sets_error(self) -> None:
        adapter = CodexBackend()
        adapter._handle_notification(
            "turn/completed",
            {"turn": {"status": "failed", "error": {"message": "oh no"}}}
        )
        status = adapter.get_status()
        assert status.state == "error"
        assert "oh no" in (status.error_reason or "")

    def test_agent_message_delta_emits_progress(self) -> None:
        adapter = CodexBackend()
        adapter._handle_notification("item/agentMessage/delta", {"delta": " thinking...", "itemId": "message-1"})
        events = adapter.drain_events()
        assert len(events) == 1
        assert events[0].type == BackendEventType.progress
        assert events[0].is_delta is True
        assert events[0].stream_id == "message-1"
        assert events[0].text == " thinking..."

    def test_agent_message_delta_empty_is_noop(self) -> None:
        adapter = CodexBackend()
        adapter._handle_notification("item/agentMessage/delta", {"delta": ""})
        assert adapter.drain_events() == []

    def test_command_execution_output_delta(self) -> None:
        adapter = CodexBackend()
        adapter._handle_notification("item/commandExecution/outputDelta", {"delta": "$ ls"})
        events = adapter.drain_events()
        assert len(events) == 1
        assert events[0].type == BackendEventType.tool_use

    def test_command_execution_output_delta_fallback_key(self) -> None:
        adapter = CodexBackend()
        adapter._handle_notification("item/commandExecution/outputDelta", {"output": "stdout"})
        events = adapter.drain_events()
        assert len(events) == 1

    def test_item_completed_command_execution(self) -> None:
        adapter = CodexBackend()
        adapter._handle_notification(
            "item/completed",
            {"item": {"type": "commandExecution", "command": "git commit"}}
        )
        events = adapter.drain_events()
        assert len(events) == 1
        assert events[0].type == BackendEventType.tool_use

    def test_item_completed_file_change(self) -> None:
        adapter = CodexBackend()
        adapter._handle_notification(
            "item/completed",
            {"item": {"type": "fileChange", "path": "src/foo.py"}}
        )
        events = adapter.drain_events()
        assert len(events) == 1
        assert "src/foo.py" in events[0].text

    def test_item_completed_other_type_is_noop(self) -> None:
        adapter = CodexBackend()
        adapter._handle_notification("item/completed", {"item": {"type": "reasoning"}})
        assert adapter.drain_events() == []

    def test_token_usage_updated(self) -> None:
        adapter = CodexBackend()
        adapter._handle_notification(
            "thread/tokenUsage/updated",
            {"tokenUsage": {"total": {"totalTokens": 1234}}}
        )
        events = adapter.drain_events()
        assert len(events) == 1
        assert "1,234" in events[0].text

    def test_token_usage_zero_is_noop(self) -> None:
        adapter = CodexBackend()
        adapter._handle_notification(
            "thread/tokenUsage/updated",
            {"tokenUsage": {"total": {"totalTokens": 0}}}
        )
        assert adapter.drain_events() == []

    def test_error_notification_fatal(self) -> None:
        adapter = CodexBackend()
        adapter._handle_notification(
            "error",
            {"error": {"message": "server crashed"}, "willRetry": False}
        )
        assert adapter.get_status().state == "error"
        events = adapter.drain_events()
        assert len(events) == 1
        assert events[0].type == BackendEventType.error

    def test_error_notification_retryable_does_not_set_error_status(self) -> None:
        adapter = CodexBackend()
        adapter._handle_notification(
            "error",
            {"error": {"message": "transient"}, "willRetry": True}
        )
        assert adapter.get_status().state == "working"

    def test_reasoning_text_delta(self) -> None:
        adapter = CodexBackend()
        adapter._handle_notification("item/reasoning/textDelta", {"delta": "hmm..."})
        events = adapter.drain_events()
        assert len(events) == 1
        assert events[0].type == BackendEventType.thinking

    def test_reasoning_text_delta_empty_is_noop(self) -> None:
        adapter = CodexBackend()
        adapter._handle_notification("item/reasoning/textDelta", {"delta": ""})
        assert adapter.drain_events() == []

    def test_unknown_notification_is_noop(self) -> None:
        adapter = CodexBackend()
        adapter._handle_notification("turn/started", {})
        assert adapter.drain_events() == []


# ---------------------------------------------------------------------------
# _handle_server_request
# ---------------------------------------------------------------------------

class TestHandleServerRequest:
    async def test_approves_command_execution(self) -> None:
        adapter = CodexBackend()
        ws = _make_ws()
        adapter._ws = ws

        await adapter._handle_server_request({
            "id": 10,
            "method": "item/commandExecution/requestApproval",
            "params": {},
        })

        ws.send_str.assert_awaited_once()
        sent = json.loads(ws.send_str.call_args[0][0])
        assert sent["id"] == 10
        assert sent["result"]["decision"] == "approved"

    async def test_approves_file_change(self) -> None:
        adapter = CodexBackend()
        ws = _make_ws()
        adapter._ws = ws

        await adapter._handle_server_request({
            "id": 11,
            "method": "item/fileChange/requestApproval",
            "params": {},
        })

        ws.send_str.assert_awaited_once()

    async def test_approves_apply_patch(self) -> None:
        adapter = CodexBackend()
        ws = _make_ws()
        adapter._ws = ws

        await adapter._handle_server_request({"id": 12, "method": "applyPatchApproval", "params": {}})
        ws.send_str.assert_awaited_once()

    async def test_approves_exec_command(self) -> None:
        adapter = CodexBackend()
        ws = _make_ws()
        adapter._ws = ws

        await adapter._handle_server_request({"id": 13, "method": "execCommandApproval", "params": {}})
        ws.send_str.assert_awaited_once()

    async def test_returns_empty_input_for_user_input_request(self) -> None:
        adapter = CodexBackend()
        ws = _make_ws()
        adapter._ws = ws

        await adapter._handle_server_request({"id": 14, "method": "item/tool/requestUserInput", "params": {}})

        ws.send_str.assert_awaited_once()
        sent = json.loads(ws.send_str.call_args[0][0])
        assert sent["result"]["input"] == ""

    async def test_unknown_server_request_sends_empty_result(self) -> None:
        adapter = CodexBackend()
        ws = _make_ws()
        adapter._ws = ws

        await adapter._handle_server_request({"id": 15, "method": "unknownMethod", "params": {}})
        ws.send_str.assert_awaited_once()

    async def test_noop_when_no_id(self) -> None:
        adapter = CodexBackend()
        ws = _make_ws()
        adapter._ws = ws

        await adapter._handle_server_request({"method": "item/commandExecution/requestApproval"})
        ws.send_str.assert_not_awaited()

    async def test_noop_when_no_ws(self) -> None:
        adapter = CodexBackend()
        adapter._ws = None
        await adapter._handle_server_request({"id": 1, "method": "applyPatchApproval"})
        # should not raise

    async def test_send_error_is_swallowed(self) -> None:
        adapter = CodexBackend()
        ws = _make_ws()
        ws.send_str = AsyncMock(side_effect=RuntimeError("closed"))
        adapter._ws = ws

        await adapter._handle_server_request({"id": 16, "method": "applyPatchApproval", "params": {}})
        # should not propagate


# ---------------------------------------------------------------------------
# _recv_loop
# ---------------------------------------------------------------------------

class TestRecvLoop:
    async def test_routes_rpc_response_to_future(self) -> None:
        adapter = CodexBackend()
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        adapter._pending[5] = fut

        ws = _make_ws([{"id": 5, "result": {"thread": {"id": "t1"}}}])
        adapter._ws = ws

        await adapter._recv_loop()
        assert fut.result() == {"thread": {"id": "t1"}}

    async def test_routes_rpc_error_to_future(self) -> None:
        adapter = CodexBackend()
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        adapter._pending[6] = fut

        ws = _make_ws([{"id": 6, "error": {"message": "bad request"}}])
        adapter._ws = ws

        await adapter._recv_loop()
        assert fut.exception() is not None

    async def test_routes_notification_to_handler(self) -> None:
        adapter = CodexBackend()
        ws = _make_ws([
            {"method": "item/agentMessage/delta", "params": {"delta": "hello"}}
        ])
        adapter._ws = ws

        await adapter._recv_loop()
        events = adapter.drain_events()
        assert any(e.text == "hello" for e in events)

    async def test_spawns_task_for_server_request(self) -> None:
        adapter = CodexBackend()
        ws = _make_ws([
            {"id": 20, "method": "applyPatchApproval", "params": {}}
        ])
        adapter._ws = ws

        await adapter._recv_loop()
        # Give spawned task a chance to run
        await asyncio.sleep(0)

    async def test_skips_non_json_message(self) -> None:
        adapter = CodexBackend()

        async def _iter():
            yield _make_ws_msg("not json at all")

        ws = MagicMock()
        ws.__aiter__ = lambda self: _iter()
        adapter._ws = ws

        await adapter._recv_loop()  # should not raise

    async def test_sets_error_on_exception(self) -> None:
        adapter = CodexBackend()

        async def _iter():
            raise RuntimeError("connection lost")
            yield  # pragma: no cover

        ws = MagicMock()
        ws.__aiter__ = lambda self: _iter()
        adapter._ws = ws

        await adapter._recv_loop()
        assert adapter.get_status().state == "error"

    async def test_cleans_up_pending_on_error(self) -> None:
        adapter = CodexBackend()
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        adapter._pending[99] = fut

        async def _iter():
            raise RuntimeError("crash")
            yield  # pragma: no cover

        ws = MagicMock()
        ws.__aiter__ = lambda self: _iter()
        adapter._ws = ws

        await adapter._recv_loop()
        assert fut.cancelled()

    async def test_noop_when_no_ws(self) -> None:
        adapter = CodexBackend()
        adapter._ws = None
        await adapter._recv_loop()  # should return immediately


# ---------------------------------------------------------------------------
# _wait_for_ready
# ---------------------------------------------------------------------------

class TestWaitForReady:
    async def test_returns_when_listening_on_found(self) -> None:
        proc = MagicMock()
        proc.stdout = MagicMock()
        proc.stdout.readline = AsyncMock(side_effect=[
            b"codex app-server (WebSockets)\n",
            b"  listening on: ws://127.0.0.1:4567\n",
        ])

        adapter = CodexBackend()
        adapter._proc = proc
        adapter._port = 4567
        await adapter._wait_for_ready()  # should not raise

    async def test_raises_on_empty_line(self) -> None:
        proc = MagicMock()
        proc.stdout = MagicMock()
        proc.stdout.readline = AsyncMock(return_value=b"")

        adapter = CodexBackend()
        adapter._proc = proc
        adapter._port = 4567

        with pytest.raises(RuntimeError, match="exited before becoming ready"):
            await adapter._wait_for_ready()

    async def test_raises_when_no_proc(self) -> None:
        adapter = CodexBackend()
        adapter._proc = None

        with pytest.raises(RuntimeError, match="not started"):
            await adapter._wait_for_ready()

    async def test_raises_on_timeout(self) -> None:

        proc = MagicMock()
        proc.stdout = MagicMock()

        call_count = 0

        async def _readline():
            nonlocal call_count
            call_count += 1
            raise asyncio.TimeoutError()

        proc.stdout.readline = AsyncMock(side_effect=_readline)

        adapter = CodexBackend()
        adapter._proc = proc
        adapter._port = 4567

        with patch("performer.backends.codex._READY_TIMEOUT", 0.01):
            with pytest.raises(RuntimeError, match="not ready within"):
                await adapter._wait_for_ready()


# ---------------------------------------------------------------------------
# _drain_logs
# ---------------------------------------------------------------------------

class TestDrainLogs:
    async def test_drains_stdout_lines(self) -> None:
        proc = MagicMock()
        proc.stdout = MagicMock()
        proc.stdout.read = AsyncMock(side_effect=[b"line one\nline two\n", b""])

        adapter = CodexBackend()
        adapter._proc = proc
        await adapter._drain_logs()

        assert "line one" in adapter._log_buffer
        assert "line two" in adapter._log_buffer

    async def test_drains_huge_line_over_64kib(self) -> None:
        # Regression: `async for ... in stdout` used to raise LimitOverrunError
        # on lines >64 KiB. Chunked reader should handle it transparently.
        huge = b"x" * 200_000
        proc = MagicMock()
        proc.stdout = MagicMock()
        proc.stdout.read = AsyncMock(side_effect=[huge[:65536], huge[65536:] + b"\n", b""])

        adapter = CodexBackend()
        adapter._proc = proc
        await adapter._drain_logs()

        assert "x" * 200_000 in adapter._log_buffer

    async def test_noop_when_no_proc(self) -> None:
        adapter = CodexBackend()
        adapter._proc = None
        await adapter._drain_logs()  # should not raise

    async def test_handles_cancelled_error(self) -> None:
        proc = MagicMock()
        proc.stdout = MagicMock()
        proc.stdout.read = AsyncMock(side_effect=asyncio.CancelledError())

        adapter = CodexBackend()
        adapter._proc = proc
        await adapter._drain_logs()  # should not propagate CancelledError


# ---------------------------------------------------------------------------
# relay_feedback()
# ---------------------------------------------------------------------------

class TestRelayFeedback:
    async def test_relay_starts_new_turn_in_thread(self) -> None:
        adapter = CodexBackend()
        adapter._thread_id = "thread-001"

        ws = _make_ws()
        adapter._ws = ws

        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        fut.set_result({"turn": {"id": "turn-002"}})

        with patch.object(adapter, "_rpc", return_value=fut.result()) as mock_rpc:
            await adapter.relay_feedback("fix the tests")

        mock_rpc.assert_called_once_with("turn/start", {
            "threadId": "thread-001",
            "input": [{"type": "text", "text": "fix the tests", "text_elements": []}],
        })
        assert adapter._current_turn_id == "turn-002"
        assert adapter.get_status().state == "working"

    async def test_relay_noop_when_no_ws(self) -> None:
        adapter = CodexBackend()
        adapter._ws = None
        adapter._thread_id = "t1"
        await adapter.relay_feedback("feedback")  # should not raise

    async def test_relay_noop_when_no_thread_id(self) -> None:
        adapter = CodexBackend()
        adapter._ws = _make_ws()
        adapter._thread_id = None
        await adapter.relay_feedback("feedback")  # should not raise


# ---------------------------------------------------------------------------
# stop()
# ---------------------------------------------------------------------------

class TestStop:
    async def test_stop_closes_ws_and_kills_proc(self, tmp_path: Path) -> None:
        proc = _fake_proc(pid=1234)
        adapter = CodexBackend()
        adapter._proc = proc

        ws = _make_ws()
        adapter._ws = ws

        with (
            patch("performer.backends.codex.os.getpgid", return_value=1234),
            patch("performer.backends.codex.os.killpg") as mock_killpg,
        ):
            await adapter.stop()

        ws.close.assert_awaited_once()
        mock_killpg.assert_called_once()

    async def test_stop_uses_psutil_fallback(self) -> None:
        proc = _fake_proc(pid=5555)
        adapter = CodexBackend()
        adapter._proc = proc
        adapter._ws = _make_ws()

        mock_psutil_proc = MagicMock()
        mock_psutil_proc.children.return_value = []
        mock_psutil_proc.kill = MagicMock()

        with (
            patch("performer.backends.codex.os.getpgid", side_effect=OSError("no pgid")),
            patch("performer.backends.codex.psutil.Process", return_value=mock_psutil_proc),
        ):
            await adapter.stop()

        mock_psutil_proc.kill.assert_called_once()

    async def test_stop_rejects_pending_futures(self) -> None:
        adapter = CodexBackend()
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        adapter._pending[1] = fut
        adapter._ws = _make_ws()

        await adapter.stop()
        assert fut.cancelled()

    async def test_stop_cancels_recv_task(self) -> None:
        adapter = CodexBackend()
        adapter._ws = _make_ws()

        async def _long_running():
            await asyncio.sleep(1000)

        adapter._recv_task = asyncio.create_task(_long_running())
        await adapter.stop()
        assert adapter._recv_task.done()

    async def test_stop_noop_when_no_proc(self) -> None:
        adapter = CodexBackend()
        await adapter.stop()  # should not raise

    async def test_stop_noop_when_proc_already_exited(self) -> None:
        proc = _fake_proc(pid=2222)
        proc.returncode = 0
        adapter = CodexBackend()
        adapter._proc = proc

        await adapter.stop()  # should not attempt kill


# ---------------------------------------------------------------------------
# start()
# ---------------------------------------------------------------------------

class TestStart:
    async def test_start_launches_subprocess_and_connects(self, tmp_path: Path) -> None:
        """start() should launch codex app-server, connect WS, and dispatch first turn."""
        proc = _fake_proc(pid=9999)
        # Simulate stdout: first readline returns "listening on:", then EOF
        proc.stdout.readline = AsyncMock(side_effect=[
            b"  listening on: ws://127.0.0.1:4040\n",
            b"",
        ])

        ws = _make_ws()
        mock_session = MagicMock()
        mock_session.ws_connect = AsyncMock(return_value=ws)

        rpc_responses = {
            "initialize": {},
            "thread/start": {"thread": {"id": "thread-start-001"}},
            "turn/start": {"turn": {"id": "turn-start-001"}},
        }

        rpc_call_order = []

        async def _mock_rpc(method: str, params: dict) -> dict:
            rpc_call_order.append(method)
            return rpc_responses[method]

        with patch(
            "performer.backends.codex.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ), patch(
            "performer.backends.codex.aiohttp.ClientSession",
            return_value=mock_session,
        ):
            adapter = CodexBackend()
            with patch.object(adapter, "_rpc", side_effect=_mock_rpc):
                await adapter.start(_stand(tmp_path), _score())

        assert adapter._thread_id == "thread-start-001"
        assert adapter._current_turn_id == "turn-start-001"
        assert rpc_call_order == ["initialize", "thread/start", "turn/start"]

    async def test_start_uses_stand_path_as_cwd(self, tmp_path: Path) -> None:
        proc = _fake_proc()
        proc.stdout.readline = AsyncMock(side_effect=[
            b"  listening on: ws://127.0.0.1:4040\n",
        ])

        ws = _make_ws()
        mock_session = MagicMock()
        mock_session.ws_connect = AsyncMock(return_value=ws)

        with patch(
            "performer.backends.codex.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ) as mock_exec, patch(
            "performer.backends.codex.aiohttp.ClientSession",
            return_value=mock_session,
        ):
            adapter = CodexBackend()
            rpc_resp = {
                "initialize": {},
                "thread/start": {"thread": {"id": "t1"}},
                "turn/start": {"turn": {"id": "r1"}},
            }
            with patch.object(adapter, "_rpc", side_effect=lambda m, p: rpc_resp[m]):
                await adapter.start(_stand(tmp_path), _score())

        call_kwargs = mock_exec.call_args[1]
        assert call_kwargs["cwd"] == str(tmp_path)

    async def test_start_appends_cache_path_after_image_path(self, tmp_path: Path) -> None:
        """088 B1: shared env policy — image PATH first, cache PATH appended.

        codex is itself a Node CLI (@openai/codex); the env-cache PATH prepends
        the project's .nvmrc node (e.g. 18.12.1) which can crash modern Node
        CLIs at startup. The CLI must launch on the IMAGE's node, but the cache
        toolchain dirs must stay REACHABLE for snapshot-env agents: image PATH
        first, cache dirs appended deduplicated. Other cache vars (NODE_PATH,
        etc.) still flow."""
        proc = _fake_proc()
        proc.stdout.readline = AsyncMock(side_effect=[
            b"  listening on: ws://127.0.0.1:4040\n",
        ])
        ws = _make_ws()
        mock_session = MagicMock()
        mock_session.ws_connect = AsyncMock(return_value=ws)

        stand = Stand(
            path=tmp_path,
            branch="main",
            git_env={"GIT_AUTHOR_NAME": "performer"},
            cache_env={"PATH": "/devenv/foo/node-v18.12.1/bin:/usr/bin", "NODE_PATH": "/devenv/foo/node_modules"},
        )

        with patch(
            "performer.backends.codex.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ) as mock_exec, patch(
            "performer.backends.codex.aiohttp.ClientSession",
            return_value=mock_session,
        ):
            adapter = CodexBackend()
            rpc_resp = {
                "initialize": {},
                "thread/start": {"thread": {"id": "t1"}},
                "turn/start": {"turn": {"id": "r1"}},
            }
            with patch.object(adapter, "_rpc", side_effect=lambda m, p: rpc_resp[m]):
                await adapter.start(stand, _score())

        env = mock_exec.call_args[1]["env"]
        assert env["NODE_PATH"] == "/devenv/foo/node_modules"
        image_path = os.environ["PATH"]
        # Image dirs FIRST — the CLI's interpreter resolves to the image's node.
        assert env["PATH"].startswith(image_path)
        # Cache toolchain dirs APPENDED — reachable, never shadowing the image.
        assert env["PATH"].index(image_path) < env["PATH"].index("node-v18.12.1")
        assert env["GIT_AUTHOR_NAME"] == "performer"

    async def test_start_git_env_overrides_cache_env_on_conflict(self, tmp_path: Path) -> None:
        """060: precedence is os.environ < cache_env < git_env — git auth must win."""
        proc = _fake_proc()
        proc.stdout.readline = AsyncMock(side_effect=[
            b"  listening on: ws://127.0.0.1:4040\n",
        ])
        ws = _make_ws()
        mock_session = MagicMock()
        mock_session.ws_connect = AsyncMock(return_value=ws)

        stand = Stand(
            path=tmp_path,
            branch="main",
            git_env={"GITHUB_TOKEN": "real-token"},
            cache_env={"GITHUB_TOKEN": "stale-cached"},
        )

        with patch(
            "performer.backends.codex.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=proc),
        ) as mock_exec, patch(
            "performer.backends.codex.aiohttp.ClientSession",
            return_value=mock_session,
        ):
            adapter = CodexBackend()
            rpc_resp = {
                "initialize": {},
                "thread/start": {"thread": {"id": "t1"}},
                "turn/start": {"turn": {"id": "r1"}},
            }
            with patch.object(adapter, "_rpc", side_effect=lambda m, p: rpc_resp[m]):
                await adapter.start(stand, _score())

        env = mock_exec.call_args[1]["env"]
        assert env["GITHUB_TOKEN"] == "real-token"


# ---------------------------------------------------------------------------
# _rpc / _notify
# ---------------------------------------------------------------------------

class TestRpcAndNotify:
    async def test_rpc_sends_request_and_returns_result(self) -> None:
        adapter = CodexBackend()
        ws = _make_ws()

        # Manually resolve the future after send_str
        async def _send_and_resolve(data: str) -> None:
            msg = json.loads(data)
            req_id = msg["id"]
            if req_id in adapter._pending:
                fut = adapter._pending[req_id]
                if not fut.done():
                    fut.set_result({"ok": True})

        ws.send_str = AsyncMock(side_effect=_send_and_resolve)
        adapter._ws = ws

        result = await adapter._rpc("some/method", {"param": 1})
        assert result == {"ok": True}

    async def test_rpc_raises_when_no_ws(self) -> None:
        adapter = CodexBackend()
        adapter._ws = None

        with pytest.raises(RuntimeError, match="WebSocket not connected"):
            await adapter._rpc("some/method", {})

    async def test_notify_sends_without_params(self) -> None:
        adapter = CodexBackend()
        ws = _make_ws()
        adapter._ws = ws

        await adapter._notify("initialized")

        ws.send_str.assert_awaited_once()
        sent = json.loads(ws.send_str.call_args[0][0])
        assert sent["method"] == "initialized"
        assert "params" not in sent

    async def test_notify_sends_with_params(self) -> None:
        adapter = CodexBackend()
        ws = _make_ws()
        adapter._ws = ws

        await adapter._notify("some/method", {"key": "val"})

        sent = json.loads(ws.send_str.call_args[0][0])
        assert sent["params"] == {"key": "val"}

    async def test_notify_noop_when_no_ws(self) -> None:
        adapter = CodexBackend()
        adapter._ws = None
        await adapter._notify("initialized")  # should not raise


# ---------------------------------------------------------------------------
# Additional stop() coverage
# ---------------------------------------------------------------------------

class TestStopAdditional:
    async def test_stop_ws_close_exception_is_swallowed(self) -> None:
        adapter = CodexBackend()
        ws = _make_ws()
        ws.close = AsyncMock(side_effect=RuntimeError("already closed"))
        adapter._ws = ws

        await adapter.stop()  # should not raise

    async def test_stop_wait_timeout_is_swallowed(self) -> None:
        proc = _fake_proc(pid=3333)
        proc.wait = AsyncMock(side_effect=asyncio.TimeoutError())
        adapter = CodexBackend()
        adapter._proc = proc
        adapter._ws = None

        with (
            patch("performer.backends.codex.os.getpgid", return_value=3333),
            patch("performer.backends.codex.os.killpg"),
        ):
            await adapter.stop()  # should not raise

    async def test_stop_psutil_child_no_such_process(self) -> None:
        """Children that disappear mid-kill should be handled gracefully."""
        import psutil as _psutil

        proc = _fake_proc(pid=4444)
        adapter = CodexBackend()
        adapter._proc = proc

        child = MagicMock()
        child.kill = MagicMock(side_effect=_psutil.NoSuchProcess(pid=4445))

        mock_psutil_proc = MagicMock()
        mock_psutil_proc.children.return_value = [child]
        mock_psutil_proc.kill = MagicMock()

        with (
            patch("performer.backends.codex.os.getpgid", side_effect=OSError("no pgid")),
            patch("performer.backends.codex.psutil.Process", return_value=mock_psutil_proc),
        ):
            await adapter.stop()

        mock_psutil_proc.kill.assert_called_once()

    async def test_stop_psutil_no_such_process_for_parent(self) -> None:
        """If the parent process is gone, psutil.NoSuchProcess is handled."""
        import psutil as _psutil

        proc = _fake_proc(pid=5555)
        adapter = CodexBackend()
        adapter._proc = proc

        with (
            patch("performer.backends.codex.os.getpgid", side_effect=OSError("nope")),
            patch(
                "performer.backends.codex.psutil.Process",
                side_effect=_psutil.NoSuchProcess(pid=5555),
            ),
        ):
            await adapter.stop()  # should not raise


# ---------------------------------------------------------------------------
# Additional _recv_loop coverage
# ---------------------------------------------------------------------------

class TestRecvLoopAdditional:
    async def test_cancelled_error_is_reraised(self) -> None:
        adapter = CodexBackend()

        async def _iter():
            raise asyncio.CancelledError()
            yield  # pragma: no cover

        ws = MagicMock()
        ws.__aiter__ = lambda self: _iter()
        adapter._ws = ws

        with pytest.raises(asyncio.CancelledError):
            await adapter._recv_loop()

    async def test_error_status_not_overwritten_if_already_done(self) -> None:
        """If status is already 'done', an exception in recv_loop shouldn't set it to error."""
        adapter = CodexBackend()
        from performer.backends.base import BackendStatus
        adapter._status = BackendStatus(state="done")

        async def _iter():
            raise RuntimeError("late error")
            yield  # pragma: no cover

        ws = MagicMock()
        ws.__aiter__ = lambda self: _iter()
        adapter._ws = ws

        await adapter._recv_loop()
        # Status should remain "done" since it wasn't "working"
        assert adapter.get_status().state == "done"


# ---------------------------------------------------------------------------
# _build_task_prompt
# ---------------------------------------------------------------------------

class TestBuildTaskPrompt:
    def test_includes_title(self) -> None:
        prompt = _build_task_prompt(_score())
        assert "Codex Task" in prompt

    def test_includes_description(self) -> None:
        score = _score(description="Careful work needed")
        prompt = _build_task_prompt(score)
        assert "Careful work needed" in prompt

    def test_includes_acceptance_criteria(self) -> None:
        score = _score(acceptance_criteria=["all tests pass"])
        prompt = _build_task_prompt(score)
        assert "all tests pass" in prompt

    def test_includes_clarifications(self) -> None:
        score = _score(clarifications=[
            {"questions": ["Framework?"], "answer": "FastAPI"}
        ])
        prompt = _build_task_prompt(score)
        assert "Framework?" in prompt
        assert "FastAPI" in prompt

    def test_includes_footer(self) -> None:
        prompt = _build_task_prompt(_score())
        assert "Commit your changes" in prompt
        assert "Do not push or open a pull request — this will be handled automatically" in prompt

    def test_reviewer_role_uses_json_only_footer(self) -> None:
        prompt = _build_task_prompt(_score(role="reviewing"))
        assert "Return ONLY a valid JSON object" in prompt
        assert "Do not include markdown, prose, or code fences." in prompt
        assert "Commit your changes" not in prompt

    def test_reviewer_noun_role_uses_json_only_footer(self) -> None:
        prompt = _build_task_prompt(_score(role="reviewer"))
        assert "Return ONLY a valid JSON object" in prompt
        assert "Commit your changes" not in prompt

    def test_qa_role_includes_verification_contract_hints(self) -> None:
        prompt = _build_task_prompt(_score(role="qa"))
        assert "verification_steps" in prompt
        assert "visual_evidence" in prompt
        assert "visual_validation_required" in prompt
        assert "demo_setup_steps" in prompt
        assert "visual_capture_commands" in prompt
        assert "visual_capture_blockers" in prompt

    def test_persona_not_embedded_in_prompt_body(self) -> None:
        """FR-017: persona is delivered via developerInstructions, not prompt body."""
        score = _score(persona_instructions="PERSONA_MARKER_CODEX be careful.")
        prompt = _build_task_prompt(score)
        assert "PERSONA_MARKER_CODEX" not in prompt
        assert "## Role Instructions" not in prompt

    def test_card_docs_section_emitted_when_folder_exists(self, tmp_path: Path) -> None:
        (tmp_path / "docs" / "cards" / "70-codex-task").mkdir(parents=True)
        score = _score(title="Codex Task", issue_number=70)
        prompt = _build_task_prompt(score, stand_path=tmp_path)
        assert "## Card Documentation" in prompt
        assert "docs/cards/70-codex-task/" in prompt

    def test_card_docs_section_omitted_when_folder_missing(self, tmp_path: Path) -> None:
        prompt = _build_task_prompt(_score(issue_number=70), stand_path=tmp_path)
        assert "## Card Documentation" not in prompt


class TestTomlQuote:
    def test_quotes_safe_value(self) -> None:
        assert _toml_quote("https://api.example/v1") == '"https://api.example/v1"'

    @pytest.mark.parametrize("bad", ['has"quote', "has\\bslash", "has\nnewline", "has\rreturn"])
    def test_rejects_injection_chars(self, bad: str) -> None:
        with pytest.raises(ValueError, match="unsafe character"):
            _toml_quote(bad)

    def test_validate_bare_key_accepts_safe(self) -> None:
        _validate_toml_bare_key("coordinare_litellm-1")

    @pytest.mark.parametrize("bad", ["with space", "with.dot", "with=eq", "with\"quote", ""])
    def test_validate_bare_key_rejects_unsafe(self, bad: str) -> None:
        with pytest.raises(ValueError, match="unsafe codex provider name"):
            _validate_toml_bare_key(bad)
