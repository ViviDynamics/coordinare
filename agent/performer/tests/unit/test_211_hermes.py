"""Spec 211: hermes backend events carry the seq of the transcript record they mirror."""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from performer import transcript
from performer.backends.hermes import HermesBackend
from performer.models import BackendEvent, BackendEventType, Score, Stand

API_KEY = "sk-test-211"
BUFFER_BYTES = 10_000_000
FAILED_RC = 1
ROUND_TRIP_SEQ = 7
REPLY = "reply line with a final answer. " * 12


@pytest.fixture
def buf():
    buffer = transcript.TranscriptBuffer("run-hermes-211", max_bytes=BUFFER_BYTES)
    transcript.set_active_buffer(buffer)
    yield buffer
    transcript.set_active_buffer(None)


@pytest.fixture
def hermes_env(monkeypatch):
    monkeypatch.setenv("HERMES_PROVIDER", "openai")
    monkeypatch.setenv("HERMES_API_KEY", API_KEY)
    monkeypatch.setenv("HERMES_MODEL", "gpt-4o")
    monkeypatch.delenv("HERMES_BASE_URL", raising=False)
    monkeypatch.delenv("HERMES_HOME", raising=False)


def _score(**kwargs) -> Score:
    defaults = dict(
        title="Transcript task",
        description="A short description.",
        acceptance_criteria=["Criterion A"],
        repo_url="https://github.com/org/repo",
        branch="feature/x",
        github_token="tok",
        persona_instructions="",
        role="implementer",
    )
    defaults.update(kwargs)
    return Score(**defaults)


def _proc(stdout: bytes = b"", stderr: bytes = b"", rc: int = 0) -> MagicMock:
    proc = MagicMock()
    proc.pid = 4242
    proc.returncode = rc
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.wait = AsyncMock(return_value=rc)
    return proc


async def _run(tmp_path: Path, score: Score, proc: MagicMock) -> HermesBackend:
    adapter = HermesBackend()
    with patch(
        "performer.backends.hermes.asyncio.create_subprocess_exec",
        new=AsyncMock(return_value=proc),
    ):
        await adapter.start(Stand(path=tmp_path, branch="feature/x"), score)
    assert adapter._reader_task is not None
    await adapter._reader_task
    return adapter


def _record_for(buf: transcript.TranscriptBuffer, event: BackendEvent) -> dict:
    assert event.transcript_seq is not None
    [record] = buf.after(event.transcript_seq - 1, 1)
    assert record["seq"] == event.transcript_seq
    return record


async def test_progress_event_points_at_the_final_reply_record(
    tmp_path: Path, hermes_env, buf,
) -> None:
    adapter = await _run(tmp_path, _score(), _proc(stdout=REPLY.encode()))
    events = adapter.drain_events()

    [progress] = [e for e in events if e.type == BackendEventType.progress]
    record = _record_for(buf, progress)
    assert record["type"] == "assistant_text"
    assert record["text"] == REPLY.strip()


async def test_nonzero_exit_error_event_points_at_error_record(
    tmp_path: Path, hermes_env, buf,
) -> None:
    stderr = b"traceback line that explains the failure"
    adapter = await _run(tmp_path, _score(), _proc(stderr=stderr, rc=FAILED_RC))
    events = adapter.drain_events()

    [err] = [e for e in events if e.type == BackendEventType.error]
    record = _record_for(buf, err)
    assert record["type"] == "error"
    assert record["text"].startswith(f"subprocess_exit:{FAILED_RC}")
    assert record["text"].endswith(stderr.decode())


async def test_malformed_output_error_event_points_at_error_record(
    tmp_path: Path, hermes_env, buf,
) -> None:
    prose = "I reviewed the diff and I think it is fine."
    adapter = await _run(tmp_path, _score(role="qa"), _proc(stdout=prose.encode(), rc=0))
    events = adapter.drain_events()

    [err] = [e for e in events if e.type == BackendEventType.error]
    assert err.text == "malformed_output"
    record = _record_for(buf, err)
    assert record["type"] == "error"
    assert record["text"].startswith("malformed_output")


async def test_no_active_buffer_leaves_every_event_seq_none(tmp_path: Path, hermes_env) -> None:
    transcript.set_active_buffer(None)
    ok = await _run(tmp_path, _score(), _proc(stdout=REPLY.encode()))
    failed = await _run(tmp_path, _score(), _proc(stderr=b"boom", rc=FAILED_RC))
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
