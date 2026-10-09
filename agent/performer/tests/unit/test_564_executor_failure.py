"""Executor failures publish a terminal result without rendering traceback locals."""
from __future__ import annotations

import pytest
from structlog.testing import capture_logs

from performer.server.job_runner import JobRunner
from performer.server.models import JobInitPayload, JobResult


@pytest.mark.asyncio
async def test_executor_failure_has_bounded_log_and_terminal_result() -> None:
    async def boom(payload: JobInitPayload) -> JobResult:
        raise RuntimeError("git failed with ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")

    payload = JobInitPayload(
        job_id="failed-push", card_id="sample", role="performer", backend="claude_code",
        persona="default", repo_url="https://example.com/sample.git", branch="feature",
    )
    runner = JobRunner(boom)
    with capture_logs() as events:
        await runner.submit(payload)
        assert runner._task is not None
        await runner._task
    status = runner.get(payload.job_id)
    assert status.state == "failed"
    assert status.finished_at is not None
    assert status.result is not None
    assert status.result.error_code == "executor_error"
    assert "ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA" not in status.result.summary
    event = next(e for e in events if e["event"] == "job_executor_failed")
    assert not event.get("exc_info")
    assert event["error_type"] == "RuntimeError"
    assert "ghp_AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA" not in str(event)
