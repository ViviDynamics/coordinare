"""Spec 211: junie backend events carry the seq of the transcript record they mirror."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from performer import transcript
from performer.backends.junie import JunieBackend
from performer.models import BackendEvent, BackendEventType, Score, Stand

BUFFER_BYTES = 10_000_000
FAILED_RC = 3
ROUND_TRIP_SEQ = 7
INPUT_TOKENS = 1200
OUTPUT_TOKENS = 340


@pytest.fixture
def buf():
    buffer = transcript.TranscriptBuffer("run-junie-211", max_bytes=BUFFER_BYTES)
    transcript.set_active_buffer(buffer)
    yield buffer
    transcript.set_active_buffer(None)


@pytest.fixture(autouse=True)
def no_custom_profile(monkeypatch):
    monkeypatch.delenv("JUNIE_PROVIDER_BASE_URL", raising=False)


def _writing_proc(backend: JunieBackend, result: object | None, **options: Any) -> MagicMock:
    """Fake junie process. On exit it writes ``result`` to the JSON output file.

    Options: ``stdout``, ``stderr`` (bytes), ``rc`` (int).
    """
    stdout: bytes = options.get("stdout", b"")
    stderr: bytes = options.get("stderr", b"")
    rc: int = options.get("rc", 0)

    async def _communicate() -> tuple[bytes, bytes]:
        path = backend._json_output_path
        if result is not None and path is not None:
            path.write_text(json.dumps(result))
        return stdout, stderr

    proc = MagicMock()
    proc.pid = 4444
    proc.returncode = rc
    proc.communicate = _communicate
    proc.wait = _noop_wait
    return proc


async def _noop_wait() -> int:
    return 0


def _return(value):
    async def _create(*_args, **_kwargs):
        return value

    return _create


async def _launch_and_wait(
    tmp_path: Path, prompt: str, result: object | None, **proc_kwargs,
) -> JunieBackend:
    backend = JunieBackend()
    backend._stand = Stand(path=tmp_path, branch="main")
    backend._score = Score(title="t", repo_url="https://github.com/o/r", branch="main")
    backend._original_prompt = prompt
    proc = _writing_proc(backend, result, **proc_kwargs)
    with patch(
        "performer.backends.junie.asyncio.create_subprocess_exec",
        new=_return(proc),
    ):
        await backend._launch(prompt)
    assert backend._reader_task is not None
    await backend._reader_task
    return backend


def _record_for(buf: transcript.TranscriptBuffer, event: BackendEvent) -> dict:
    assert event.transcript_seq is not None
    [record] = buf.after(event.transcript_seq - 1, 1)
    assert record["seq"] == event.transcript_seq
    return record


async def test_cost_and_progress_events_point_at_usage_and_assistant_records(
    tmp_path: Path, buf,
) -> None:
    result = {
        "summary": "junie summary of the run",
        "usage": {"input_tokens": INPUT_TOKENS, "output_tokens": OUTPUT_TOKENS},
    }
    backend = await _launch_and_wait(tmp_path, "t", result)
    events = backend.drain_events()

    [cost] = [e for e in events if e.type == BackendEventType.cost]
    usage_record = _record_for(buf, cost)
    assert usage_record["type"] == "usage"
    assert usage_record["usage"]["input_tokens"] == INPUT_TOKENS

    [progress] = [e for e in events if e.type == BackendEventType.progress]
    text_record = _record_for(buf, progress)
    assert text_record["type"] == "assistant_text"
    assert text_record["text"] == "junie summary of the run"


async def test_error_field_event_points_at_error_record(tmp_path: Path, buf) -> None:
    backend = await _launch_and_wait(tmp_path, "t", {"error": "model refused the request"})
    events = backend.drain_events()

    [err] = [e for e in events if e.type == BackendEventType.error]
    record = _record_for(buf, err)
    assert record["type"] == "error"
    assert record["text"] == "model refused the request"


async def test_nonzero_exit_without_result_file_event_points_at_error_record(
    tmp_path: Path, buf,
) -> None:
    backend = await _launch_and_wait(
        tmp_path, "t", None, stderr=b"node failed to start", rc=FAILED_RC,
    )
    events = backend.drain_events()

    [err] = [e for e in events if e.type == BackendEventType.error]
    record = _record_for(buf, err)
    assert record["type"] == "error"
    assert record["text"] == "node failed to start"


async def test_no_active_buffer_leaves_every_event_seq_none(tmp_path: Path) -> None:
    transcript.set_active_buffer(None)
    ok = await _launch_and_wait(tmp_path, "t", {"summary": "ok"})
    failed = await _launch_and_wait(tmp_path, "t", None, stderr=b"boom", rc=FAILED_RC)
    events = ok.drain_events() + failed.drain_events()
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


async def test_a_failure_in_the_status_mapping_keeps_the_outcome_record(tmp_path: Path, buf) -> None:
    # 211: the outcome records are written before the status mapping runs. When that mapping raises,
    # the run's result record is still in the transcript (before 211 it was not), and the status is
    # the same error the reader loop has always reported.
    with patch.object(JunieBackend, "_apply_parsed_result", side_effect=RuntimeError("boom")):
        backend = await _launch_and_wait(tmp_path, "t", {"summary": "finished the task"})
    status = backend.get_status()
    assert status.state == "error"
    assert status.error_reason == "boom"
    records = buf.after(0, 100)
    assert [r["type"] for r in records if r["type"] == "result"] == ["result"]
    assert any(r["type"] == "error" and r["text"] == "junie reader error: boom" for r in records)
