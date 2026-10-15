"""Unit tests for the performer-side JobRunner (spec 056, T017)."""

from __future__ import annotations

import asyncio
import signal
from unittest.mock import MagicMock

import pytest

import performer.server.job_runner as job_runner_mod

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


CANCEL_TEST_GRACE_SECONDS = 0.01
CANCEL_TEST_DEADLINE_SECONDS = 0.1
BACKEND_TEST_PID = 12345


@pytest.mark.asyncio
async def test_cancel_forces_stubborn_task_and_frees_slot_without_stale_updates(monkeypatch) -> None:
    monkeypatch.setattr(job_runner_mod, "JOB_CANCEL_TIMEOUT_SECONDS", CANCEL_TEST_GRACE_SECONDS, raising=False)
    started = asyncio.Event()
    cancelled = asyncio.Event()
    release = asyncio.Event()
    new_started = asyncio.Event()
    new_release = asyncio.Event()

    async def stubborn(payload: JobInitPayload) -> JobResult:
        if payload.job_id == "job-2":
            new_started.set()
            await new_release.wait()
            return JobResult(success=True, summary="new result")
        started.set()
        push = job_runner_mod._progress_cb_var.get()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            cancelled.set()
            await release.wait()
        if push is not None:
            push([{"type": "progress", "text": "stale progress"}], None)
        return JobResult(success=True, summary="stale result")

    logger = MagicMock()
    monkeypatch.setattr(job_runner_mod, "log", logger)
    runner = JobRunner(stubborn)
    await runner.submit(_payload())
    old_task = runner._task
    await started.wait()
    cancel_task = asyncio.create_task(runner.cancel("job-1"))
    try:
        done, _ = await asyncio.wait({cancel_task}, timeout=CANCEL_TEST_DEADLINE_SECONDS)
        assert cancel_task in done, "cancel must return even if the executor ignores cancellation"
        assert cancel_task.result().honored
        assert cancelled.is_set()
        assert runner.current_job_id is None
        assert runner.get("job-1").state == "cancelled"
        logger.warning.assert_any_call("job.cancel_forced", job_id="job-1")
        accepted = await runner.submit(_payload("job-2"))
        assert isinstance(accepted, JobAcceptResponse)
        await new_started.wait()
        release.set()
        assert old_task is not None
        await old_task
        assert runner.current_job_id == "job-2"
        assert runner.get("job-2").state == "running"
        assert runner.get("job-2").events == []
    finally:
        release.set()
        new_release.set()
        await cancel_task
        if runner._task is not None:
            await runner._task


@pytest.mark.asyncio
async def test_forced_cancel_kills_registered_backend_process_group(monkeypatch) -> None:
    monkeypatch.setattr(job_runner_mod, "JOB_CANCEL_TIMEOUT_SECONDS", CANCEL_TEST_GRACE_SECONDS, raising=False)
    started = asyncio.Event()
    release = asyncio.Event()
    backend = MagicMock()
    backend._proc.pid = BACKEND_TEST_PID
    backend._proc.returncode = None
    kill = MagicMock()
    monkeypatch.setattr("os.killpg", kill)
    monkeypatch.setattr("os.getpgid", lambda _pid: BACKEND_TEST_PID)

    async def stubborn(_: JobInitPayload) -> JobResult:
        job_runner_mod.register_cancel_backend(backend)
        started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release.wait()
        return JobResult(success=True, summary="done")

    runner = JobRunner(stubborn)
    await runner.submit(_payload())
    assert runner._task is not None
    old_task = runner._task
    # Wait for either startup or an early executor failure so the red test cannot hang.
    startup = asyncio.create_task(started.wait())
    await asyncio.wait({startup, runner._task}, return_when=asyncio.FIRST_COMPLETED)
    cancel_task = asyncio.create_task(runner.cancel("job-1"))
    try:
        done, _ = await asyncio.wait({cancel_task}, timeout=CANCEL_TEST_DEADLINE_SECONDS)
        assert cancel_task in done
        kill.assert_called_once_with(BACKEND_TEST_PID, signal.SIGKILL)
    finally:
        release.set()
        startup.cancel()
        await asyncio.gather(startup, return_exceptions=True)
        await cancel_task
        await old_task
