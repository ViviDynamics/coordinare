"""Spec 129: OpenCodeAdapter transcript records (full content, tool pairing,
single capability record, error and result records, redaction).

Drives the real SSE reader and start/relay paths with captured event shapes.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import respx

from performer import transcript
from performer.backends.opencode import OpenCodeAdapter
from performer.models import Score, Stand

LONG_TEXT = "The plan has several steps that must all be kept in full. " * 12
LONG_OUTPUT = "line of tool output that is long enough to pass 200 chars. " * 10
LONG_COMMAND = "ls -la " + ("/some/long/path/segment " * 14)
TOKEN = "ghp_" + "a" * 36
PORT = 19950


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


async def _run_turn(tmp_path: Path, port: int, events: list[dict[str, Any]]) -> OpenCodeAdapter:
    respx.get(f"http://127.0.0.1:{port}/event").mock(
        return_value=httpx.Response(200, content=_sse(events), headers={"content-type": "text/event-stream"}),
    )
    adapter = OpenCodeAdapter()
    adapter._port = port
    adapter._session_id = "sess-1"
    adapter._client = httpx.AsyncClient(
        base_url=f"http://127.0.0.1:{port}",
        headers={"x-opencode-directory": str(tmp_path)},
    )
    await adapter._event_reader_loop()
    return adapter


def _fake_proc() -> MagicMock:
    proc = MagicMock()
    proc.pid = 999
    proc.returncode = None
    proc.stdout = MagicMock()
    proc.stdout.read = AsyncMock(return_value=b"")
    proc.wait = AsyncMock(return_value=0)
    return proc


@respx.mock
async def test_start_records_one_capability_then_session_then_full_prompt(
    tmp_path: Path, buffer: transcript.TranscriptBuffer,
) -> None:
    port = PORT + 1
    respx.get(f"http://127.0.0.1:{port}/global/health").mock(
        return_value=httpx.Response(200, json={"healthy": True}),
    )
    respx.post(f"http://127.0.0.1:{port}/session").mock(
        return_value=httpx.Response(200, json={"id": "sess-1"}),
    )
    respx.post(f"http://127.0.0.1:{port}/session/sess-1/prompt_async").mock(
        return_value=httpx.Response(204),
    )
    respx.get(f"http://127.0.0.1:{port}/event").mock(return_value=httpx.Response(200, content=b""))
    score = Score(
        title="T",
        description=LONG_TEXT,
        repo_url="https://github.com/org/repo",
        branch="main",
        github_token="tok",
    )
    adapter = OpenCodeAdapter()
    with (
        patch(
            "performer.backends.opencode.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=_fake_proc()),
        ),
        patch("performer.backends.opencode._find_free_port", return_value=port),
    ):
        await adapter.start(Stand(path=tmp_path, branch="main"), score, model="m1")
    if adapter._reader_task is not None:
        await adapter._reader_task

    records = _records(buffer)
    assert _types(records)[:3] == ["capability", "system", "user"]
    assert _types(records).count("capability") == 1
    user = next(r for r in records if r["type"] == "user")
    assert LONG_TEXT in user["text"]
    system = next(r for r in records if r["type"] == "system")
    assert system["input"]["session"] == {"id": "sess-1"}
    assert system["input"]["model"] == "m1"
    capability = next(r for r in records if r["type"] == "capability")
    assert capability["input"]["unavailable"] == ["thinking", "usage"]


@respx.mock
async def test_turn_records_whole_text_tool_pair_and_result(
    tmp_path: Path, buffer: transcript.TranscriptBuffer,
) -> None:
    events = [
        _part_event({"type": "text", "id": "p1", "text": "The plan begins. "}),
        _part_event({"type": "text", "id": "p1", "text": LONG_TEXT}),
        _part_event({
            "type": "tool", "id": "prt_1", "callID": "call_1", "tool": "bash",
            "state": {"status": "pending", "input": {}},
        }),
        _part_event({
            "type": "tool", "id": "prt_1", "callID": "call_1", "tool": "bash",
            "state": {"status": "running", "input": {"command": LONG_COMMAND}},
        }),
        _part_event({
            "type": "tool", "id": "prt_1", "callID": "call_1", "tool": "bash",
            "state": {"status": "completed", "input": {"command": LONG_COMMAND}, "output": LONG_OUTPUT},
        }),
        _part_event({"type": "text", "id": "p2", "text": "Done."}),
        {"type": "session.idle", "properties": {}},
    ]
    adapter = await _run_turn(tmp_path, PORT, events)

    records = _records(buffer)
    assert _types(records) == [
        "assistant_text", "tool_call", "tool_result", "assistant_text", "result",
    ]
    text_records = [r for r in records if r["type"] == "assistant_text"]
    assert text_records[0]["text"] == LONG_TEXT
    assert text_records[1]["text"] == "Done."

    call = next(r for r in records if r["type"] == "tool_call")
    result = next(r for r in records if r["type"] == "tool_result")
    assert call["tool_name"] == "bash"
    assert call["call_id"] == result["call_id"] == "call_1"
    assert call["input"] == {"command": LONG_COMMAND}
    assert result["output"] == LONG_OUTPUT
    assert result["is_error"] is False

    final = records[-1]
    assert final["output"] == adapter.get_status().output
    assert final["is_error"] is False
    assert final["input"]["status"] == "done"


@respx.mock
async def test_failed_tool_is_flagged_with_full_error(
    tmp_path: Path, buffer: transcript.TranscriptBuffer,
) -> None:
    error_text = "permission denied for path " + ("/locked/area " * 25)
    events = [
        _part_event({
            "type": "tool", "id": "prt_2", "callID": "call_2", "tool": "write",
            "state": {"status": "running", "input": {"path": "/locked/x"}},
        }),
        _part_event({
            "type": "tool", "id": "prt_2", "callID": "call_2", "tool": "write",
            "state": {"status": "error", "input": {"path": "/locked/x"}, "error": error_text},
        }),
        {"type": "session.idle", "properties": {}},
    ]
    await _run_turn(tmp_path, PORT + 2, events)

    result = next(r for r in _records(buffer) if r["type"] == "tool_result")
    assert result["call_id"] == "call_2"
    assert result["is_error"] is True
    assert result["output"] == error_text


@respx.mock
async def test_tool_output_secret_is_redacted(
    tmp_path: Path, buffer: transcript.TranscriptBuffer,
) -> None:
    events = [
        _part_event({
            "type": "tool", "id": "prt_3", "callID": "call_3", "tool": "bash",
            "state": {"status": "completed", "input": {"command": "env"},
                      "output": "GH_TOKEN=" + TOKEN + "\n" + LONG_OUTPUT},
        }),
        {"type": "session.idle", "properties": {}},
    ]
    await _run_turn(tmp_path, PORT + 3, events)

    dumped = json.dumps(_records(buffer))
    assert TOKEN not in dumped
    result = next(r for r in _records(buffer) if r["type"] == "tool_result")
    assert "[REDACTED]" in result["output"]
    assert LONG_OUTPUT in result["output"]


@respx.mock
async def test_session_error_is_full_and_result_is_error(
    tmp_path: Path, buffer: transcript.TranscriptBuffer,
) -> None:
    message = "provider rejected the request " + ("with a long reason " * 15)
    events = [{"type": "session.error", "properties": {"error": {"message": message}}}]
    await _run_turn(tmp_path, PORT + 4, events)

    records = _records(buffer)
    error = next(r for r in records if r["type"] == "error")
    assert error["text"] == message
    result = records[-1]
    assert result["type"] == "result"
    assert result["is_error"] is True
    assert result["text"] == message


@respx.mock
async def test_reader_exception_is_recorded_as_error(
    tmp_path: Path, buffer: transcript.TranscriptBuffer,
) -> None:
    failure = "connection dropped " + ("mid stream " * 25)
    respx.get(f"http://127.0.0.1:{PORT + 5}/event").mock(side_effect=httpx.ConnectError(failure))
    adapter = OpenCodeAdapter()
    adapter._client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{PORT + 5}")
    adapter._session_id = "sess-1"
    await adapter._event_reader_loop()

    records = _records(buffer)
    error = next(r for r in records if r["type"] == "error")
    assert error["text"] == failure
    assert records[-1]["type"] == "result"
    assert records[-1]["is_error"] is True


@respx.mock
async def test_relay_feedback_records_user_turn_without_second_capability(
    tmp_path: Path, buffer: transcript.TranscriptBuffer,
) -> None:
    port = PORT + 6
    respx.post(f"http://127.0.0.1:{port}/session/sess-1/prompt_async").mock(
        return_value=httpx.Response(204),
    )
    respx.get(f"http://127.0.0.1:{port}/event").mock(return_value=httpx.Response(200, content=b""))
    respx.get(f"http://127.0.0.1:{port}/session/sess-1").mock(
        return_value=httpx.Response(200, json={"time": {"idle": 1}}),
    )
    adapter = OpenCodeAdapter()
    adapter._client = httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}")
    adapter._session_id = "sess-1"
    feedback ="Please also cover the edge case. " * 10
    await adapter.relay_feedback(feedback)
    if adapter._reader_task is not None:
        await adapter._reader_task

    records = _records(buffer)
    assert _types(records) == ["user", "result"]
    assert records[0]["text"] == feedback
    assert "capability" not in _types(records)


def test_no_active_buffer_means_no_records_and_no_error() -> None:
    transcript.set_active_buffer(None)
    adapter = OpenCodeAdapter()
    adapter._handle_event(_part_event({"type": "text", "id": "p", "text": LONG_TEXT}))
    adapter._handle_event({"type": "session.idle", "properties": {}})
    assert adapter.get_status().state == "done"
