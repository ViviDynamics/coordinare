"""Spec 129: OpenCodeCompatAdapter transcript records.

Same SSE shape as OpenCodeAdapter (the compat adapter shares its transcript tap).
Checks that the transcript keeps full text while the BackendEvent summary stays
capped at 200 characters.
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

LONG_TEXT = "Compatibility profile reasoning, kept whole for the transcript. " * 10
LONG_OUTPUT = "output line from a compatible backend that runs past two hundred. " * 8
TOKEN = "sk-litellm-" + "b" * 30
SUMMARY_CAP = 200
PORT = 19960


@pytest.fixture
def buffer():
    buf = transcript.TranscriptBuffer("run", max_bytes=10_000_000)
    transcript.set_active_buffer(buf)
    yield buf
    transcript.set_active_buffer(None)


def _records(buf: transcript.TranscriptBuffer) -> list[dict[str, Any]]:
    return buf.after(0, 10_000)


def _types(records: list[dict[str, Any]]) -> list[str]:
    return [r["type"] for r in records]


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


@respx.mock
async def test_text_and_tool_parts_are_recorded_whole(
    tmp_path: Path, buffer: transcript.TranscriptBuffer,
) -> None:
    events = [
        _part_event({"type": "text", "id": "t1", "text": LONG_TEXT}),
        _part_event({
            "type": "tool", "id": "prt_9", "callID": "call_9", "tool": "read_file",
            "state": {"status": "running", "input": {"path": "/src/" + "d/" * 120}},
        }),
        _part_event({
            "type": "tool", "id": "prt_9", "callID": "call_9", "tool": "read_file",
            "state": {"status": "completed", "input": {"path": "x"}, "output": LONG_OUTPUT},
        }),
        {"type": "session.idle", "properties": {}},
    ]
    adapter = await _run_turn(tmp_path, PORT, events)

    records = _records(buffer)
    assert _types(records) == ["assistant_text", "tool_call", "tool_result", "result"]
    assert records[0]["text"] == LONG_TEXT
    assert len(records[1]["input"]["path"]) > SUMMARY_CAP
    assert records[2]["call_id"] == "call_9"
    assert records[2]["output"] == LONG_OUTPUT
    assert records[3]["is_error"] is False
    # The compat adapter keeps no assembled output, so the result mirrors its status.
    assert records[3].get("output") == adapter.get_status().output


@respx.mock
async def test_backend_event_summary_stays_capped_while_transcript_is_whole(
    tmp_path: Path, buffer: transcript.TranscriptBuffer,
) -> None:
    events = [
        _part_event({"type": "text", "id": "t2", "text": LONG_TEXT}),
        {"type": "session.idle", "properties": {}},
    ]
    adapter = await _run_turn(tmp_path, PORT + 1, events)

    progress = [e for e in adapter.drain_events() if e.type.value == "progress"]
    assert progress and all(len(e.text) <= SUMMARY_CAP for e in progress)
    text_record = next(r for r in _records(buffer) if r["type"] == "assistant_text")
    assert text_record["text"] == LONG_TEXT
    assert len(text_record["text"]) > SUMMARY_CAP


@respx.mock
async def test_session_error_is_full_text_and_result_is_error(
    tmp_path: Path, buffer: transcript.TranscriptBuffer,
) -> None:
    message = "upstream refused the call " + ("with detail " * 20)
    events = [{"type": "session.error", "properties": {"error": {"message": message}}}]
    await _run_turn(tmp_path, PORT + 2, events)

    records = _records(buffer)
    assert next(r for r in records if r["type"] == "error")["text"] == message
    assert records[-1]["type"] == "result"
    assert records[-1]["is_error"] is True
    assert records[-1]["text"] == message


@respx.mock
async def test_tool_output_secret_is_redacted(
    tmp_path: Path, buffer: transcript.TranscriptBuffer,
) -> None:
    events = [
        _part_event({
            "type": "tool", "id": "prt_10", "callID": "call_10", "tool": "bash",
            "state": {"status": "completed", "input": {"command": "env"},
                      "output": "OPENAI_API_KEY=" + TOKEN + "\n" + LONG_OUTPUT},
        }),
        {"type": "session.idle", "properties": {}},
    ]
    await _run_turn(tmp_path, PORT + 3, events)

    dumped = json.dumps(_records(buffer))
    assert TOKEN not in dumped
    assert LONG_OUTPUT in dumped


@respx.mock
async def test_relay_feedback_records_user_turn(
    tmp_path: Path, buffer: transcript.TranscriptBuffer,
) -> None:
    port = PORT + 4
    respx.post(f"http://127.0.0.1:{port}/session/sess-1/prompt_async").mock(
        return_value=httpx.Response(204),
    )
    respx.get(f"http://127.0.0.1:{port}/event").mock(return_value=httpx.Response(200, content=b""))
    respx.get(f"http://127.0.0.1:{port}/session/sess-1").mock(
        return_value=httpx.Response(200, json={"time": {"idle": 1}}),
    )
    adapter = OpenCodeCompatAdapter(base_url="http://127.0.0.1:9/v1")
    adapter._client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}")
    adapter._session_id = "sess-1"
    feedback = "Repair the JSON so it matches the contract. " * 8
    await adapter.relay_feedback(feedback)
    if adapter._reader_task is not None:
        await adapter._reader_task

    records = _records(buffer)
    assert _types(records) == ["user", "result"]
    assert records[0]["text"] == feedback
