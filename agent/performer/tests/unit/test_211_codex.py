"""Spec 211: Codex BackendEvents carry the seq of the transcript record they describe.

Drives the real notification handler with the payload shapes of
test_129_codex.py. The transcript buffer is a real TranscriptBuffer.
"""
from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from performer import transcript
from performer.backends.codex import CodexBackend
from performer.models import BackendEvent, BackendEventType

_LONG_OUTPUT = "pytest line\n" * 40
_CALL_ID = "call_shell_1"
RECORD_BATCH = 1


@pytest.fixture
def buf() -> Iterator[transcript.TranscriptBuffer]:
    buffer = transcript.TranscriptBuffer("run-codex-211", max_bytes=10_000_000)
    transcript.set_active_buffer(buffer)
    yield buffer
    transcript.set_active_buffer(None)


def _notify(adapter: CodexBackend, method: str, params: dict[str, Any]) -> None:
    adapter._handle_notification(method, params)


def _command_item(status: str, output: str | None, exit_code: int | None) -> dict[str, Any]:
    return {
        "type": "commandExecution",
        "id": _CALL_ID,
        "command": "/bin/zsh -lc 'python -m pytest -q'",
        "cwd": "/work/stand",
        "commandActions": [],
        "status": status,
        "aggregatedOutput": output,
        "exitCode": exit_code,
        "durationMs": 812,
        "source": "agent",
    }


def _record_at(buffer: transcript.TranscriptBuffer, seq: int) -> dict[str, Any]:
    records = buffer.after(seq - RECORD_BATCH, RECORD_BATCH)
    assert records, f"no record at seq {seq}"
    assert records[0]["seq"] == seq
    return records[0]


def _linked(adapter: CodexBackend, event_type: BackendEventType) -> list[BackendEvent]:
    return [e for e in adapter.drain_events() if e.type == event_type]


def test_completed_command_links_its_tool_result_record(buf: transcript.TranscriptBuffer) -> None:
    adapter = CodexBackend()
    _notify(adapter, "item/started", {"item": _command_item("inProgress", None, None)})
    _notify(adapter, "item/completed", {"item": _command_item("completed", _LONG_OUTPUT, 0)})

    (event,) = _linked(adapter, BackendEventType.tool_use)
    assert event.transcript_seq is not None
    record = _record_at(buf, event.transcript_seq)
    assert record["type"] == "tool_result"
    assert record["call_id"] == _CALL_ID
    assert record["output"]["aggregatedOutput"] == _LONG_OUTPUT


def test_failed_command_event_links_the_same_result_record(
    buf: transcript.TranscriptBuffer,
) -> None:
    adapter = CodexBackend()
    _notify(adapter, "item/completed", {"item": _command_item("failed", "boom\n", 1)})

    (event,) = _linked(adapter, BackendEventType.tool_use)
    assert event.status == "error"
    assert event.transcript_seq is not None
    record = _record_at(buf, event.transcript_seq)
    assert record["type"] == "tool_result"
    assert record["is_error"] is True


def test_completed_file_change_links_its_tool_result_record(
    buf: transcript.TranscriptBuffer,
) -> None:
    adapter = CodexBackend()
    _notify(adapter, "item/completed", {"item": {
        "type": "fileChange", "id": "fc_1", "path": "src/app.py", "status": "completed",
    }})

    (event,) = _linked(adapter, BackendEventType.tool_use)
    assert event.transcript_seq is not None
    record = _record_at(buf, event.transcript_seq)
    assert record["type"] == "tool_result"
    assert record["call_id"] == "fc_1"


def test_error_notification_links_its_error_record(buf: transcript.TranscriptBuffer) -> None:
    adapter = CodexBackend()
    _notify(adapter, "error", {"error": {"message": "rate limited"}, "willRetry": True})

    (event,) = _linked(adapter, BackendEventType.error)
    assert event.text == "rate limited"
    assert event.transcript_seq is not None
    record = _record_at(buf, event.transcript_seq)
    assert record["type"] == "error"
    assert record["text"] == "rate limited"


def test_failed_turn_links_the_turn_error_record(buf: transcript.TranscriptBuffer) -> None:
    adapter = CodexBackend()
    _notify(adapter, "turn/completed", {"turn": {
        "id": "turn-002", "status": "failed", "error": {"message": "model refused"}, "items": [],
    }})

    (event,) = _linked(adapter, BackendEventType.error)
    assert event.transcript_seq is not None
    record = _record_at(buf, event.transcript_seq)
    assert record["type"] == "error"
    assert record["text"] == "model refused"


def test_streamed_deltas_and_cost_have_no_record_counterpart(
    buf: transcript.TranscriptBuffer,
) -> None:
    adapter = CodexBackend()
    _notify(adapter, "item/agentMessage/delta", {"delta": "Hel", "itemId": "msg_1"})
    _notify(adapter, "item/reasoning/textDelta", {"delta": "hmm", "itemId": "r_1"})
    _notify(adapter, "thread/tokenUsage/updated", {"tokenUsage": {"total": {"totalTokens": 9}}})

    events = adapter.drain_events()
    assert [e.type for e in events] == [
        BackendEventType.progress, BackendEventType.thinking, BackendEventType.cost,
    ]
    assert all(e.transcript_seq is None for e in events)


def test_main_path_yields_events_with_a_seq(buf: transcript.TranscriptBuffer) -> None:
    adapter = CodexBackend()
    _notify(adapter, "item/started", {"item": _command_item("inProgress", None, None)})
    _notify(adapter, "item/completed", {"item": _command_item("completed", "ok\n", 0)})
    _notify(adapter, "error", {"error": {"message": "rate limited"}, "willRetry": True})

    assert any(e.transcript_seq is not None for e in adapter.drain_events())


def test_no_buffer_means_every_event_has_no_seq() -> None:
    transcript.set_active_buffer(None)
    adapter = CodexBackend()
    _notify(adapter, "item/completed", {"item": _command_item("completed", "ok\n", 0)})
    _notify(adapter, "error", {"error": {"message": "rate limited"}, "willRetry": True})

    events = adapter.drain_events()
    assert events
    assert all(e.transcript_seq is None for e in events)


def test_event_without_the_key_validates_to_none() -> None:
    event = BackendEvent.model_validate({"type": "progress", "text": "hello"})
    assert event.transcript_seq is None


def test_seq_key_round_trips_through_model_dump() -> None:
    event = BackendEvent.model_validate(
        {"type": "tool_use", "text": "shell", "transcript_seq": 11},
    )
    assert event.model_dump()["transcript_seq"] == 11
    assert BackendEvent.model_validate(event.model_dump()).transcript_seq == 11
