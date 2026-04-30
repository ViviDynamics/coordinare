"""Contract test for POST /jobs (spec 056, T014)."""

from __future__ import annotations

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from performer.server import create_app
from performer.server.models import (
    JobAcceptResponse,
    JobBusyResponse,
    JobInitPayload,
    JobResult,
)


def _payload(job_id: str = "job-1") -> dict:
    return {
        "job_id": job_id,
        "card_id": "card-1",
        "role": "performer",
        "backend": "claude_code",
        "persona": "default",
        "repo_url": "https://example.com/repo.git",
        "branch": "main",
    }


@pytest.mark.asyncio
async def test_submit_happy_path_returns_202() -> None:
    async def executor(_: JobInitPayload) -> JobResult:
        return JobResult(success=True, summary="ok")

    app = create_app(expected_token=None, executor=executor)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/jobs", json=_payload("job-1"))
    assert response.status_code == 202
    accept = JobAcceptResponse.model_validate(response.json())
    assert accept.job_id == "job-1"
    assert accept.accepted is True


@pytest.mark.asyncio
async def test_second_submission_while_busy_returns_409() -> None:
    gate = asyncio.Event()

    async def slow(_: JobInitPayload) -> JobResult:
        await gate.wait()
        return JobResult(success=True, summary="ok")

    app = create_app(expected_token=None, executor=slow)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        first = await client.post("/jobs", json=_payload("job-1"))
        assert first.status_code == 202
        second = await client.post("/jobs", json=_payload("job-2"))
        assert second.status_code == 409
        busy = JobBusyResponse.model_validate(second.json())
        assert busy.reason == "busy"
        assert busy.accepted is False
        gate.set()
        runner = app.state.runner
        if runner._task is not None:
            await runner._task
