"""Unit tests for the performer-side JobRunner (spec 056, T017)."""

from __future__ import annotations

import asyncio

import pytest

from performer.server.job_runner import JobNotFoundError, JobRunner
from performer.server.models import (
    JobAcceptResponse,
    JobBusyResponse,
    JobInitPayload,
    JobResult,
)


def _payload(job_id: str = "job-1") -> JobInitPayload:
    return JobInitPayload(
        job_id=job_id,
        card_id="card-1",
        role="performer",
        backend="claude_code",
        persona="default",
        repo_url="https://example.com/repo.git",
        branch="main",
    )


@pytest.mark.asyncio
async def test_single_slot_second_submit_busy() -> None:
    gate = asyncio.Event()

    async def slow_executor(_: JobInitPayload) -> JobResult:
        await gate.wait()
        return JobResult(success=True, summary="ok")

    runner = JobRunner(slow_executor)
    first = await runner.submit(_payload("job-1"))
    assert isinstance(first, JobAcceptResponse)

    second = await runner.submit(_payload("job-2"))
    assert isinstance(second, JobBusyResponse)
    assert second.reason == "busy"

    gate.set()
    assert runner._task is not None
    await runner._task


@pytest.mark.asyncio
async def test_terminal_state_populates_result() -> None:
    async def executor(_: JobInitPayload) -> JobResult:
        return JobResult(success=True, summary="done")

    runner = JobRunner(executor)
    await runner.submit(_payload())
    assert runner._task is not None
    await runner._task

    status = runner.get("job-1")
    assert status.state == "succeeded"
    assert status.result is not None
    assert status.result.success is True
    assert status.result.summary == "done"
    assert status.finished_at is not None
    assert status.progress_pct == 100


@pytest.mark.asyncio
async def test_failed_executor_marks_failed() -> None:
    async def boom(_: JobInitPayload) -> JobResult:
        raise RuntimeError("kaboom")

    runner = JobRunner(boom)
    await runner.submit(_payload())
    assert runner._task is not None
    await runner._task

    status = runner.get("job-1")
    assert status.state == "failed"
    assert status.result is not None
    assert status.result.success is False
    assert status.result.error_code == "executor_error"


@pytest.mark.asyncio
async def test_cancel_transitions_to_cancelled() -> None:
    started = asyncio.Event()

    async def hang(_: JobInitPayload) -> JobResult:
        started.set()
        await asyncio.sleep(60)
        return JobResult(success=True, summary="never")

    runner = JobRunner(hang)
    await runner.submit(_payload())
    await started.wait()

    response = await runner.cancel("job-1")
    assert response.honored is True

    status = runner.get("job-1")
    assert status.state == "cancelled"
    assert status.result is not None
    assert status.result.error_code == "cancelled"


@pytest.mark.asyncio
async def test_cancel_unknown_job_raises() -> None:
    async def executor(_: JobInitPayload) -> JobResult:
        return JobResult(success=True, summary="ok")

    runner = JobRunner(executor)
    with pytest.raises(JobNotFoundError):
        await runner.cancel("nonexistent")


@pytest.mark.asyncio
async def test_get_unknown_job_raises() -> None:
    async def executor(_: JobInitPayload) -> JobResult:
        return JobResult(success=True, summary="ok")

    runner = JobRunner(executor)
    with pytest.raises(JobNotFoundError):
        runner.get("nonexistent")


@pytest.mark.asyncio
async def test_submit_after_completion_accepts_new_job() -> None:
    async def executor(_: JobInitPayload) -> JobResult:
        return JobResult(success=True, summary="ok")

    runner = JobRunner(executor)
    await runner.submit(_payload("job-1"))
    assert runner._task is not None
    await runner._task

    second = await runner.submit(_payload("job-2"))
    assert isinstance(second, JobAcceptResponse)
    assert runner._task is not None
    await runner._task
    assert runner.get("job-2").state == "succeeded"
