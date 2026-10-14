"""Spec 211: PrimeAgentBackend events carry the seq of the transcript record they describe.

Drives the real ``--mode json`` stdout reader (``_read_loop``) with a faked process
whose stdout replays event lines, then checks each event's ``transcript_seq``
against the record stored at that seq.
"""
from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any

import pytest

from performer import transcript
from performer.backends.prime_agent import PrimeAgentBackend
from performer.models import BackendEvent, BackendEventType

if TYPE_CHECKING:
    import pathlib
    from collections.abc import Iterator

_MAX_BYTES = 10_000_000
_LONG_OUTPUT = "tool output line that runs well past the two hundred character summary cap. " * 6
_SEQ = 7
_RECORD_FOR_EVENT = {
    BackendEventType.tool_use: "tool_call",
    BackendEventType.error: "error",
}


@pytest.fixture
def buf() -> Iterator[transcript.TranscriptBuffer]:
    buffer = transcript.TranscriptBuffer("run-211-prime", max_bytes=_MAX_BYTES)
    transcript.set_active_buffer(buffer)
    try:
        yield buffer
    finally:
        transcript.set_active_buffer(None)


class _StreamProc:
    """A finished prime-agent process whose stdout replays fixed JSON lines."""

    pid = 4321

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


def _line(event: dict[str, Any]) -> str:
    return json.dumps(event)


async def _run(backend: PrimeAgentBackend, *events: dict[str, Any], returncode: int = 0) -> list[BackendEvent]:
    backend._proc = _StreamProc(*[_line(e) for e in events], returncode=returncode)
    await backend._read_loop()
    return backend.drain_events()


async def test_tool_use_event_points_at_its_tool_call_record(
    tmp_path: pathlib.Path, buf: transcript.TranscriptBuffer,
) -> None:
    backend = PrimeAgentBackend()
    events = await _run(
        backend,
        {"type": "tool_execution_start", "toolName": "bash", "toolCallId": "tc_1",
         "args": {"command": "pytest -q"}},
        {"type": "tool_execution_end", "toolName": "bash", "toolCallId": "tc_1",
         "result": {"output": _LONG_OUTPUT}, "isError": False},
    )

    [tool] = [e for e in events if e.type == BackendEventType.tool_use]
    assert tool.transcript_seq is not None
    record = _record_at(buf, tool.transcript_seq)
    assert record["type"] == "tool_call"
    assert record["call_id"] == "tc_1"
    assert record["tool_name"] == "bash"


async def test_retry_and_failure_events_point_at_their_error_records(
    tmp_path: pathlib.Path, buf: transcript.TranscriptBuffer,
) -> None:
    failure = "provider 500 upstream failed"
    backend = PrimeAgentBackend()
    events = await _run(
        backend,
        {"type": "auto_retry_start", "attempt": 1, "maxAttempts": 3, "errorMessage": "rate limited"},
        {"type": "agent_end", "messages": [
            {"role": "assistant", "content": "", "stopReason": "error", "errorMessage": failure},
        ]},
        returncode=1,
    )

    errors = [e for e in events if e.type == BackendEventType.error]
    assert [e.text for e in errors] == ["retry 1/3: rate limited", failure]
    for event in errors:
        assert event.transcript_seq is not None
        record = _record_at(buf, event.transcript_seq)
        assert record["type"] == "error"
        assert record["text"] == event.text


async def test_error_event_points_at_its_error_record(
    tmp_path: pathlib.Path, buf: transcript.TranscriptBuffer,
) -> None:
    backend = PrimeAgentBackend()
    events = await _run(
        backend,
        {"type": "error", "error": {"message": "rate limited"}, "willRetry": True},
    )

    [error] = [e for e in events if e.type == BackendEventType.error]
    record = _record_at(buf, error.transcript_seq)
    assert record["type"] == "error"
    assert record["text"] == "rate limited"


async def test_main_path_events_point_at_matching_records(
    tmp_path: pathlib.Path, buf: transcript.TranscriptBuffer,
) -> None:
    backend = PrimeAgentBackend()
    events = await _run(
        backend,
        {"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "Plan"}},
        {"type": "message_end"},
        {"type": "tool_execution_start", "toolName": "read", "toolCallId": "tc_2",
         "args": {"path": "a.py"}},
        {"type": "tool_execution_end", "toolName": "read", "toolCallId": "tc_2",
         "result": {"output": "x"}, "isError": False},
        {"type": "agent_end", "messages": [{"role": "assistant", "content": "Done", "stopReason": "end_turn"}]},
    )

    with_seq = [e for e in events if e.transcript_seq is not None]
    assert with_seq, "the main path must yield at least one event with a seq"
    for event in with_seq:
        record = _record_at(buf, event.transcript_seq)
        assert record["type"] == _RECORD_FOR_EVENT[event.type]
    # A streamed text delta has no record of its own; assistant_text is written at message end.
    [progress] = [e for e in events if e.type == BackendEventType.progress]
    assert progress.transcript_seq is None


async def test_no_active_buffer_leaves_every_event_seq_none(tmp_path: pathlib.Path) -> None:
    transcript.set_active_buffer(None)
    assert transcript.transcript_enabled() is False
    backend = PrimeAgentBackend()
    events = await _run(
        backend,
        {"type": "tool_execution_start", "toolName": "bash", "toolCallId": "tc_3",
         "args": {"command": "ls"}},
        {"type": "error", "error": {"message": "boom"}, "willRetry": True},
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
