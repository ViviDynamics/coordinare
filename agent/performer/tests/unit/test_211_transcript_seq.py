"""Spec 211: the transcript seq of a record is returned to the caller, and an activity event can carry it."""
from __future__ import annotations

from performer import transcript
from performer.models import BackendEvent, BackendEventType

ROOMY_BYTES = 10_000_000
FIRST_SEQ = 1
TINY_CAP_BYTES = 1


def test_emit_returns_the_seq_of_the_stored_record() -> None:
    buf = transcript.TranscriptBuffer("run-211", max_bytes=ROOMY_BYTES)
    transcript.set_active_buffer(buf)
    try:
        first = transcript.emit("tool_call", tool_name="Bash", call_id="c1")
        second = transcript.emit("tool_result", call_id="c1", output_data={"exit_code": 0})
    finally:
        transcript.set_active_buffer(None)
    assert first == FIRST_SEQ
    assert second == FIRST_SEQ + 1
    assert [r["seq"] for r in buf.after(0, 10)] == [first, second]


def test_emit_returns_none_without_an_active_buffer() -> None:
    transcript.set_active_buffer(None)
    assert transcript.emit("user", text="nothing recorded") is None


def test_append_returns_none_when_the_byte_cap_drops_the_record() -> None:
    buf = transcript.TranscriptBuffer("run-211", max_bytes=TINY_CAP_BYTES)
    # The first record over the cap becomes the truncated marker, which is not the caller's record.
    assert buf.append("assistant_text", text="x" * 500) is None
    assert buf.append("assistant_text", text="later") is None
    assert buf.dropped_records == 1


def test_a_withheld_record_keeps_its_seq() -> None:
    buf = transcript.TranscriptBuffer("run-211", max_bytes=ROOMY_BYTES)
    assert buf.append("tool_result", call_id="c9", output_data={"stdout": "ok"}) == FIRST_SEQ


def test_backend_event_has_no_transcript_seq_by_default() -> None:
    event = BackendEvent(type=BackendEventType.progress, text="working")
    assert event.transcript_seq is None


def test_backend_event_from_an_older_performer_has_no_seq() -> None:
    older = {"type": "tool_use", "text": "Bash", "timestamp": "2026-10-01T10:00:00+00:00"}
    assert BackendEvent.model_validate(older).transcript_seq is None


def test_backend_event_seq_round_trips_through_the_wire_dict() -> None:
    event = BackendEvent(type=BackendEventType.tool_use, text="Bash", transcript_seq=42)
    wire = event.model_dump()
    assert wire["transcript_seq"] == 42
    assert BackendEvent.model_validate(wire).transcript_seq == 42
