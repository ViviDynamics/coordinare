"""Spec 129: NareBackend transcript records.

Drives the real stdout reader (``_read_loop``) and the start/relay paths over
nare JSONL lines. The process is faked; the line shapes are what nare's contract 4
writes (tests/contract/test_nare_real_binary.py runs the real binary).
"""
from __future__ import annotations

import asyncio
import datetime
import json
import pathlib
from typing import Any

import pytest

from performer import transcript
from performer.backends.nare import NareBackend
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
    """A finished nare process whose stdout replays fixed JSONL lines."""

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
    return Stand(path=tmp_path, branch="nare/x", created_at=datetime.datetime.now(datetime.UTC))


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
        branch="nare/x",
        persona_instructions="Be precise.",
    )
    backend = NareBackend()
    await backend.start(_stand(tmp_path), score)
    if backend._reader_task is not None:
        await backend._reader_task

    records = _records(buffer)
    assert _types(records)[:2] == ["capability", "user"]
    assert _types(records).count("capability") == 1
    capability = records[0]
    assert capability["input"]["unavailable"] == ["tool_result_content"]
    user = records[1]
    assert LONG_DELTA in user["text"]
    assert user["text"].startswith("# Task: Fix the bug")


async def test_stream_records_full_text_tool_thinking_usage_error_and_result(
    tmp_path: pathlib.Path, buffer: transcript.TranscriptBuffer,
) -> None:
    backend = NareBackend()
    backend._proc = _StreamProc(
        json.dumps({"type": "progress", "text": "context window 200000 (provider)",
                    "detail": {"context_window": {"tokens": 200000, "source": "provider"}}}),
        json.dumps({"type": "thinking", "text": "weighing the two parsers " * 12, "detail": {}}),
        json.dumps({"type": "cost", "text": "120 in / 45 out", "detail": {
            "input": 120, "output": 45, "cache_read": 7, "cache_write": 0,
            "cost": None, "estimated": False, "total": 172}}),
        json.dumps({"type": "tool_use", "text": "read", "tool_use_id": "c1",
                    "detail": {"path": LONG_EVENT + TOKEN}}),
        json.dumps({"type": "progress", "text": "read result: ok", "tool_use_id": "c1",
                    "detail": {"tool": "read", "tool_result": {
                        "tool_use_id": "c1", "is_error": False, "chars": 900}}}),
        "nare: a non-JSON diagnostic line",
        json.dumps({"type": "error", "text": "upstream " + ("e" * 250), "detail": {}}),
        json.dumps({"type": "output", "text": LONG_OUTPUT, "detail": {}}),
        json.dumps({
            "type": "result", "status": "done", "output": None, "text": LONG_OUTPUT,
            "stop_reason": "end_turn", "session_id": "sess-1", "turns": 2, "contract": 4,
            "usage": {"input": 500, "output": 90, "cache_read": 10, "cache_write": 0,
                      "cost": 0.02, "estimated": False, "total": 600},
        }),
    )
    await backend._read_loop()

    records = _records(buffer)
    assert _types(records) == [
        "system", "thinking", "usage", "tool_call", "tool_result", "system", "error",
        "assistant_text", "usage", "result",
    ]
    call = next(r for r in records if r["type"] == "tool_call")
    assert call["tool_name"] == "read"
    assert call["call_id"] == "c1"
    assert call["input"]["path"] == LONG_EVENT + "[REDACTED]"

    result_record = next(r for r in records if r["type"] == "tool_result")
    assert result_record["call_id"] == "c1"
    assert result_record["is_error"] is False

    cost = records[2]
    assert cost["usage"]["input_tokens"] == 120
    assert cost["usage"]["output_tokens"] == 45
    assert cost["usage"]["cache_read"] == 7
    assert cost["scope"] == "message"

    streamed = next(r for r in records if r["type"] == "assistant_text")
    assert streamed["text"] == LONG_OUTPUT

    run_total = records[8]
    assert run_total["usage"]["input_tokens"] == 500
    assert run_total["usage"]["output_tokens"] == 90
    assert run_total["scope"] == "run"
    assert "run total" in run_total["text"]

    result = records[9]
    # output_data is the status output (stripped); the raw result line keeps the
    # untrimmed final reply in ``text``.
    assert result["output"] == LONG_OUTPUT.strip()
    assert result["input"]["text"] == LONG_OUTPUT
    assert result["is_error"] is False
    assert result["input"]["session_id"] == "sess-1"


async def test_exit_without_result_records_an_error_result(
    tmp_path: pathlib.Path, buffer: transcript.TranscriptBuffer,
) -> None:
    backend = NareBackend()
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
    backend = NareBackend()
    backend._cwd = str(tmp_path)
    backend._session_path = tmp_path / "session.json"
    backend._proc = _FakeProc(live=True)
    feedback = "Please address the two review comments in full. " * 6

    async def _noop_cancel(self) -> None:
        return None

    async def _noop_terminate(self) -> None:
        return None

    async def _noop_spawn(self, cmd: list[str]) -> None:
        return None

    monkeypatch.setattr(NareBackend, "_cancel_reader", _noop_cancel)
    monkeypatch.setattr(NareBackend, "_terminate", _noop_terminate)
    monkeypatch.setattr(NareBackend, "_spawn", _noop_spawn)
    await backend.relay_feedback(feedback)

    records = _records(buffer)
    assert _types(records) == ["user"]
    assert records[0]["text"] == feedback
    assert "capability" not in _types(records)
