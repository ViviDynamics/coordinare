"""Spec 129: the performer's job route serves transcript batches by cursor."""
from __future__ import annotations

import asyncio
from typing import Any

from httpx import ASGITransport, AsyncClient

from performer import transcript
from performer.server import create_app
from performer.server.models import JobInitPayload, JobResult


def _job(job_id: str, max_bytes: int | None) -> dict[str, Any]:
    payload = JobInitPayload(
        job_id=job_id,
        card_id="PVTI_route",
        role="implementer",
        backend="claude_code",
        persona="coder",
        repo_url="https://github.com/acme/widgets",
        branch="main",
        transcript_max_bytes=max_bytes,
    )
    return payload.model_dump(mode="json")


async def _executor(payload: JobInitPayload) -> JobResult:
    transcript.emit("user", text="the prompt")
    for i in range(4):
        transcript.emit("assistant_text", text=f"reply {i}")
    return JobResult(success=True, summary="ok")


async def _wait_terminal(client: AsyncClient, job_id: str) -> dict[str, Any]:
    body: dict[str, Any] = {}
    for _ in range(500):
        response = await client.get(f"/jobs/{job_id}")
        body = response.json()
        if body["state"] in {"succeeded", "failed", "cancelled"}:
            return body
        await asyncio.sleep(0.01)
    return body


async def test_cursor_pages_through_records_over_the_job_route() -> None:
    app = create_app(expected_token=None, executor=_executor)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        accepted = await client.post("/jobs", json=_job("job-route-1", max_bytes=1_000_000))
        assert accepted.status_code == 202
        await _wait_terminal(client, "job-route-1")

        first = (await client.get(
            "/jobs/job-route-1", params={"transcript_after": 0, "transcript_limit": 2},
        )).json()
        assert first["transcript_last_seq"] == 5
        assert [r["seq"] for r in first["transcript"]] == [1, 2]
        assert first["transcript"][0]["type"] == "user"

        rest = (await client.get(
            "/jobs/job-route-1", params={"transcript_after": 2, "transcript_limit": 500},
        )).json()
        assert [r["seq"] for r in rest["transcript"]] == [3, 4, 5]

        status_only = (await client.get("/jobs/job-route-1")).json()
        assert status_only["transcript_last_seq"] == 5
        assert status_only["transcript"] == []


async def test_disabled_job_reports_no_transcript() -> None:
    app = create_app(expected_token=None, executor=_executor)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post("/jobs", json=_job("job-route-off", max_bytes=None))
        await _wait_terminal(client, "job-route-off")
        body = (await client.get(
            "/jobs/job-route-off", params={"transcript_after": 0},
        )).json()
        assert body["transcript_last_seq"] is None
        assert body["transcript"] == []


async def test_transcript_limit_is_bounded_by_the_route() -> None:
    app = create_app(expected_token=None, executor=_executor)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        await client.post("/jobs", json=_job("job-route-limit", max_bytes=1_000_000))
        await _wait_terminal(client, "job-route-limit")
        response = await client.get(
            "/jobs/job-route-limit", params={"transcript_after": 0, "transcript_limit": 9999},
        )
        assert response.status_code == 422
