"""US5 / Spec 073 Phase 9: PATCH /jobs/{job_id}/secrets propagates new secrets.

Performer side: coordinare PATCHes a refreshed github_token mid-job. The
runner must store it where the running _perform_job loop can pick it up
before the next status tick (so push_branch sees the fresh token).

Regression for website #70.
"""

from __future__ import annotations

import asyncio

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from performer.server.job_runner import (
    JobRunner,
    _refreshed_secrets_var,
)
from performer.server.models import JobInitPayload, JobResult
from performer.server.routes import register_routes


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


def _app(runner: JobRunner) -> FastAPI:
    app = FastAPI()
    app.state.runner = runner
    app.state.capabilities = None
    app.state.auth_enabled = False
    app.state.version = "test"
    register_routes(app)
    return app


@pytest.mark.asyncio
async def test_refresh_secrets_updates_running_job_var() -> None:
    """A running job sees PATCH-delivered secrets via _refreshed_secrets_var."""

    observed: list[dict[str, str] | None] = []
    first_observed = asyncio.Event()
    proceed = asyncio.Event()

    async def executor(_: JobInitPayload) -> JobResult:
        # Simulate one tick of the status loop reading the contextvar.
        first = _refreshed_secrets_var.get()
        observed.append(dict(first) if first is not None else None)
        first_observed.set()
        await proceed.wait()
        second = _refreshed_secrets_var.get()
        observed.append(dict(second) if second is not None else None)
        return JobResult(success=True, summary="ok")

    runner = JobRunner(executor)
    await runner.submit(_payload("job-1"))

    # Wait until the executor has captured its first observation before
    # refreshing — otherwise the test races against the executor task.
    await first_observed.wait()

    # Refresh secrets while the job is mid-flight.
    await runner.refresh_secrets("job-1", {"github_token": "ghs_fresh"})

    proceed.set()
    assert runner._task is not None
    await runner._task

    # First observation was before refresh — empty dict (var was set to the
    # runner's mutable holder, which starts empty). Second after refresh.
    assert observed[0] == {}
    assert observed[1] is not None
    assert observed[1].get("github_token") == "ghs_fresh"


def test_patch_endpoint_calls_refresh_secrets(monkeypatch) -> None:
    """PATCH /jobs/{id}/secrets routes through JobRunner.refresh_secrets."""

    calls: list[tuple[str, dict[str, str]]] = []

    async def executor(_: JobInitPayload) -> JobResult:
        return JobResult(success=True, summary="ok")

    runner = JobRunner(executor)

    async def fake_refresh(job_id: str, secrets: dict[str, str]) -> None:
        calls.append((job_id, secrets))

    monkeypatch.setattr(runner, "refresh_secrets", fake_refresh)
    client = TestClient(_app(runner))

    r = client.patch(
        "/jobs/job-1/secrets",
        json={"secrets": {"github_token": "ghs_refreshed"}},
    )
    assert r.status_code == 204, r.text
    assert calls == [("job-1", {"github_token": "ghs_refreshed"})]


def test_patch_endpoint_404_for_unknown_job() -> None:
    async def executor(_: JobInitPayload) -> JobResult:
        return JobResult(success=True, summary="ok")

    runner = JobRunner(executor)
    client = TestClient(_app(runner))

    r = client.patch(
        "/jobs/no-such-job/secrets",
        json={"secrets": {"github_token": "ghs_x"}},
    )
    assert r.status_code == 404
