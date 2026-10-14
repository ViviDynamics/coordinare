"""Spec 129: PrimeAgentBackend transcript records.

Drives the real ``--mode json`` stdout reader (``_read_loop``) and the start and
relay paths. The process is faked with a stream of event lines in the shared
Pi/Prime vocabulary.
"""
from __future__ import annotations

import asyncio
import datetime
import json
import pathlib
from typing import Any

import pytest

from performer import transcript
from performer.backends.prime_agent import PrimeAgentBackend
from performer.models import Score, Stand

LONG_DELTA = "Here is the plan, which has several steps that must be kept whole. " * 8
LONG_OUTPUT = "tool output line that runs well past the two hundred character summary cap. " * 6
LONG_COMMAND = "cat " + ("/var/some/long/path/file.txt " * 12)
TOKEN = "ghp_" + "d" * 36
SUMMARY_CAP = 200


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


class _SilentProc:
    pid = 5555
    returncode = 0
    stdout = None

    async def wait(self) -> int:
        return 0


def _stand(tmp_path: pathlib.Path) -> Stand:
    return Stand(path=tmp_path, branch="prime/x", created_at=datetime.datetime.now(datetime.UTC))


async def test_start_records_one_capability_then_full_prompt_as_user(
    tmp_path: pathlib.Path, monkeypatch, buffer: transcript.TranscriptBuffer,
) -> None:
    async def _fake_exec(*argv: str, **kwargs: Any) -> _SilentProc:
        return _SilentProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
    score = Score(
        title="Plan the change",
        description=LONG_DELTA,
        repo_url="https://github.com/o/r.git",
        branch="prime/x",
    )
    backend = PrimeAgentBackend()
    await backend.start(_stand(tmp_path), score)

    records = _records(buffer)
    assert _types(records)[:2] == ["capability", "user"]
    assert _types(records).count("capability") == 1
    capability = records[0]
    assert capability["input"]["unavailable"] == ["thinking", "usage"]
    assert LONG_DELTA in records[1]["text"]


async def test_stream_records_whole_messages_tools_errors_and_result(
    tmp_path: pathlib.Path, buffer: transcript.TranscriptBuffer,
) -> None:
    backend = PrimeAgentBackend()
    backend._proc = _StreamProc(
        json.dumps({"type": "session", "version": 3, "id": "sess-prime"}),
        json.dumps({"type": "agent_start"}),
        json.dumps({"type": "turn_start"}),
        json.dumps({"type": "message_start"}),
        json.dumps({"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "Plan: "}}),
        json.dumps({"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": LONG_DELTA}}),
        json.dumps({"type": "message_end"}),
        json.dumps({
            "type": "tool_execution_start", "toolName": "bash", "toolCallId": "tc_1",
            "args": {"command": LONG_COMMAND + TOKEN},
        }),
        json.dumps({
            "type": "tool_execution_end", "toolName": "bash", "toolCallId": "tc_1",
            "result": {"output": LONG_OUTPUT}, "isError": False,
        }),
        json.dumps({"type": "message_start"}),
        json.dumps({"type": "message_update", "assistantMessageEvent": {"type": "text_delta", "delta": "Done"}}),
        json.dumps({"type": "message_end"}),
        json.dumps({"type": "error", "error": {"message": "rate limited " + ("r" * 240)}, "willRetry": True}),
        json.dumps({
            "type": "agent_end",
            "messages": [{"role": "assistant", "content": "Done", "stopReason": "end_turn"}],
        }),
    )
    await backend._read_loop()

    records = _records(buffer)
    assert _types(records) == [
        "system", "system", "assistant_text", "tool_call", "tool_result",
        "assistant_text", "error", "result",
    ]
    assert records[0]["input"]["id"] == "sess-prime"
    assert records[2]["text"] == "Plan: " + LONG_DELTA

    call = records[3]
    assert call["tool_name"] == "bash"
    assert call["call_id"] == "tc_1"
    assert len(call["input"]["args"]["command"]) > SUMMARY_CAP
    assert TOKEN not in json.dumps(call)

    result_record = records[4]
    assert result_record["call_id"] == "tc_1"
    assert result_record["is_error"] is False
    assert result_record["output"]["result"]["output"] == LONG_OUTPUT

    assert records[5]["text"] == "Done"
    assert records[6]["text"] == "rate limited " + ("r" * 240)

    final = records[7]
    assert final["output"] == "Done"
    assert final["is_error"] is False
    assert final["input"]["state"] == "done"


async def test_failed_turn_is_an_error_record_and_an_error_result(
    tmp_path: pathlib.Path, buffer: transcript.TranscriptBuffer,
) -> None:
    message = "provider 500 " + ("upstream detail " * 15)
    backend = PrimeAgentBackend()
    backend._proc = _StreamProc(
        json.dumps({
            "type": "agent_end",
            "messages": [{"role": "assistant", "content": "", "stopReason": "error", "errorMessage": message}],
        }),
        returncode=1,
    )
    await backend._read_loop()

    records = _records(buffer)
    assert _types(records) == ["error", "result"]
    assert records[0]["text"] == message
    assert records[1]["is_error"] is True
    assert records[1]["text"] == message


async def test_unparseable_line_is_kept_as_a_system_record(
    tmp_path: pathlib.Path, buffer: transcript.TranscriptBuffer,
) -> None:
    raw = "warning: not json, but the full line is kept " + ("x" * 230)
    backend = PrimeAgentBackend()
    backend._proc = _StreamProc(raw, json.dumps({"type": "agent_end", "messages": []}))
    await backend._read_loop()

    records = _records(buffer)
    assert records[0]["type"] == "system"
    assert records[0]["text"] == raw


async def test_relay_feedback_records_the_new_prompt(
    tmp_path: pathlib.Path, monkeypatch, buffer: transcript.TranscriptBuffer,
) -> None:
    async def _fake_exec(*argv: str, **kwargs: Any) -> _SilentProc:
        return _SilentProc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_exec)
    backend = PrimeAgentBackend()
    backend._cwd = str(tmp_path)
    feedback = "Please restate the plan with the two requested changes. " * 6
    await backend.relay_feedback(feedback)

    records = _records(buffer)
    assert _types(records) == ["user"]
    assert records[0]["text"] == feedback
