"""Spec 211: OpenCodeCompatAdapter events carry the seq of the transcript record they mirror.

The compat adapter builds its own events but shares OpenCodeTranscriptTap with
OpenCodeAdapter, so its tool and error events are paired the same way.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
import respx

from performer import transcript
from performer.backends.opencode_compat import OpenCodeCompatAdapter
from performer.models import BackendEvent, BackendEventType

BUFFER_BYTES = 10_000_000
PORT = 19975
ROUND_TRIP_SEQ = 7
TOOL_OUTPUT = "contents of the compatible backend file"


@pytest.fixture
def buffer():
    buf = transcript.TranscriptBuffer("run-compat-211", max_bytes=BUFFER_BYTES)
    transcript.set_active_buffer(buf)
    yield buf
    transcript.set_active_buffer(None)


def _part_event(part: dict[str, Any]) -> dict[str, Any]:
    return {"type": "message.part.updated", "properties": {"part": part}}


def _sse(events: list[dict[str, Any]]) -> bytes:
    return "".join(f"data: {json.dumps(e)}\n\n" for e in events).encode()


async def _run_turn(tmp_path: Path, port: int, events: list[dict[str, Any]]) -> OpenCodeCompatAdapter:
    respx.get(f"http://127.0.0.1:{port}/event").mock(
        return_value=httpx.Response(200, content=_sse(events), headers={"content-type": "text/event-stream"}),
    )
    adapter = OpenCodeCompatAdapter(base_url="http://127.0.0.1:9/v1")
    adapter._session_id = "sess-1"
    adapter._client = httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{port}",
        headers={"x-opencode-directory": str(tmp_path)},
    )
    await adapter._event_reader_loop()
    return adapter


def _record_for(buf: transcript.TranscriptBuffer, event: BackendEvent) -> dict[str, Any]:
    assert event.transcript_seq is not None
    [record] = buf.after(event.transcript_seq - 1, 1)
    assert record["seq"] == event.transcript_seq
    return record


@respx.mock
async def test_tool_input_and_output_events_point_at_their_records(
    tmp_path: Path, buffer: transcript.TranscriptBuffer,
) -> None:
    events = [
        _part_event({
            "type": "tool-input", "id": "prt_c", "callID": "call_c",
            "toolName": "read_file", "input": {"path": "/src/main.py"},
        }),
        _part_event({
            "type": "tool-output", "id": "prt_c", "callID": "call_c",
            "toolName": "read_file", "output": TOOL_OUTPUT,
        }),
        {"type": "session.idle", "properties": {}},
    ]
    adapter = await _run_turn(tmp_path, PORT, events)
    tool_events = [e for e in adapter.drain_events() if e.type == BackendEventType.tool_use]

    call_event, result_event = tool_events
    call_record = _record_for(buffer, call_event)
    assert call_record["type"] == "tool_call"
    assert call_record["call_id"] == "call_c"
    result_record = _record_for(buffer, result_event)
    assert result_record["type"] == "tool_result"
    assert result_record["call_id"] == "call_c"
    assert result_record["output"] == TOOL_OUTPUT


@respx.mock
async def test_session_error_event_points_at_error_record(
    tmp_path: Path, buffer: transcript.TranscriptBuffer,
) -> None:
    message = "upstream refused the call"
    events = [{"type": "session.error", "properties": {"error": {"message": message}}}]
    adapter = await _run_turn(tmp_path, PORT + 1, events)

    [err] = [e for e in adapter.drain_events() if e.type == BackendEventType.error]
    record = _record_for(buffer, err)
    assert record["type"] == "error"
    assert record["text"] == message


def test_no_active_buffer_leaves_every_event_seq_none() -> None:
    transcript.set_active_buffer(None)
    adapter = OpenCodeCompatAdapter(base_url="http://127.0.0.1:9/v1")
    adapter._handle_event(_part_event({
        "type": "tool-input", "id": "prt_d", "callID": "call_d",
        "toolName": "read_file", "input": {"path": "/x"},
    }))
    adapter._handle_event({"type": "session.error", "properties": {"error": {"message": "boom"}}})
    events = adapter.drain_events()
    assert events
    assert all(e.transcript_seq is None for e in events)


def test_event_without_seq_key_validates_to_none() -> None:
    event = BackendEvent.model_validate({"type": "progress", "text": "ok"})
    assert event.transcript_seq is None


def test_seq_key_round_trips_through_model_dump() -> None:
    event = BackendEvent(type=BackendEventType.error, text="boom", transcript_seq=ROUND_TRIP_SEQ)
    dumped = event.model_dump()
    assert dumped["transcript_seq"] == ROUND_TRIP_SEQ
    assert BackendEvent.model_validate(dumped).transcript_seq == ROUND_TRIP_SEQ
