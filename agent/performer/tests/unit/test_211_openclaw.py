"""Spec 211: OpenClawBackend events carry the seq of the transcript record they describe.

Drives ``_wait_and_parse`` with a faked subprocess, then checks each event's
``transcript_seq`` against the record stored at that seq.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from performer import transcript
from performer.backends.openclaw import OpenClawBackend
from performer.models import BackendEvent, BackendEventType

if TYPE_CHECKING:
    from collections.abc import Iterator

_MAX_BYTES = 10_000_000
_SEQ = 7
_SHORT_STDERR = b"boom: config missing"
_FIRST_REPLY = "first reply, delivered as the first payload"
_SECOND_REPLY = "second payload"


@pytest.fixture
def buf() -> Iterator[transcript.TranscriptBuffer]:
    buffer = transcript.TranscriptBuffer("run-211-openclaw", max_bytes=_MAX_BYTES)
    transcript.set_active_buffer(buffer)
    try:
        yield buffer
    finally:
        transcript.set_active_buffer(None)


@pytest.fixture
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Keep openclaw's HOME-relative workspace symlink and config inside tmp_path."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("OPENCLAW_PROVIDER_BASE_URL", raising=False)
    return home


def _proc(stdout: bytes = b"", stderr: bytes = b"", rc: int = 0) -> MagicMock:
    proc = MagicMock()
    proc.pid = 4343
    proc.returncode = rc
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.wait = AsyncMock(return_value=rc)
    return proc


def _turn(payloads: list[str], **meta_overrides: Any) -> bytes:
    meta: dict[str, Any] = {
        "stopReason": "stop",
        "aborted": False,
        "finalAssistantVisibleText": payloads[0] if payloads else "",
    }
    meta.update(meta_overrides)
    return json.dumps({"payloads": [{"text": t} for t in payloads], "meta": meta}).encode()


async def _parse(stdout: bytes, *, rc: int = 0, stderr: bytes = b"") -> list[BackendEvent]:
    backend = OpenClawBackend()
    backend._proc = _proc(stdout=stdout, stderr=stderr, rc=rc)
    await backend._wait_and_parse()
    return backend.drain_events()


def _record_at(buffer: transcript.TranscriptBuffer, seq: int) -> dict[str, Any]:
    [record] = buffer.after(seq - 1, 1)
    assert record["seq"] == seq
    return record


async def test_progress_event_points_at_the_first_assistant_message(
    isolated_home: Path, buf: transcript.TranscriptBuffer,
) -> None:
    events = await _parse(_turn([_FIRST_REPLY, _SECOND_REPLY]))

    [progress] = [e for e in events if e.type == BackendEventType.progress]
    assert progress.transcript_seq is not None
    record = _record_at(buf, progress.transcript_seq)
    assert record["type"] == "assistant_text"
    assert record["text"] == _FIRST_REPLY
    assert progress.text == _FIRST_REPLY


async def test_nonzero_exit_error_event_points_at_the_error_record(
    isolated_home: Path, buf: transcript.TranscriptBuffer,
) -> None:
    events = await _parse(b"", rc=2, stderr=_SHORT_STDERR)

    [error] = [e for e in events if e.type == BackendEventType.error]
    record = _record_at(buf, error.transcript_seq)
    assert record["type"] == "error"
    assert record["text"] == _SHORT_STDERR.decode()
    assert error.text == _SHORT_STDERR.decode()


async def test_malformed_output_error_event_points_at_the_error_record(
    isolated_home: Path, buf: transcript.TranscriptBuffer,
) -> None:
    events = await _parse(b"not a json object")

    [error] = [e for e in events if e.type == BackendEventType.error]
    record = _record_at(buf, error.transcript_seq)
    assert record["type"] == "error"
    assert record["text"].startswith("malformed_output")


async def test_aborted_turn_error_event_points_at_the_error_record(
    isolated_home: Path, buf: transcript.TranscriptBuffer,
) -> None:
    events = await _parse(_turn([_FIRST_REPLY], stopReason="length"))

    [error] = [e for e in events if e.type == BackendEventType.error]
    record = _record_at(buf, error.transcript_seq)
    assert record["type"] == "error"
    assert "length" in record["text"]


async def test_main_path_yields_events_with_seq(
    isolated_home: Path, buf: transcript.TranscriptBuffer,
) -> None:
    events = await _parse(_turn([_FIRST_REPLY]))

    with_seq = [e for e in events if e.transcript_seq is not None]
    assert with_seq, "the main path must yield at least one event with a seq"
    for event in with_seq:
        assert _record_at(buf, event.transcript_seq)["type"] == "assistant_text"


async def test_no_active_buffer_leaves_every_event_seq_none(isolated_home: Path) -> None:
    transcript.set_active_buffer(None)
    assert transcript.transcript_enabled() is False
    events = await _parse(_turn([_FIRST_REPLY]))
    failed = await _parse(b"", rc=2, stderr=_SHORT_STDERR)

    assert events and failed
    assert all(e.transcript_seq is None for e in events + failed)


def test_event_protocol_is_compatible_with_and_without_the_seq_key() -> None:
    legacy = BackendEvent.model_validate({"type": "progress", "text": "ok"})
    assert legacy.transcript_seq is None

    keyed = BackendEvent(type=BackendEventType.error, text="boom", transcript_seq=_SEQ)
    dumped = keyed.model_dump()
    assert dumped["transcript_seq"] == _SEQ
    assert BackendEvent.model_validate(dumped).transcript_seq == _SEQ
