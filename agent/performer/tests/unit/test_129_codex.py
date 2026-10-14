"""Spec 129: CodexBackend run-transcript records.

Drives the real notification handler and the start/relay_feedback paths with
realistic app-server payloads (item ids, statuses and usage shapes taken from
the codex app-server v2 schema). The transport (subprocess, WebSocket) is
mocked; the transcript buffer is a real TranscriptBuffer.
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from performer import transcript
from performer.backends.codex import CodexBackend
from performer.models import Score, Stand

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

_LONG_OUTPUT = "pytest line\n" * 400  # well over the 200-char BackendEvent summary
_LONG_MESSAGE = "The fix is in place. " * 200
_GITHUB_TOKEN = "ghp_" + "A" * 36


@pytest.fixture
def buf() -> Iterator[transcript.TranscriptBuffer]:
    buffer = transcript.TranscriptBuffer("run", max_bytes=10_000_000)
    transcript.set_active_buffer(buffer)
    try:
        yield buffer
    finally:
        transcript.set_active_buffer(None)


def _records(buffer: transcript.TranscriptBuffer) -> list[dict[str, Any]]:
    return buffer.after(0, 10_000)


def _types(buffer: transcript.TranscriptBuffer) -> list[str]:
    return [r["type"] for r in _records(buffer)]


def _notify(adapter: CodexBackend, method: str, params: dict[str, Any]) -> None:
    adapter._handle_notification(method, params)


def _command_item(status: str, output: str | None, exit_code: int | None) -> dict[str, Any]:
    return {
        "type": "commandExecution",
        "id": "call_shell_1",
        "command": "/bin/zsh -lc 'python -m pytest -q'",
        "cwd": "/work/stand",
        "commandActions": [],
        "status": status,
        "aggregatedOutput": output,
        "exitCode": exit_code,
        "durationMs": 812,
        "source": "agent",
    }


def _usage_total() -> dict[str, Any]:
    return {
        "tokenUsage": {
            "total": {
                "inputTokens": 5210,
                "cachedInputTokens": 3000,
                "cacheWriteInputTokens": 120,
                "outputTokens": 840,
                "reasoningOutputTokens": 310,
                "totalTokens": 6050,
            },
            "last": {
                "inputTokens": 5210,
                "cachedInputTokens": 3000,
                "cacheWriteInputTokens": 120,
                "outputTokens": 840,
                "reasoningOutputTokens": 310,
                "totalTokens": 6050,
            },
            "modelContextWindow": 200000,
        },
        "threadId": "thread-001",
        "turnId": "turn-001",
    }


class TestTurnRecords:
    def test_full_turn_record_sequence(self, buf: transcript.TranscriptBuffer) -> None:
        adapter = CodexBackend()
        _notify(adapter, "item/started", {"item": _command_item("inProgress", None, None)})
        _notify(adapter, "item/completed", {
            "item": _command_item("completed", _LONG_OUTPUT, 0),
        })
        _notify(adapter, "item/completed", {"item": {
            "type": "agentMessage", "id": "msg_1", "text": _LONG_MESSAGE,
        }})
        _notify(adapter, "thread/tokenUsage/updated", _usage_total())
        _notify(adapter, "turn/completed", {"turn": {
            "id": "turn-001", "status": "completed", "items": [], "usage": {"totalTokens": 6050},
        }})

        assert _types(buf) == [
            "tool_call", "tool_result", "assistant_text", "usage", "result",
        ]

    def test_long_text_is_not_truncated(self, buf: transcript.TranscriptBuffer) -> None:
        adapter = CodexBackend()
        _notify(adapter, "item/completed", {"item": {
            "type": "agentMessage", "id": "msg_1", "text": _LONG_MESSAGE,
        }})
        _notify(adapter, "item/completed", {"item": _command_item(
            "completed", _LONG_OUTPUT, 0,
        )})

        records = _records(buf)
        assert records[0]["text"] == _LONG_MESSAGE
        assert len(records[0]["text"]) > 200
        assert records[2]["output"]["aggregatedOutput"] == _LONG_OUTPUT

    def test_tool_call_and_result_share_call_id(self, buf: transcript.TranscriptBuffer) -> None:
        adapter = CodexBackend()
        _notify(adapter, "item/started", {"item": _command_item("inProgress", None, None)})
        _notify(adapter, "item/completed", {"item": _command_item("completed", "ok\n", 0)})

        call, result = _records(buf)
        assert call["type"] == "tool_call"
        assert result["type"] == "tool_result"
        assert call["tool_name"] == "shell"
        assert call["call_id"] == "call_shell_1"
        assert result["call_id"] == "call_shell_1"
        assert call["input"]["command"] == "/bin/zsh -lc 'python -m pytest -q'"
        assert result["output"]["aggregatedOutput"] == "ok\n"
        assert result["is_error"] is False

    def test_failed_command_result_is_error(self, buf: transcript.TranscriptBuffer) -> None:
        adapter = CodexBackend()
        _notify(adapter, "item/completed", {"item": _command_item("failed", "boom\n", 1)})

        call, result = _records(buf)
        assert call["type"] == "tool_call"
        assert result["type"] == "tool_result"
        assert result["is_error"] is True
        assert result["output"]["exitCode"] == 1

    def test_completion_without_started_still_records_call(
        self, buf: transcript.TranscriptBuffer,
    ) -> None:
        adapter = CodexBackend()
        _notify(adapter, "item/completed", {"item": {
            "type": "fileChange",
            "id": "patch_1",
            "status": "declined",
            "changes": [{"path": "src/a.py", "kind": {"type": "update"}, "diff": "+x\n"}],
        }})

        assert _types(buf) == ["tool_call", "tool_result"]
        call, result = _records(buf)
        assert call["tool_name"] == "file_change"
        assert result["call_id"] == "patch_1"
        assert result["is_error"] is True
        assert result["output"]["changes"][0]["diff"] == "+x\n"

    def test_mcp_tool_name_and_error_flag(self, buf: transcript.TranscriptBuffer) -> None:
        adapter = CodexBackend()
        _notify(adapter, "item/started", {"item": {
            "type": "mcpToolCall", "id": "mcp_1", "server": "docs", "tool": "search",
            "arguments": {"query": "widgets"}, "status": "inProgress",
        }})
        _notify(adapter, "item/completed", {"item": {
            "type": "mcpToolCall", "id": "mcp_1", "server": "docs", "tool": "search",
            "arguments": {"query": "widgets"}, "status": "failed",
            "error": {"message": "server unavailable"},
        }})

        call, result = _records(buf)
        assert call["tool_name"] == "search"
        assert call["input"]["arguments"] == {"query": "widgets"}
        assert result["call_id"] == "mcp_1"
        assert result["is_error"] is True

    def test_reasoning_deltas_assembled_into_thinking(
        self, buf: transcript.TranscriptBuffer,
    ) -> None:
        adapter = CodexBackend()
        for delta in ("First, ", "read the failing test. ", "Then patch."):
            _notify(adapter, "item/reasoning/textDelta", {
                "delta": delta, "itemId": "rs_1", "contentIndex": 0,
            })
        _notify(adapter, "item/completed", {"item": {
            "type": "reasoning", "id": "rs_1", "content": [], "summary": [],
        }})

        thinking = _records(buf)
        assert [r["type"] for r in thinking] == ["thinking"]
        assert thinking[0]["text"] == "First, read the failing test. Then patch."

    def test_reasoning_item_content_preferred_over_deltas(
        self, buf: transcript.TranscriptBuffer,
    ) -> None:
        adapter = CodexBackend()
        _notify(adapter, "item/reasoning/textDelta", {
            "delta": "partial", "itemId": "rs_2", "contentIndex": 0,
        })
        _notify(adapter, "item/completed", {"item": {
            "type": "reasoning", "id": "rs_2", "content": ["full reasoning text"], "summary": [],
        }})

        assert _records(buf)[0]["text"] == "full reasoning text"

    def test_unfinished_reasoning_flushed_at_turn_end(
        self, buf: transcript.TranscriptBuffer,
    ) -> None:
        adapter = CodexBackend()
        _notify(adapter, "item/reasoning/textDelta", {
            "delta": "interrupted thought", "itemId": "rs_3", "contentIndex": 0,
        })
        _notify(adapter, "turn/completed", {"turn": {
            "id": "turn-001", "status": "interrupted", "items": [],
            "error": {"message": "stopped by user"},
        }})

        assert _types(buf) == ["thinking", "error", "result"]
        assert _records(buf)[0]["text"] == "interrupted thought"


class TestUsageAndTurnEnd:
    def test_usage_is_one_run_total_record(self, buf: transcript.TranscriptBuffer) -> None:
        adapter = CodexBackend()
        _notify(adapter, "thread/tokenUsage/updated", _usage_total())
        _notify(adapter, "turn/completed", {"turn": {
            "id": "turn-001", "status": "completed", "items": [],
        }})

        usage = _records(buf)[0]
        assert usage["type"] == "usage"
        assert "run total" in usage["text"]
        # 134 review: the app-server's inputTokens already includes cachedInputTokens
        # (totalTokens = inputTokens + outputTokens = 6050), so the fresh input is the
        # difference. Codex has no cache-write field, so cache_write is not reported.
        assert usage["usage"]["input_tokens"] == 5210 - 3000
        assert usage["usage"]["output_tokens"] == 840
        assert usage["usage"]["cache_read"] == 3000
        assert "cache_write" not in usage["usage"]
        kinds = usage["usage"]["input_tokens"] + usage["usage"]["cache_read"] + usage["usage"]["output_tokens"]
        assert kinds == 6050  # the kinds add up to the app-server's own total
        assert "model" not in usage["usage"]
        assert "totalTokens=6050" in usage["text"]

    def test_no_usage_record_without_token_notification(
        self, buf: transcript.TranscriptBuffer,
    ) -> None:
        adapter = CodexBackend()
        _notify(adapter, "turn/completed", {"turn": {
            "id": "turn-001", "status": "completed", "items": [],
        }})

        assert _types(buf) == ["result"]

    def test_completed_turn_result_carries_final_text(
        self, buf: transcript.TranscriptBuffer,
    ) -> None:
        adapter = CodexBackend()
        _notify(adapter, "item/agentMessage/delta", {"delta": "all green", "itemId": "msg_1"})
        _notify(adapter, "turn/completed", {"turn": {
            "id": "turn-001", "status": "completed", "items": [],
        }})

        result = _records(buf)[-1]
        assert result["type"] == "result"
        assert result["text"] == "all green"
        assert result["is_error"] is False
        assert result["input"] == {"status": "completed"}

    def test_failed_turn_emits_error_and_error_result(
        self, buf: transcript.TranscriptBuffer,
    ) -> None:
        adapter = CodexBackend()
        failure = "model returned 502: upstream connect error " + "x" * 300
        _notify(adapter, "turn/completed", {"turn": {
            "id": "turn-001", "status": "failed", "items": [],
            "error": {"message": failure},
        }})

        assert _types(buf) == ["error", "result"]
        error, result = _records(buf)
        assert error["text"] == failure
        assert result["is_error"] is True
        assert result["input"] == {"status": "failed"}

    def test_error_notification_full_message_nonfatal(
        self, buf: transcript.TranscriptBuffer,
    ) -> None:
        adapter = CodexBackend()
        message = "rate limited, retrying: " + "y" * 300
        _notify(adapter, "error", {
            "error": {"message": message}, "willRetry": True,
            "threadId": "thread-001", "turnId": "turn-001",
        })

        record = _records(buf)[0]
        assert record["type"] == "error"
        assert record["text"] == message
        assert adapter.get_status().state != "error"


class TestRedaction:
    def test_secret_in_output_is_redacted(self, buf: transcript.TranscriptBuffer) -> None:
        adapter = CodexBackend()
        output = f"cloning with {_GITHUB_TOKEN}\n" + _LONG_OUTPUT
        _notify(adapter, "item/completed", {"item": _command_item("completed", output, 0)})

        result = _records(buf)[-1]
        dumped = repr(result)
        assert _GITHUB_TOKEN not in dumped
        assert "[REDACTED]" in result["output"]["aggregatedOutput"]
        assert result["output"]["aggregatedOutput"].endswith(_LONG_OUTPUT)


class TestStartAndRelay:
    @staticmethod
    @contextmanager
    def _patched_start(
        adapter: CodexBackend, rpc_responses: dict[str, Any],
    ) -> Iterator[None]:
        proc = MagicMock()
        proc.pid = 9999
        proc.returncode = None
        mock_session = MagicMock()
        ws = MagicMock()
        ws.send_str = AsyncMock()
        mock_session.ws_connect = AsyncMock(return_value=ws)

        async def _mock_rpc(method: str, params: dict[str, Any]) -> dict[str, Any]:
            return rpc_responses[method]

        with ExitStack() as stack:
            stack.enter_context(patch(
                "performer.backends.codex.asyncio.create_subprocess_exec",
                new=AsyncMock(return_value=proc),
            ))
            stack.enter_context(
                patch("performer.backends.codex.aiohttp.ClientSession", return_value=mock_session),
            )
            stack.enter_context(patch.object(CodexBackend, "_wait_for_ready", new=AsyncMock()))
            stack.enter_context(patch.object(CodexBackend, "_drain_logs", new=AsyncMock()))
            stack.enter_context(patch.object(CodexBackend, "_recv_loop", new=AsyncMock()))
            stack.enter_context(patch.object(adapter, "_rpc", side_effect=_mock_rpc))
            yield

    async def test_start_emits_capability_once_then_system_and_user(
        self, tmp_path: Path, buf: transcript.TranscriptBuffer,
    ) -> None:
        adapter = CodexBackend()
        thread_resp = {"thread": {"id": "thread-start-001"}, "model": "gpt-test"}
        score = Score(
            title="Codex Task",
            repo_url="https://github.com/org/repo",
            branch="main",
            github_token="tok",
            description="Fix the parser. " * 40,
        )
        with self._patched_start(adapter, {
            "initialize": {},
            "thread/start": thread_resp,
            "turn/start": {"turn": {"id": "turn-start-001"}},
        }):
            await adapter.start(Stand(path=tmp_path, branch="main"), score)

        records = _records(buf)
        types = [r["type"] for r in records]
        assert types == ["capability", "system", "user"]
        assert types.count("capability") == 1

        capability = records[0]
        assert capability["input"]["unavailable"] == []
        assert "thinking" in capability["input"]["provided"]
        assert "usage" in capability["input"]["provided"]

        system = records[1]
        assert system["input"] == thread_resp
        assert "thread-start-001" in system["text"]

        user = records[2]
        assert user["text"].startswith("# Task: Codex Task")
        assert len(user["text"]) > 200
        assert ("Fix the parser. " * 40).strip() in user["text"]

    async def test_relay_feedback_emits_full_user_text_without_capability(
        self, tmp_path: Path, buf: transcript.TranscriptBuffer,
    ) -> None:
        adapter = CodexBackend()
        adapter._thread_id = "thread-001"
        adapter._ws = MagicMock()
        feedback = "Reviewer says: " + "please also cover the edge case. " * 20

        with patch.object(
            adapter, "_rpc", new=AsyncMock(return_value={"turn": {"id": "turn-002"}}),
        ):
            await adapter.relay_feedback(feedback)

        records = _records(buf)
        assert [r["type"] for r in records] == ["user"]
        assert records[0]["text"] == feedback

    async def test_capability_is_not_repeated_across_start_and_relay(
        self, tmp_path: Path, buf: transcript.TranscriptBuffer,
    ) -> None:
        adapter = CodexBackend()
        with self._patched_start(adapter, {
            "initialize": {},
            "thread/start": {"thread": {"id": "thread-start-002"}},
            "turn/start": {"turn": {"id": "turn-start-002"}},
        }):
            await adapter.start(Stand(path=tmp_path, branch="main"), Score(
                title="T", repo_url="https://github.com/org/repo", branch="main",
                github_token="tok",
            ))
            with patch.object(
                adapter, "_rpc", new=AsyncMock(return_value={"turn": {"id": "turn-3"}}),
            ):
                await adapter.relay_feedback("again")

        types = [r["type"] for r in _records(buf)]
        assert types.count("capability") == 1
        assert types.count("user") == 2

    async def test_combined_run_emits_every_declared_provided_type(
        self, buf: transcript.TranscriptBuffer,
    ) -> None:
        adapter = CodexBackend()
        transcript.emit("capability", text="declared", input_data={
            "unavailable": [],
            "provided": [
                "system", "user", "assistant_text", "thinking", "tool_call",
                "tool_result", "usage", "result", "error",
            ],
        })
        transcript.emit("system", text="thread")
        transcript.emit("user", text="task")
        _notify(adapter, "item/reasoning/textDelta", {"delta": "hm", "itemId": "rs_9"})
        _notify(adapter, "item/completed", {"item": {
            "type": "reasoning", "id": "rs_9", "content": ["hm"], "summary": [],
        }})
        _notify(adapter, "item/started", {"item": _command_item("inProgress", None, None)})
        _notify(adapter, "item/completed", {"item": _command_item("completed", "ok", 0)})
        _notify(adapter, "item/completed", {"item": {
            "type": "agentMessage", "id": "msg_9", "text": "done",
        }})
        _notify(adapter, "error", {"error": {"message": "transient"}, "willRetry": True})
        _notify(adapter, "thread/tokenUsage/updated", _usage_total())
        _notify(adapter, "turn/completed", {"turn": {
            "id": "turn-001", "status": "completed", "items": [],
        }})

        emitted = set(_types(buf))
        assert emitted >= {
            "system", "user", "assistant_text", "thinking", "tool_call",
            "tool_result", "usage", "result", "error",
        }
        assert _types(buf).count("capability") == 1


class TestNoBuffer:
    def test_handlers_are_noops_without_active_buffer(self) -> None:
        assert transcript.transcript_enabled() is False
        adapter = CodexBackend()
        _notify(adapter, "item/completed", {"item": _command_item("completed", "ok", 0)})
        _notify(adapter, "turn/completed", {"turn": {
            "id": "turn-001", "status": "completed", "items": [],
        }})
        assert adapter.get_status().state == "done"
