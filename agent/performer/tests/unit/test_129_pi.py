"""Spec 129: PiBackend run-transcript records.

Events follow the pi ``--mode json`` schema (pi 0.73.1 docs/json.md and the
pi-ai AssistantMessage/Usage types): ``message_end`` carries the complete
assistant message, ``tool_execution_start``/``_end`` carry toolCallId, args and
result. The start/relay tests feed a real asyncio.StreamReader through the real
``_read_loop``; only the subprocess itself is faked.
"""
from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from performer import transcript
from performer.backends.pi import PiBackend
from performer.models import Score, Stand

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

_MODEL = "example-provider/test-model"
_LONG_OUTPUT = "PASSED test_case_line\n" * 300  # far beyond the 200-char summary
_LONG_THINKING = "Plan: read the parser, then patch the off-by-one. " * 30
_LONG_TEXT = "Patched the off-by-one in parse_line; the suite is green. " * 30
_GITHUB_TOKEN = "ghp_" + "B" * 36


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


def _usage(input_tokens: int = 1200, output_tokens: int = 310) -> dict[str, Any]:
    return {
        "input": input_tokens,
        "output": output_tokens,
        "cacheRead": 0,
        "cacheWrite": 0,
        "totalTokens": input_tokens + output_tokens,
        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0, "total": 0},
    }


