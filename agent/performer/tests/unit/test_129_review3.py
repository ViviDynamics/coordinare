"""Spec 129, third review, performer side: one test per finding.

Covers the status route that must never fail because of the transcript, the
per-record guards in capture and in serving, the size bounds on a record, and the
refreshed-secret handling.
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Iterator
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from performer import transcript
from performer.server import create_app
from performer.server.job_runner import JobRunner
from performer.server.models import JobInitPayload, JobResult

SECRET = "ghp_" + "w" * 36


@pytest.fixture
def buffer() -> Iterator[transcript.TranscriptBuffer]:
    buf = transcript.TranscriptBuffer("run-r3", max_bytes=50_000_000, secrets=lambda: [SECRET])
    transcript.set_active_buffer(buf)
    yield buf
    transcript.set_active_buffer(None)


def _payload(job_id: str) -> dict[str, Any]:
    return JobInitPayload(
        job_id=job_id,
        card_id="PVTI_r3",
        role="implementer",
        backend="claude_code",
        persona="coder",
        repo_url="https://github.com/acme/widgets",
        branch="main",
        transcript_max_bytes=50_000_000,
    ).model_dump(mode="json")


async def _executor(payload: JobInitPayload) -> JobResult:
    transcript.emit("user", text="prompt")
    transcript.emit("assistant_text", text="reply")
    return JobResult(success=True, summary="ok")


async def _wait_terminal(client: AsyncClient, job_id: str) -> None:
    for _ in range(500):
        body = (await client.get(f"/jobs/{job_id}")).json()
        if body["state"] in {"succeeded", "failed", "cancelled"}:
            return
        await asyncio.sleep(0.01)


# --- finding 1: the status route never fails because of the transcript ------------


async def test_a_failing_batch_still_returns_the_status_with_an_error_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Review item 1: GET /jobs?transcript_after= must not 500 on a bad record."""

    def explode(self: transcript.TranscriptBuffer, seq: int, limit: int) -> list[dict[str, Any]]:
        message = "cannot serve"
        raise RuntimeError(message)

    monkeypatch.setattr(transcript.TranscriptBuffer, "after", explode)
    app = create_app(expected_token=None, executor=_executor)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post("/jobs", json=_payload("job-r3-route"))
        await _wait_terminal(client, "job-r3-route")
        response = await client.get(
            "/jobs/job-r3-route", params={"transcript_after": 0, "transcript_limit": 10},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["state"] == "succeeded", "completion is still observed"
        assert body["transcript"] == []
        assert body["transcript_error"] == "RuntimeError"
        assert body["transcript_last_seq"] == 2, "the daemon can still tell how far to go"


# --- finding 1: a record that cannot be captured or served keeps its seq ------------


def test_a_record_that_cannot_be_redacted_is_served_as_a_withheld_marker_with_its_seq(
    buffer: transcript.TranscriptBuffer, monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_redact = transcript.redact_value

    def refuse_one(value: Any, secrets: Any) -> Any:
        if isinstance(value, str) and "BOOM" in value:
            message = "redaction refused"
            raise ValueError(message)
        return real_redact(value, secrets)

    monkeypatch.setattr(transcript, "redact_value", refuse_one)
    transcript.emit("tool_result", output_data="BOOM")
    transcript.emit("assistant_text", text="fine after")
    served = buffer.after(0, 10)
    assert [entry["seq"] for entry in served] == [1, 2]
    assert served[0]["type"] == "error"
    assert "withheld" in served[0]["text"]
    assert served[1]["text"] == "fine after"


def test_a_record_that_cannot_be_captured_keeps_its_seq_and_the_run_goes_on(
    buffer: transcript.TranscriptBuffer, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A value that cannot be serialised at capture is withheld, not dropped silently."""
    real_jsonable = transcript._jsonable

    def refuse_marker(value: Any) -> Any:
        if value == "unserialisable-marker":
            message = "cannot serialise"
            raise TypeError(message)
        return real_jsonable(value)

    monkeypatch.setattr(transcript, "_jsonable", refuse_marker)
    transcript.emit("tool_result", output_data="unserialisable-marker")
    transcript.emit("assistant_text", text="next")
    records = buffer.after(0, 10)
    assert [entry["seq"] for entry in records] == [1, 2]
    assert records[0]["type"] == "error"
    assert records[1]["text"] == "next"


# --- finding 9: one record is bounded before it is buffered --------------------------


def test_a_huge_text_field_is_cut_with_a_marker_before_buffering(
    buffer: transcript.TranscriptBuffer,
) -> None:
    transcript.emit("assistant_text", text="x" * (3 * 1024 * 1024))
    [record] = buffer.after(0, 10)
    assert len(record["text"]) < transcript.FIELD_LIMIT_CHARS + 200
    assert "transcript field truncated" in record["text"]


def test_a_record_too_large_even_after_field_bounds_has_its_payload_withheld(
    buffer: transcript.TranscriptBuffer,
) -> None:
    many = {f"k{index}": "v" * 200_000 for index in range(12)}
    transcript.emit("tool_result", output_data=many)
    [record] = buffer.after(0, 10)
    assert record["seq"] == 1
    served_size = len(json.dumps(record))
    assert served_size < 2 * transcript.RECORD_LIMIT_BYTES


# --- finding 8: the refreshed-secret dict is read under its lock --------------------


def test_refreshed_secrets_reach_the_redaction_set_of_the_running_job() -> None:
    """Review item 8: a PATCHed token is covered by the job's redaction at once."""

    async def scenario() -> None:
        release = asyncio.Event()

        async def held(payload: JobInitPayload) -> JobResult:
            transcript.emit("user", text="prompt")
            await release.wait()
            return JobResult(success=True, summary="ok")

        runner = JobRunner(held, resolver=None, required_secrets=())
        payload = JobInitPayload(
            job_id="job-r3-lock",
            card_id="PVTI_lock",
            role="implementer",
            backend="claude_code",
            persona="coder",
            repo_url="https://github.com/acme/widgets",
            branch="main",
            transcript_max_bytes=10_000_000,
        )
        await runner.submit(payload)
        await runner.refresh_secrets("job-r3-lock", {"GITHUB_TOKEN": "refreshed-token-value-77"})
        assert runner._transcript is not None
        assert "refreshed-token-value-77" in runner._transcript._secret_values()
        release.set()
        assert runner._task is not None
        await runner._task

    asyncio.run(scenario())


def test_a_refresh_during_a_secret_read_does_not_raise() -> None:
    """Review item 8: the dict is written on the loop while a worker thread reads it."""

    async def scenario() -> None:
        release = asyncio.Event()

        async def held(payload: JobInitPayload) -> JobResult:
            transcript.emit("user", text="prompt")
            await release.wait()
            return JobResult(success=True, summary="ok")

        runner = JobRunner(held, resolver=None, required_secrets=())
        payload = JobInitPayload(
            job_id="job-r3-race",
            card_id="PVTI_race",
            role="implementer",
            backend="claude_code",
            persona="coder",
            repo_url="https://github.com/acme/widgets",
            branch="main",
            transcript_max_bytes=10_000_000,
        )
        await runner.submit(payload)
        assert runner._transcript is not None
        buf = runner._transcript
        errors: list[BaseException] = []

        def reader() -> None:
            try:
                for _ in range(2000):
                    buf._secret_values()
            except RuntimeError as exc:  # "dictionary changed size during iteration"
                errors.append(exc)

        thread = threading.Thread(target=reader)
        thread.start()
        for index in range(2000):
            await runner.refresh_secrets("job-r3-race", {f"KEY_{index}": f"value-{index:08d}"})
        thread.join()
        release.set()
        assert runner._task is not None
        await runner._task
        assert errors == []

    asyncio.run(scenario())
