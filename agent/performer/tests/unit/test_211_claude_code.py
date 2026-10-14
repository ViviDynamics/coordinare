"""Spec 211: Claude Code BackendEvents carry the seq of the transcript record they describe.

Drives the real stream-json handler with the scenario shapes of
test_129_claude_code.py. The transcript buffer is a real TranscriptBuffer.
"""
from __future__ import annotations

from collections.abc import Iterator

import pytest

from performer import transcript
from performer.backends.claude_code import ClaudeCodeBackend
from performer.models import BackendEvent, BackendEventType

LONG_OUTPUT = "line of tool output\n" * 50
RECORD_BATCH = 1
EVENT_RECORD_TYPES = {
    BackendEventType.progress: "assistant_text",
    BackendEventType.thinking: "thinking",
    BackendEventType.cost: "usage",
    BackendEventType.error: "error",
}


@pytest.fixture
def buffer() -> Iterator[transcript.TranscriptBuffer]:
    buf = transcript.TranscriptBuffer("run-cc-211", max_bytes=10_000_000)
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


def _full_run() -> list[dict]:
    return [
        _assistant("m1", [
            {"type": "thinking", "thinking": "plan the edit"},
            {"type": "text", "text": "I will list the files."},
            {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "ls -la"}},
        ]),
        {
            "type": "user",
            "message": {"content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": LONG_OUTPUT,
                 "is_error": False},
            ]},
        },
        _assistant("m1", [{"type": "text", "text": "Listing done."}], output_tokens=40),
        {
            "type": "result",
            "subtype": "success",
            "result": "final answer",
            "is_error": False,
            "stop_reason": "end_turn",
            "num_turns": 2,
            "total_cost_usd": 0.01,
            "usage": {"input_tokens": 10, "output_tokens": 40,
                      "cache_read_input_tokens": 100, "cache_creation_input_tokens": 7},
            "modelUsage": {"claude-sonnet-test": {}},
            "session_id": "s1",
        },
    ]


def _record_at(buf: transcript.TranscriptBuffer, seq: int) -> dict:
    records = buf.after(seq - RECORD_BATCH, RECORD_BATCH)
    assert records, f"no record at seq {seq}"
    assert records[0]["seq"] == seq
    return records[0]


def _expected_record_type(event: BackendEvent) -> str:
    if event.type == BackendEventType.tool_use:
        return "tool_result" if event.text.startswith("result:") else "tool_call"
    return EVENT_RECORD_TYPES[event.type]


def test_every_event_of_a_full_run_points_at_its_record(
    buffer: transcript.TranscriptBuffer,
) -> None:
    backend = ClaudeCodeBackend()
    _feed(backend, _full_run())
    events = backend.drain_events()

    assert events
    # Every event of this run has a record counterpart, so every one is linked.
    assert all(e.transcript_seq is not None for e in events)
    for event in events:
        assert event.transcript_seq is not None
        record = _record_at(buffer, event.transcript_seq)
        assert record["type"] == _expected_record_type(event)


def test_tool_call_and_tool_result_events_link_their_own_records(
    buffer: transcript.TranscriptBuffer,
) -> None:
    backend = ClaudeCodeBackend()
    # A top-level tool_result event is the one that yields a tool_use "result:" BackendEvent.
    _feed(backend, [
        _assistant("m1", [{"type": "tool_use", "id": "t1", "name": "Bash",
                           "input": {"command": "ls -la"}}]),
        {"type": "tool_result", "tool_use_id": "t1", "content": LONG_OUTPUT, "is_error": False},
    ])
    events = backend.drain_events()

    call_event = next(e for e in events if e.type == BackendEventType.tool_use
                      and not e.text.startswith("result:"))
    result_event = next(e for e in events if e.type == BackendEventType.tool_use
                        and e.text.startswith("result:"))

    assert call_event.transcript_seq is not None
    assert result_event.transcript_seq is not None
    call = _record_at(buffer, call_event.transcript_seq)
    assert call["type"] == "tool_call"
    assert call["call_id"] == "t1"
    assert call["tool_name"] == "Bash"

    result = _record_at(buffer, result_event.transcript_seq)
    assert result["type"] == "tool_result"
    assert result["call_id"] == "t1"
    assert result["output"] == LONG_OUTPUT


def test_cost_event_links_the_run_usage_record(buffer: transcript.TranscriptBuffer) -> None:
    backend = ClaudeCodeBackend()
    _feed(backend, _full_run())
    cost = next(e for e in backend.drain_events() if e.type == BackendEventType.cost)

    assert cost.transcript_seq is not None
    record = _record_at(buffer, cost.transcript_seq)
    assert record["type"] == "usage"
    assert record["scope"] == "run"


def test_api_retry_and_error_result_events_link_their_error_records(
    buffer: transcript.TranscriptBuffer,
) -> None:
    backend = ClaudeCodeBackend()
    _feed(backend, [
        {"type": "system", "subtype": "api_retry", "error_status": 529, "error": "overloaded"},
        {"type": "result", "subtype": "error", "error": "tool crashed hard"},
    ])
    errors = [e for e in backend.drain_events() if e.type == BackendEventType.error]

    assert [e.text for e in errors] == ["upstream api_retry (overloaded)", "tool crashed hard"]
    for event in errors:
        assert event.transcript_seq is not None
        record = _record_at(buffer, event.transcript_seq)
        assert record["type"] == "error"
        assert record["text"] == event.text


def test_max_tokens_limit_event_links_the_result_record(
    buffer: transcript.TranscriptBuffer,
) -> None:
    backend = ClaudeCodeBackend()
    _feed(backend, [
        {"type": "result", "subtype": "success", "result": "cut off", "stop_reason": "max_tokens",
         "usage": {}},
    ])
    limit = next(e for e in backend.drain_events() if e.text == "output token limit reached")

    assert limit.transcript_seq is not None
    record = _record_at(buffer, limit.transcript_seq)
    assert record["type"] == "result"
    assert record["output"]["stop_reason"] == "max_tokens"


def test_main_path_yields_events_with_a_seq(buffer: transcript.TranscriptBuffer) -> None:
    backend = ClaudeCodeBackend()
    _feed(backend, _full_run())
    assert any(e.transcript_seq is not None for e in backend.drain_events())


def test_no_buffer_means_every_event_has_no_seq() -> None:
    transcript.set_active_buffer(None)
    backend = ClaudeCodeBackend()
    _feed(backend, _full_run())
    events = backend.drain_events()

    assert events
    assert all(e.transcript_seq is None for e in events)


def test_event_without_the_key_validates_to_none() -> None:
    event = BackendEvent.model_validate({"type": "progress", "text": "hello"})
    assert event.transcript_seq is None


def test_seq_key_round_trips_through_model_dump() -> None:
    event = BackendEvent.model_validate(
        {"type": "progress", "text": "hello", "transcript_seq": 7},
    )
    assert event.model_dump()["transcript_seq"] == 7
    assert BackendEvent.model_validate(event.model_dump()).transcript_seq == 7
