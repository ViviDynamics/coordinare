"""Spec 129: Claude Code stream-json events map to full-fidelity transcript records."""
from __future__ import annotations

from collections.abc import Iterator

import pytest

from performer import transcript
from performer.backends.claude_code import ClaudeCodeBackend

GH_TOKEN = "ghp_" + "b" * 36
LONG_OUTPUT = "line of tool output\n" * 6000  # about 120 KB, far past the 200-char summary cap


@pytest.fixture
def buffer() -> Iterator[transcript.TranscriptBuffer]:
    buf = transcript.TranscriptBuffer("run-cc", max_bytes=10_000_000)
    transcript.set_active_buffer(buf)
    yield buf
    transcript.set_active_buffer(None)


def _feed(backend: ClaudeCodeBackend, events: list[dict]) -> None:
    for event in events:
        backend._handle_event(event)


def _assistant(message_id: str, content: list[dict], output_tokens: int = 5) -> dict:
    return {
        "type": "assistant",
        "message": {
            "id": message_id,
            "model": "claude-sonnet-test",
            "content": content,
            "usage": {
                "input_tokens": 10,
                "output_tokens": output_tokens,
                "cache_read_input_tokens": 100,
                "cache_creation_input_tokens": 7,
            },
        },
    }


def test_full_run_is_recorded_in_order(buffer: transcript.TranscriptBuffer) -> None:
    backend = ClaudeCodeBackend()
    _feed(backend, [
        {"type": "system", "subtype": "init", "session_id": "s1", "model": "claude-sonnet-test"},
        _assistant("m1", [
            {"type": "thinking", "thinking": "plan the edit"},
            {"type": "text", "text": "I will list the files."},
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls -la"}},
        ]),
        # Second content block of the same message: usage must not be counted twice.
        # Later chunk of the same message, with the grown output count.
        _assistant("m1", [{"type": "text", "text": "Listing done."}], output_tokens=40),
        {
            "type": "user",
            "message": {"content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": LONG_OUTPUT,
                 "is_error": False},
            ]},
        },
        {
            "type": "result",
            "subtype": "success",
            "result": "final answer",
            "is_error": False,
            "stop_reason": "end_turn",
            "num_turns": 2,
            "total_cost_usd": 0.01,
            "usage": {"input_tokens": 10, "output_tokens": 5,
                      "cache_read_input_tokens": 100, "cache_creation_input_tokens": 7},
            "modelUsage": {"claude-sonnet-test": {}},
            "session_id": "s1",
        },
    ])
    records = buffer.after(0, 100)
    types = [r["type"] for r in records]
    assert types == [
        "system",
        "thinking",
        "assistant_text",
        "tool_call",
        "assistant_text",
        "tool_result",
        "usage",
        "usage",
        "result",
    ]
    assert [r["seq"] for r in records] == list(range(1, len(records) + 1))

    call = next(r for r in records if r["type"] == "tool_call")
    assert call["tool_name"] == "Bash"
    assert call["call_id"] == "t1"
    assert call["input"] == {"command": "ls -la"}

    result_rec = next(r for r in records if r["type"] == "tool_result")
    assert result_rec["call_id"] == "t1"
    assert result_rec["tool_name"] == "Bash"
    assert result_rec["output"] == LONG_OUTPUT
    assert result_rec["is_error"] is False

    # One message-scope record per message id, holding the LAST chunk's usage.
    # The first chunk reported output_tokens=5; keeping it would undercount to 5.
    per_message, run_total = [r for r in records if r["type"] == "usage"]
    assert per_message["scope"] == "message"
    assert per_message["usage"] == {
        "input_tokens": 10,
        "output_tokens": 40,
        "cache_read": 100,
        "cache_write": 7,
        "model": "claude-sonnet-test",
    }
    assert run_total["scope"] == "run"

    final = records[-1]
    assert final["output"]["result"] == "final answer"
    assert final["output"]["stop_reason"] == "end_turn"


def test_error_result_and_api_retry_are_recorded(buffer: transcript.TranscriptBuffer) -> None:
    backend = ClaudeCodeBackend()
    _feed(backend, [
        {"type": "system", "subtype": "api_retry", "error_status": 529, "error": "overloaded"},
        {"type": "result", "subtype": "error", "error": "tool crashed hard"},
    ])
    records = buffer.after(0, 100)
    kinds = [(r["type"], r.get("text")) for r in records]
    assert ("error", "upstream api_retry (overloaded)") in kinds
    assert ("error", "tool crashed hard") in kinds
    assert any(r["type"] == "result" and r["is_error"] is True for r in records)


def test_secrets_in_tool_input_and_text_are_redacted(buffer: transcript.TranscriptBuffer) -> None:
    backend = ClaudeCodeBackend()
    _feed(backend, [
        _assistant("m2", [
            {"type": "text", "text": f"using {GH_TOKEN} for the push"},
            {"type": "tool_use", "id": "t9", "name": "Bash",
             "input": {"command": f"git push https://x:{GH_TOKEN}@github.com/a/b"}},
        ]),
    ])
    blob = repr(buffer.after(0, 100))
    assert GH_TOKEN not in blob
    assert "[REDACTED]" in blob


def test_no_buffer_means_no_records_and_normal_events_still_work() -> None:
    transcript.set_active_buffer(None)
    backend = ClaudeCodeBackend()
    _feed(backend, [
        _assistant("m3", [{"type": "text", "text": "hello"}]),
        {"type": "result", "subtype": "success", "result": "done", "usage": {}},
    ])
    assert backend.get_status().state == "done"
    assert backend.get_status().output == "done"


def test_stream_event_and_unknown_types_are_kept_not_dropped(
    buffer: transcript.TranscriptBuffer,
) -> None:
    backend = ClaudeCodeBackend()
    raw = {"type": "stream_event", "event": {"type": "message_delta", "usage": {"output_tokens": 9}}}
    _feed(backend, [raw, {"type": "future_kind", "payload": "x"}])
    records = buffer.after(0, 10)
    assert [r["type"] for r in records] == ["system", "system"]
    assert records[0]["input"] == raw
    assert "stream_event" in str(records[0]["text"])
    assert "future_kind" in str(records[1]["text"])
