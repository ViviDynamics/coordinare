"""Contract test for POST /jobs/{id}/cancel (spec 056, T016)."""

from __future__ import annotations

import asyncio

import pytest
from httpx import ASGITransport, AsyncClient

from performer.server import create_app
from performer.server.models import CancelResponse, JobInitPayload, JobResult


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
async def test_cancel_running_job_is_honored() -> None:
    started = asyncio.Event()

    async def hang(_: JobInitPayload) -> JobResult:
        started.set()
        await asyncio.sleep(60)
        return JobResult(success=True, summary="never")

    app = create_app(expected_token=None, executor=hang)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post("/jobs", json=_payload("job-1"))
        await started.wait()
        response = await client.post("/jobs/job-1/cancel")

    assert response.status_code == 200
    parsed = CancelResponse.model_validate(response.json())
    assert parsed.honored is True


@pytest.mark.asyncio
async def test_cancel_unknown_job_returns_404() -> None:
    app = create_app(expected_token=None)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/jobs/nope/cancel")
    assert response.status_code == 404