def _assistant(
    content: list[dict[str, Any]],
    *,
    stop: str = "toolUse",
    usage: dict[str, Any] | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    message: dict[str, Any] = {
        "role": "assistant",
        "content": content,
        "api": "openai-completions",
        "provider": "litellm",
        "model": _MODEL,
        "stopReason": stop,
        "timestamp": 1_750_000_000_000,
    }
    if usage is not None:
        message["usage"] = usage
    if error is not None:
        message["errorMessage"] = error
    return message


def _tool_result_payload(output: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": output}], "details": {}}


def _feed(adapter: PiBackend, events: list[dict[str, Any]]) -> None:
    for event in events:
        adapter._handle_event(event)


class TestEventRecords:
    def test_full_tool_turn_record_sequence(self, buf: transcript.TranscriptBuffer) -> None:
        adapter = PiBackend()
        _feed(adapter, [
            {"type": "session", "version": 3, "id": "sess-1", "cwd": "/work/stand"},
            {"type": "agent_start"},
            {"type": "turn_start"},
            {"type": "message_end", "message": _assistant([
                {"type": "thinking", "thinking": _LONG_THINKING},
                {"type": "text", "text": "Running the suite first."},
                {"type": "toolCall", "id": "call_bash_1", "name": "bash",
                 "arguments": {"command": "pytest -q"}},
            ], usage=_usage())},
            {"type": "tool_execution_start", "toolCallId": "call_bash_1",
             "toolName": "bash", "args": {"command": "pytest -q"}},
            {"type": "tool_execution_end", "toolCallId": "call_bash_1", "toolName": "bash",
             "result": _tool_result_payload(_LONG_OUTPUT), "isError": False},
            {"type": "message_end", "message": _assistant(
                [{"type": "text", "text": _LONG_TEXT}], stop="stop", usage=_usage(1500, 90),
            )},
            {"type": "agent_end", "messages": [
                {"role": "user", "content": "task"},
                _assistant([{"type": "text", "text": _LONG_TEXT}], stop="stop"),
            ]},
        ])

        assert _types(buf) == [
            "system", "system",
            "thinking", "assistant_text", "usage",
            "tool_call", "tool_result",
            "assistant_text", "usage",
            "result",
        ]

    def test_full_text_thinking_and_output_not_truncated(
        self, buf: transcript.TranscriptBuffer,
    ) -> None:
        adapter = PiBackend()
        _feed(adapter, [
            {"type": "message_end", "message": _assistant([
                {"type": "thinking", "thinking": _LONG_THINKING},
                {"type": "text", "text": _LONG_TEXT},
            ])},
            {"type": "tool_execution_start", "toolCallId": "call_1", "toolName": "bash",
             "args": {"command": "pytest -q"}},
            {"type": "tool_execution_end", "toolCallId": "call_1", "toolName": "bash",
             "result": _tool_result_payload(_LONG_OUTPUT), "isError": False},
        ])

        thinking, text, call, result = _records(buf)
        assert thinking["text"] == _LONG_THINKING
        assert text["text"] == _LONG_TEXT
        assert len(text["text"]) > 200
        assert call["input"] == {"command": "pytest -q"}
        assert result["output"] == _tool_result_payload(_LONG_OUTPUT)

    def test_tool_call_and_result_pair_on_tool_call_id(
        self, buf: transcript.TranscriptBuffer,
    ) -> None:
        adapter = PiBackend()
        _feed(adapter, [
            {"type": "tool_execution_start", "toolCallId": "call_x", "toolName": "read",
             "args": {"path": "src/a.py"}},
            {"type": "tool_execution_start", "toolCallId": "call_y", "toolName": "bash",
             "args": {"command": "ls"}},
            {"type": "tool_execution_end", "toolCallId": "call_y", "toolName": "bash",
             "result": _tool_result_payload("a.py\n"), "isError": True},
            {"type": "tool_execution_end", "toolCallId": "call_x", "toolName": "read",
             "result": _tool_result_payload("print(1)\n"), "isError": False},
        ])

        calls = [r for r in _records(buf) if r["type"] == "tool_call"]
        results = [r for r in _records(buf) if r["type"] == "tool_result"]
        assert [c["call_id"] for c in calls] == ["call_x", "call_y"]
        assert [r["call_id"] for r in results] == ["call_y", "call_x"]
        assert calls[0]["tool_name"] == "read"
        assert results[0]["is_error"] is True
        assert results[1]["is_error"] is False

    def test_only_assistant_messages_are_recorded_from_message_end(
        self, buf: transcript.TranscriptBuffer,
    ) -> None:
        adapter = PiBackend()
        _feed(adapter, [
            {"type": "message_end", "message": {"role": "user", "content": "task text"}},
            {"type": "message_end", "message": {
                "role": "toolResult", "toolCallId": "call_1", "toolName": "bash",
                "content": [{"type": "text", "text": "out"}], "isError": False,
            }},
        ])

        assert _types(buf) == []


class TestUsage:
    def test_usage_is_per_model_call_with_real_counts(
        self, buf: transcript.TranscriptBuffer,
    ) -> None:
        adapter = PiBackend()
        _feed(adapter, [
            {"type": "message_end", "message": _assistant(
                [{"type": "text", "text": "ok"}], stop="stop", usage=_usage(1200, 310),
            )},
        ])

        usage = next(r for r in _records(buf) if r["type"] == "usage")
        assert usage["usage"] == {
            "input_tokens": 1200,
            "output_tokens": 310,
            "cache_read": 0,
            "cache_write": 0,
            "model": _MODEL,
        }
        assert "one assistant message" in usage["text"]

    def test_no_usage_record_when_stream_has_no_usage(
        self, buf: transcript.TranscriptBuffer,
    ) -> None:
        adapter = PiBackend()
        _feed(adapter, [
            {"type": "message_end", "message": _assistant(
                [{"type": "text", "text": "ok"}], stop="stop",
            )},
        ])

        assert _types(buf) == ["assistant_text"]


class TestErrorsAndResult:
    def test_failed_model_turn_emits_full_error_message(
        self, buf: transcript.TranscriptBuffer,
    ) -> None:
        adapter = PiBackend()
        failure = "provider 502 from upstream: " + "detail " * 60
        _feed(adapter, [
            {"type": "message_end", "message": _assistant(
                [], stop="error", error=failure,
            )},
        ])

        error = _records(buf)[0]
        assert error["type"] == "error"
        assert error["text"] == failure
        assert error["input"] == {"stop_reason": "error"}

    def test_auto_retry_start_is_a_nonfatal_error_record(
        self, buf: transcript.TranscriptBuffer,
    ) -> None:
        adapter = PiBackend()
        retry_reason = "rate limit exceeded, retry scheduled " + "z" * 250
        _feed(adapter, [{
            "type": "auto_retry_start", "attempt": 1, "maxAttempts": 3,
            "delayMs": 2000, "errorMessage": retry_reason,
        }])

        record = _records(buf)[0]
        assert record["type"] == "error"
        assert record["text"] == retry_reason
        assert record["input"]["attempt"] == 1
        assert adapter.get_status().state != "error"

    def test_agent_end_result_carries_final_text_and_stop_reason(
        self, buf: transcript.TranscriptBuffer,
    ) -> None:
        adapter = PiBackend()
        _feed(adapter, [{"type": "agent_end", "messages": [
            {"role": "user", "content": "task"},
            _assistant([{"type": "text", "text": _LONG_TEXT}], stop="stop"),
        ]}])

        result = _records(buf)[0]
        assert result["type"] == "result"
        assert result["text"] == _LONG_TEXT
        assert result["input"] == {"stop_reason": "stop"}
        assert result["is_error"] is False
        assert adapter.get_status().state == "done"

    def test_agent_end_after_failed_turn_is_error_result(
        self, buf: transcript.TranscriptBuffer,
    ) -> None:
        adapter = PiBackend()
        _feed(adapter, [{"type": "agent_end", "messages": [
            _assistant([], stop="aborted", error="user abort"),
        ]}])

        result = _records(buf)[0]
        assert result["is_error"] is True
        assert result["input"] == {"stop_reason": "aborted"}


class TestRedaction:
    def test_secret_in_tool_output_is_redacted(self, buf: transcript.TranscriptBuffer) -> None:
        adapter = PiBackend()
        output = f"remote set to https://{_GITHUB_TOKEN}@github.com/org/repo\n" + _LONG_OUTPUT
        _feed(adapter, [
            {"type": "tool_execution_end", "toolCallId": "call_r", "toolName": "bash",
             "result": _tool_result_payload(output), "isError": False},
        ])

        result = _records(buf)[0]
        assert _GITHUB_TOKEN not in repr(result)
        assert "[REDACTED]" in result["output"]["content"][0]["text"]
        assert result["output"]["content"][0]["text"].endswith(_LONG_OUTPUT)


class TestStartAndRelay:
    @staticmethod
    def _stream(lines: list[dict[str, Any]], *, returncode: int) -> tuple[MagicMock, asyncio.StreamReader]:
        reader = asyncio.StreamReader()
        for line in lines:
            reader.feed_data((json.dumps(line) + "\n").encode())
        reader.feed_eof()
        proc = MagicMock()
        proc.pid = 4321
        proc.returncode = returncode
        proc.stdout = reader
        proc.wait = AsyncMock(return_value=returncode)
        return proc, reader

    async def test_start_emits_capability_once_and_user_then_stream(
        self, tmp_path: Path, buf: transcript.TranscriptBuffer, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("PI_PROVIDER_BASE_URL", raising=False)
        proc, _ = self._stream([
            {"type": "session", "version": 3, "id": "sess-2", "cwd": str(tmp_path)},
            {"type": "agent_start"},
            {"type": "message_end", "message": _assistant(
                [{"type": "text", "text": _LONG_TEXT}], stop="stop", usage=_usage(),
            )},
            {"type": "agent_end", "messages": [
                _assistant([{"type": "text", "text": _LONG_TEXT}], stop="stop"),
            ]},
        ], returncode=0)
        score = Score(
            title="Pi Task",
            repo_url="https://github.com/org/repo",
            branch="main",
            github_token="tok",
            description="Fix the parser. " * 40,
        )
        adapter = PiBackend()
        create = AsyncMock(return_value=proc)
        with patch("performer.backends.pi.asyncio.create_subprocess_exec", new=create):
            await adapter.start(Stand(path=tmp_path, branch="main"), score)
            assert adapter._reader_task is not None
            await adapter._reader_task

        types = _types(buf)
        assert types[0] == "capability"
        assert types.count("capability") == 1
        assert types[1] == "user"
        assert types[2:] == ["system", "system", "assistant_text", "usage", "result"]

        capability = _records(buf)[0]
        assert capability["input"]["unavailable"] == []
        assert "usage" in capability["input"]["provided"]

        user = _records(buf)[1]
        assert user["text"].startswith("# Task: Pi Task")
        assert ("Fix the parser. " * 40).strip() in user["text"]

    async def test_relay_feedback_emits_full_user_text_without_capability(
        self, tmp_path: Path, buf: transcript.TranscriptBuffer, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.delenv("PI_PROVIDER_BASE_URL", raising=False)
        first, _ = self._stream([{"type": "agent_start"}], returncode=0)
        second, _ = self._stream([
            {"type": "agent_end", "messages": [
                _assistant([{"type": "text", "text": "fixed"}], stop="stop"),
            ]},
        ], returncode=0)
        score = Score(
            title="Pi Task", repo_url="https://github.com/org/repo", branch="main",
            github_token="tok",
        )
        feedback = "Reviewer says: " + "cover the empty-input case. " * 20
        adapter = PiBackend()
        with patch(
            "performer.backends.pi.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=first),
        ):
            await adapter.start(Stand(path=tmp_path, branch="main"), score)
            assert adapter._reader_task is not None
            await adapter._reader_task
        with patch(
            "performer.backends.pi.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=second),
        ):
            await adapter.relay_feedback(feedback)
            assert adapter._reader_task is not None
            await adapter._reader_task

        records = _records(buf)
        assert [r["type"] for r in records].count("capability") == 1
        user_records = [r for r in records if r["type"] == "user"]
        assert [u["text"] for u in user_records][1] == feedback

    async def test_stream_end_without_terminal_event_records_inferred_result(
        self, tmp_path: Path, buf: transcript.TranscriptBuffer,
    ) -> None:
        proc, _ = self._stream([
            {"type": "message_end", "message": _assistant(
                [{"type": "text", "text": "partial answer"}], stop="stop",
            )},
        ], returncode=2)
        adapter = PiBackend()
        adapter._proc = proc
        await adapter._read_loop()

        result = next(r for r in _records(buf) if r["type"] == "result")
        assert result["text"] == "partial answer"
        assert result["is_error"] is True
        assert result["input"] == {"stop_reason": None, "inferred_from_exit_code": 2}
        assert adapter.get_status().state == "error"


class TestNoBuffer:
    def test_handlers_are_noops_without_active_buffer(self) -> None:
        assert transcript.transcript_enabled() is False
        adapter = PiBackend()
        _feed(adapter, [
            {"type": "agent_end", "messages": [
                _assistant([{"type": "text", "text": "done"}], stop="stop"),
            ]},
        ])
        assert adapter.get_status().state == "done"
