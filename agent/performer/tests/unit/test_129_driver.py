"""Spec 129: DriverBackend transcript records.

Drives the real stdout reader (``_read_loop``) and the start/relay paths over
driver JSONL lines. The process is faked; the wire shape is the contract's.
"""
from __future__ import annotations

import asyncio
import datetime
import json
import pathlib
from typing import Any

import pytest

from performer import transcript
from performer.backends.driver import DriverBackend
from performer.models import Score, Stand

LONG_DELTA = "The fix touches the parser and the cache layer together. " * 8
LONG_EVENT = "read_file: " + ("src/module/file.py " * 20)
LONG_OUTPUT = "Final answer with the complete summary of every change made. " * 8
TOKEN = "ghp_" + "c" * 36


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


def _stand(tmp_path: pathlib.Path) -> Stand:
    return Stand(path=tmp_path, branch="driver/x", created_at=datetime.datetime.now(datetime.UTC))


class _FakeProc:
    def __init__(self, *, live: bool = True) -> None:
        self.returncode = None if live else 0
        self.stdout = None
        self.pid = 1_234_567

    async def wait(self) -> int:
        return 0


async def test_start_records_one_capability_then_full_prompt_as_user(
    tmp_path: pathlib.Path, monkeypatch, buffer: transcript.TranscriptBuffer,
) -> None:
    async def _fake_exec(*cmd: str, **kwargs: Any) -> _FakeProc:
        return _FakeProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
    score = Score(
        title="Fix the bug",
        description=LONG_DELTA,
        repo_url="https://github.com/o/r.git",
        branch="driver/x",
        persona_instructions="Be precise.",
    )
    backend = DriverBackend()
    await backend.start(_stand(tmp_path), score)
    if backend._reader_task is not None:
        await backend._reader_task

    records = _records(buffer)
    assert _types(records)[:2] == ["capability", "user"]
    assert _types(records).count("capability") == 1
    capability = records[0]
    assert capability["input"]["unavailable"] == ["tool_result"]
    user = records[1]
    assert LONG_DELTA in user["text"]
    assert user["text"].startswith("# Task: Fix the bug")


async def test_stream_records_full_text_tool_thinking_usage_error_and_result(
    tmp_path: pathlib.Path, buffer: transcript.TranscriptBuffer,
) -> None:
    backend = DriverBackend()
    backend._proc = _StreamProc(
        json.dumps({"type": "progress", "text": "The fix ", "is_delta": True, "stream_id": "s1"}),
        json.dumps({"type": "progress", "text": LONG_DELTA, "is_delta": True, "stream_id": "s1"}),
        json.dumps({"type": "tool_use", "text": LONG_EVENT + TOKEN, "detail": "read_file"}),
        json.dumps({"type": "thinking", "text": "weighing the two parsers " * 12}),
        json.dumps({"type": "cost", "text": "turn cost", "usage": {"input_tokens": 120, "output_tokens": 45}}),
        json.dumps({"type": "progress", "text": "compaction_start"}),
        "driver: a non-JSON diagnostic line",
        json.dumps({"type": "error", "text": "upstream " + ("e" * 250)}),
        json.dumps({
            "type": "result", "status": "done", "output": LONG_OUTPUT, "stop_reason": "end_turn",
            "session_id": "sess-1", "turns": 2,
            "usage": {"input_tokens": 500, "output_tokens": 90, "total_tokens": 590},
        }),
    )
    await backend._read_loop()

    records = _records(buffer)
    assert _types(records) == [
        "assistant_text", "tool_call", "thinking", "usage", "system", "system", "error", "usage", "result",
    ]
    streamed = next(r for r in records if r["type"] == "assistant_text")
    assert streamed["text"] == "The fix " + LONG_DELTA

    call = next(r for r in records if r["type"] == "tool_call")
    assert call["tool_name"] == "read_file"
    assert call["text"] == LONG_EVENT + "[REDACTED]"
    assert call.get("call_id") is None
    assert "tool_result" not in _types(records)

    cost = records[3]
    assert cost["usage"]["input_tokens"] == 120
    assert cost["usage"]["output_tokens"] == 45

    run_total = records[7]
    assert run_total["usage"]["input_tokens"] == 500
    assert run_total["usage"]["output_tokens"] == 90
    assert "run total" in run_total["text"]

    result = records[8]
    # output_data is the status output (the existing _optional_text strips it);
    # the raw result line in input keeps the untrimmed final answer.
    assert result["output"] == LONG_OUTPUT.strip()
    assert result["input"]["output"] == LONG_OUTPUT
    assert result["is_error"] is False
    assert result["input"]["session_id"] == "sess-1"


async def test_delta_text_is_flushed_before_the_result(
    tmp_path: pathlib.Path, buffer: transcript.TranscriptBuffer,
) -> None:
    backend = DriverBackend()
    backend._proc = _StreamProc(
        json.dumps({"type": "progress", "text": LONG_DELTA, "is_delta": True, "stream_id": ""}),
        json.dumps({"type": "result", "status": "done", "output": "ok", "usage": None}),
    )
    await backend._read_loop()

    assert _types(_records(buffer)) == ["assistant_text", "result"]
    assert _records(buffer)[0]["text"] == LONG_DELTA


async def test_exit_without_result_records_an_error_result(
    tmp_path: pathlib.Path, buffer: transcript.TranscriptBuffer,
) -> None:
    backend = DriverBackend()
    backend._proc = _StreamProc(returncode=2)
    await backend._read_loop()

    records = _records(buffer)
    assert _types(records) == ["result"]
    assert records[0]["is_error"] is True
    assert records[0]["input"]["source"] == "process exited without a result line"
    assert "exit 2" in records[0]["text"]


async def test_relay_feedback_records_the_resumed_turn(
    tmp_path: pathlib.Path, monkeypatch, buffer: transcript.TranscriptBuffer,
) -> None:
    backend = DriverBackend()
    backend._cwd = str(tmp_path)
    backend._session_path = tmp_path / "session.json"
    backend._proc = _FakeProc(live=True)
    feedback = "Please address the two review comments in full. " * 6

    def _noop_cancel(self) -> None:
        return None

    async def _noop_terminate(self) -> None:
        return None

    async def _noop_spawn(self, cmd: list[str]) -> None:
        return None

    monkeypatch.setattr(DriverBackend, "_cancel_reader", _noop_cancel)
    monkeypatch.setattr(DriverBackend, "_terminate", _noop_terminate)
    monkeypatch.setattr(DriverBackend, "_spawn", _noop_spawn)
    await backend.relay_feedback(feedback)

    records = _records(buffer)
    assert _types(records) == ["user"]
    assert records[0]["text"] == feedback
    assert "capability" not in _types(records)
