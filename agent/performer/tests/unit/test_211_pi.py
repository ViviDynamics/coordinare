"""Spec 211: PiBackend events carry the seq of the transcript record they describe.

Feeds Pi ``--mode json`` stream events through the real ``_handle_event`` with an
active transcript buffer, then checks each event's ``transcript_seq`` against the
record stored at that seq.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from performer import transcript
from performer.backends.pi import PiBackend
from performer.models import BackendEvent, BackendEventType

if TYPE_CHECKING:
    from collections.abc import Iterator

_MAX_BYTES = 10_000_000
_MODEL = "example-provider/test-model"
_LONG_OUTPUT = "PASSED test_case_line\n" * 40
_RETRY_REASON = "rate limit exceeded, retry scheduled " + "z" * 250
_SEQ = 7
# The record type each event type is built from, for the events that have one.
_RECORD_FOR_EVENT = {
    BackendEventType.tool_use: "tool_call",
    BackendEventType.error: "error",
}


@pytest.fixture
def buf() -> Iterator[transcript.TranscriptBuffer]:
    buffer = transcript.TranscriptBuffer("run-211-pi", max_bytes=_MAX_BYTES)
    transcript.set_active_buffer(buffer)
    try:
        yield buffer
    finally:
        transcript.set_active_buffer(None)


def _feed(adapter: PiBackend, events: list[dict[str, Any]]) -> list[BackendEvent]:
    for event in events:
        adapter._handle_event(event)
    return adapter.drain_events()


def _record_at(buffer: transcript.TranscriptBuffer, seq: int) -> dict[str, Any]:
    [record] = buffer.after(seq - 1, 1)
    assert record["seq"] == seq
    return record


def _assistant(text: str) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": [{"type": "text", "text": text}],
        "api": "openai-completions",
        "provider": "litellm",
        "model": _MODEL,
        "stopReason": "stop",
    }


def test_tool_use_event_points_at_its_tool_call_record(buf: transcript.TranscriptBuffer) -> None:
    adapter = PiBackend()
    events = _feed(adapter, [
        {"type": "tool_execution_start", "toolCallId": "call_1", "toolName": "bash",
         "args": {"command": "pytest -q"}},
        {"type": "tool_execution_end", "toolCallId": "call_1", "toolName": "bash",
         "result": {"content": [{"type": "text", "text": _LONG_OUTPUT}]}, "isError": False},
    ])

    [tool] = [e for e in events if e.type == BackendEventType.tool_use]
    assert tool.transcript_seq is not None
    record = _record_at(buf, tool.transcript_seq)
    assert record["type"] == "tool_call"
    assert record["call_id"] == "call_1"
    assert record["tool_name"] == "bash"


def test_error_event_points_at_its_error_record(buf: transcript.TranscriptBuffer) -> None:
    adapter = PiBackend()
    events = _feed(adapter, [
        {"type": "error", "error": {"message": _RETRY_REASON}, "willRetry": False},
    ])

    [error] = [e for e in events if e.type == BackendEventType.error]
    assert error.transcript_seq is not None
    record = _record_at(buf, error.transcript_seq)
    assert record["type"] == "error"
    assert record["text"] == _RETRY_REASON
    # The terminal error is followed by its result record, which is not the event's record.
    assert _record_at(buf, error.transcript_seq + 1)["type"] == "result"


def test_auto_retry_event_points_at_its_error_record(buf: transcript.TranscriptBuffer) -> None:
    adapter = PiBackend()
    events = _feed(adapter, [{
        "type": "auto_retry_start", "attempt": 1, "maxAttempts": 3,
        "delayMs": 2000, "errorMessage": _RETRY_REASON,
    }])

    [error] = [e for e in events if e.type == BackendEventType.error]
    record = _record_at(buf, error.transcript_seq)
    assert record["type"] == "error"
    assert record["text"] == _RETRY_REASON
    assert adapter.get_status().state != "error"


def test_main_path_events_point_at_matching_records(buf: transcript.TranscriptBuffer) -> None:
    adapter = PiBackend()
    events = _feed(adapter, [
        {"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "Running"}},
        {"type": "message_end", "message": _assistant("Running the suite.")},
        {"type": "tool_execution_start", "toolCallId": "call_2", "toolName": "bash",
         "args": {"command": "pytest -q"}},
        {"type": "tool_execution_end", "toolCallId": "call_2", "toolName": "bash",
         "result": {"content": []}, "isError": False},
        {"type": "error", "error": {"message": "provider 502"}, "willRetry": True},
    ])

    with_seq = [e for e in events if e.transcript_seq is not None]
    assert with_seq, "the main path must yield at least one event with a seq"
    for event in with_seq:
        record = _record_at(buf, event.transcript_seq)
        assert record["type"] == _RECORD_FOR_EVENT[event.type]
    # A streamed text delta has no record of its own; the assistant_text record comes at message_end.
    [progress] = [e for e in events if e.type == BackendEventType.progress]
    assert progress.transcript_seq is None


def test_no_active_buffer_leaves_every_event_seq_none() -> None:
    transcript.set_active_buffer(None)
    assert transcript.transcript_enabled() is False
    adapter = PiBackend()
    events = _feed(adapter, [
        {"type": "tool_execution_start", "toolCallId": "call_3", "toolName": "read",
         "args": {"path": "a.py"}},
        {"type": "error", "error": {"message": "boom"}, "willRetry": False},
    ])

    assert events
    assert all(e.transcript_seq is None for e in events)


def test_event_protocol_is_compatible_with_and_without_the_seq_key() -> None:
    legacy = BackendEvent.model_validate({"type": "tool_use", "text": "bash"})
    assert legacy.transcript_seq is None

    keyed = BackendEvent(type=BackendEventType.error, text="boom", transcript_seq=_SEQ)
    dumped = keyed.model_dump()
    assert dumped["transcript_seq"] == _SEQ
    assert BackendEvent.model_validate(dumped).transcript_seq == _SEQ
