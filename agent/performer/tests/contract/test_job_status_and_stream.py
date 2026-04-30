"""Contract test for GET /jobs/{id} and /stream (spec 056, T015)."""

from __future__ import annotations

import json

import pytest
from httpx import ASGITransport, AsyncClient

from performer.server import create_app
from performer.server.models import JobInitPayload, JobResult, JobStatus


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
async def test_get_job_returns_status() -> None:
    async def executor(_: JobInitPayload) -> JobResult:
        return JobResult(success=True, summary="ok")

    app = create_app(expected_token=None, executor=executor)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post("/jobs", json=_payload("job-1"))
        runner = app.state.runner
        if runner._task is not None:
            await runner._task
        response = await client.get("/jobs/job-1")

    assert response.status_code == 200
    status = JobStatus.model_validate(response.json())
    assert status.job_id == "job-1"
    assert status.state == "succeeded"


@pytest.mark.asyncio
async def test_get_job_unknown_returns_404() -> None:
    app = create_app(expected_token=None)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/jobs/nope")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_stream_emits_sse_events_until_terminal() -> None:
    async def executor(_: JobInitPayload) -> JobResult:
        return JobResult(success=True, summary="done")

    app = create_app(expected_token=None, executor=executor)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post("/jobs", json=_payload("job-1"))
        async with client.stream("GET", "/jobs/job-1/stream") as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            chunks: list[str] = []
            async for line in response.aiter_lines():
                if line.startswith("data: "):
                    chunks.append(line[len("data: ") :])
                if any(
                    json.loads(c).get("state") in {"succeeded", "failed", "cancelled"}
                    for c in chunks
                ):
                    break

    assert chunks, "expected at least one SSE event"
    states = [json.loads(c)["state"] for c in chunks]
    assert states[-1] == "succeeded"
    for chunk in chunks:
        JobStatus.model_validate(json.loads(chunk))
