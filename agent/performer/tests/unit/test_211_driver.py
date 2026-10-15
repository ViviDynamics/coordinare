"""Spec 211: DriverBackend events carry the seq of the transcript record they describe.

Drives the real stdout reader (``_read_loop``) over driver JSONL lines with a faked
process, then checks each event's ``transcript_seq`` against the record stored at
that seq.
"""
from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

import pytest

from performer import transcript
from performer.backends.driver import DriverBackend
from performer.models import BackendEvent, BackendEventType

if TYPE_CHECKING:
    from collections.abc import Iterator

_MAX_BYTES = 10_000_000
_SEQ = 7
_DELTA = "The fix touches the parser and the cache layer together. " * 8
_TOOL_TEXT = "read_file: src/module/file.py"
# Each event type that has a record of its own, with that record's type.
_RECORD_FOR_EVENT = {
    BackendEventType.tool_use: "tool_call",
    BackendEventType.thinking: "thinking",
    BackendEventType.cost: "usage",
    BackendEventType.error: "error",
}


@pytest.fixture
def buf() -> Iterator[transcript.TranscriptBuffer]:
    buffer = transcript.TranscriptBuffer("run-211-driver", max_bytes=_MAX_BYTES)
    transcript.set_active_buffer(buffer)
    try:
        yield buffer
    finally:
        transcript.set_active_buffer(None)


class _StreamProc:
    """A finished driver process whose stdout replays fixed JSONL lines."""

    pid = 4242

    def __init__(self, *lines: str, returncode: int = 0) -> None:
        self.returncode = returncode
        self.stdout = asyncio.StreamReader()
        for line in lines:
            self.stdout.feed_data(line.encode() + b"\n")
        self.stdout.feed_eof()

    async def wait(self) -> int:
        return self.returncode


def _record_at(buf: transcript.TranscriptBuffer, seq: int) -> dict[str, Any]:
    [record] = buf.after(seq - 1, 1)
    assert record["seq"] == seq
    return record


async def _run(backend: DriverBackend, *events: dict[str, Any]) -> list[BackendEvent]:
    backend._proc = _StreamProc(*[json.dumps(e) for e in events])
    await backend._read_loop()
    return backend.drain_events()


async def test_tool_use_event_points_at_its_tool_call_record(buf: transcript.TranscriptBuffer) -> None:
    backend = DriverBackend()
    events = await _run(
        backend,
        {"type": "tool_use", "text": _TOOL_TEXT, "detail": "read_file"},
    )

    [tool] = [e for e in events if e.type == BackendEventType.tool_use]
    assert tool.transcript_seq is not None
    record = _record_at(buf, tool.transcript_seq)
    assert record["type"] == "tool_call"
    assert record["tool_name"] == "read_file"


async def test_error_thinking_and_cost_events_point_at_their_records(
    buf: transcript.TranscriptBuffer,
) -> None:
    backend = DriverBackend()
    events = await _run(
        backend,
        {"type": "thinking", "text": "weighing the two parsers"},
        {"type": "cost", "text": "turn cost", "usage": {"input_tokens": 120, "output_tokens": 45}},
        {"type": "error", "text": "upstream timed out"},
    )

    by_type = {e.type: e for e in events}
    thinking = _record_at(buf, by_type[BackendEventType.thinking].transcript_seq)
    assert thinking["type"] == "thinking"
    assert thinking["text"] == "weighing the two parsers"

    usage = _record_at(buf, by_type[BackendEventType.cost].transcript_seq)
    assert usage["type"] == "usage"
    assert usage["usage"]["input_tokens"] == 120

    error = _record_at(buf, by_type[BackendEventType.error].transcript_seq)
    assert error["type"] == "error"
    assert error["text"] == "upstream timed out"


async def test_non_delta_progress_points_at_its_system_record(buf: transcript.TranscriptBuffer) -> None:
    backend = DriverBackend()
    events = await _run(backend, {"type": "progress", "text": "compaction_start"})

    [progress] = events
    record = _record_at(buf, progress.transcript_seq)
    assert record["type"] == "system"
    assert record["text"] == "compaction_start"


async def test_main_path_events_point_at_matching_records(buf: transcript.TranscriptBuffer) -> None:
    backend = DriverBackend()
    events = await _run(
        backend,
        {"type": "progress", "text": "The fix ", "is_delta": True, "stream_id": "s1"},
        {"type": "progress", "text": _DELTA, "is_delta": True, "stream_id": "s1"},
        {"type": "tool_use", "text": _TOOL_TEXT, "detail": "read_file"},
        {"type": "thinking", "text": "weighing the two parsers"},
        {"type": "error", "text": "upstream timed out"},
    )

    with_seq = [e for e in events if e.transcript_seq is not None]
    assert with_seq, "the main path must yield at least one event with a seq"
    for event in with_seq:
        record = _record_at(buf, event.transcript_seq)
        assert record["type"] == _RECORD_FOR_EVENT[event.type]
    # A streamed delta has no record of its own; its assistant_text record is written on flush.
    deltas = [e for e in events if e.type == BackendEventType.progress]
    assert len(deltas) == 2
    assert all(e.transcript_seq is None for e in deltas)


async def test_no_active_buffer_leaves_every_event_seq_none() -> None:
    transcript.set_active_buffer(None)
    assert transcript.transcript_enabled() is False
    backend = DriverBackend()
    events = await _run(
        backend,
        {"type": "tool_use", "text": _TOOL_TEXT, "detail": "read_file"},
        {"type": "error", "text": "boom"},
    )

    assert events
    assert all(e.transcript_seq is None for e in events)


def test_event_protocol_is_compatible_with_and_without_the_seq_key() -> None:
    legacy = BackendEvent.model_validate({"type": "tool_use", "text": "bash"})
    assert legacy.transcript_seq is None

    keyed = BackendEvent(type=BackendEventType.error, text="boom", transcript_seq=_SEQ)
    dumped = keyed.model_dump()
    assert dumped["transcript_seq"] == _SEQ
    assert BackendEvent.model_validate(dumped).transcript_seq == _SEQ


# --- 178: an unrecognised driver event is a raw output line, not narration -----


async def test_178_an_unrecognised_event_type_is_recorded_as_output(buf: transcript.TranscriptBuffer) -> None:
    backend = DriverBackend()
    events = await _run(backend, {"type": "mystery_kind", "text": "stray backend line"})

    [raw] = events
    assert raw.type == BackendEventType.output
    assert raw.text == "stray backend line"
    assert _record_at(buf, raw.transcript_seq)["type"] == "system"


async def test_178_an_output_event_from_an_older_driver_is_still_output(buf: transcript.TranscriptBuffer) -> None:
    backend = DriverBackend()
    [raw] = await _run(backend, {"type": "output", "text": "raw line"})
    assert raw.type == BackendEventType.output


async def test_178_a_progress_event_from_a_newer_driver_is_still_progress(buf: transcript.TranscriptBuffer) -> None:
    backend = DriverBackend()
    [progress] = await _run(backend, {"type": "progress", "text": "compaction_start"})
    assert progress.type == BackendEventType.progress
