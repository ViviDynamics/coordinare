"""Unit tests for the coordinare-side HTTP performer transport (spec 056, T019)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from coordinare.models.performer_endpoint import (
    CancelResponse,
    JobAcceptResponse,
    JobBusyResponse,
    JobInitPayload,
    JobResult,
    JobStatus,
    PerformerCapabilities,
    PerformerStatus,
)
from coordinare.transport.http_transport import (
    PerformerAuthError,
    PerformerHTTPClient,
    TransportError,
    TransportTimeoutError,
    utc_now,
)


def _started_at() -> str:
    return datetime.now(UTC).isoformat()


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


def _client_with(
    handler: httpx.MockTransport, *, auth_token: str | None = None
) -> PerformerHTTPClient:
    return PerformerHTTPClient(
        "http://performer.test",
        auth_token=auth_token,
        client=httpx.AsyncClient(transport=handler),
    )


@pytest.mark.asyncio
async def test_get_status_round_trips() -> None:
    body: dict[str, Any] = {
        "availability": "idle",
        "capabilities": {"backends": ["claude_code"], "tool_flags": ["git"]},
        "auth_enabled": False,
        "current_job_id": None,
        "version": "test",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/status"
        return httpx.Response(200, json=body)

    client = _client_with(httpx.MockTransport(handler))
    status = await client.get_status()
    assert isinstance(status, PerformerStatus)
    assert status.availability == "idle"
    assert isinstance(status.capabilities, PerformerCapabilities)
    await client.aclose()


@pytest.mark.asyncio
async def test_post_job_202_returns_accept() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/jobs"
        return httpx.Response(
            202, json={"accepted": True, "job_id": "job-1", "started_at": _started_at()}
        )

    client = _client_with(httpx.MockTransport(handler))
    response = await client.post_job(_payload())
    assert isinstance(response, JobAcceptResponse)
    assert response.job_id == "job-1"
    await client.aclose()


@pytest.mark.asyncio
async def test_post_job_409_returns_busy() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409, json={"accepted": False, "reason": "busy", "retry_after_s": 5}
        )

    client = _client_with(httpx.MockTransport(handler))
    response = await client.post_job(_payload())
    assert isinstance(response, JobBusyResponse)
    assert response.reason == "busy"
    await client.aclose()


@pytest.mark.asyncio
async def test_get_job_round_trips() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/jobs/job-1"
        return httpx.Response(
            200,
            json={
                "job_id": "job-1",
                "state": "succeeded",
                "started_at": _started_at(),
                "finished_at": _started_at(),
                "result": {"success": True, "summary": "ok"},
            },
        )

    client = _client_with(httpx.MockTransport(handler))
    status = await client.get_job("job-1")
    assert isinstance(status, JobStatus)
    assert status.state == "succeeded"
    assert isinstance(status.result, JobResult)
    await client.aclose()


@pytest.mark.asyncio
async def test_cancel_job_uses_post() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["path"] = request.url.path
        return httpx.Response(200, json={"honored": True})

    client = _client_with(httpx.MockTransport(handler))
    response = await client.cancel_job("job-1")
    assert captured == {"method": "POST", "path": "/jobs/job-1/cancel"}
    assert isinstance(response, CancelResponse)
    assert response.honored is True
    await client.aclose()


@pytest.mark.asyncio
async def test_stream_job_yields_status_updates() -> None:
    started = _started_at()
    events = [
        f'data: {{"job_id": "job-1", "state": "running", "started_at": "{started}"}}\n\n',
        f'data: {{"job_id": "job-1", "state": "succeeded", "started_at": "{started}", '
        f'"finished_at": "{started}", "result": {{"success": true, "summary": "ok"}}}}\n\n',
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/jobs/job-1/stream"
        return httpx.Response(
            200,
            content="".join(events).encode(),
            headers={"content-type": "text/event-stream"},
        )

    client = _client_with(httpx.MockTransport(handler))
    statuses: list[JobStatus] = []
    async for status in client.stream_job("job-1"):
        statuses.append(status)
    assert [s.state for s in statuses] == ["running", "succeeded"]
    await client.aclose()


@pytest.mark.asyncio
async def test_auth_token_is_sent_and_401_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.headers.get("authorization") != "Bearer secret":
            return httpx.Response(401, json={"error": "unauthorized"})
        return httpx.Response(
            200,
            json={
                "availability": "idle",
                "capabilities": {"backends": [], "tool_flags": []},
                "auth_enabled": True,
            },
        )

    good = _client_with(httpx.MockTransport(handler), auth_token="secret")
    status = await good.get_status()
    assert status.auth_enabled is True
    await good.aclose()

    bad = _client_with(httpx.MockTransport(handler), auth_token="wrong")
    with pytest.raises(PerformerAuthError):
        await bad.get_status()
    await bad.aclose()


@pytest.mark.asyncio
async def test_request_raises_transport_timeout_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    client = _client_with(httpx.MockTransport(handler))
    with pytest.raises(TransportTimeoutError):
        await client.get_status()
    await client.aclose()


@pytest.mark.asyncio
async def test_post_job_unexpected_status_raises_transport_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "server error"})

    client = _client_with(httpx.MockTransport(handler))
    with pytest.raises(httpx.HTTPStatusError):
        await client.post_job(_payload())
    await client.aclose()


@pytest.mark.asyncio
async def test_post_job_unexpected_2xx_raises_transport_error() -> None:
    """A 200 OK (not 202/409) falls through to the explicit TransportError raise."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True})

    client = _client_with(httpx.MockTransport(handler))
    with pytest.raises(TransportError):
        await client.post_job(_payload())
    await client.aclose()


@pytest.mark.asyncio
async def test_async_context_manager_works() -> None:
    """__aenter__ and __aexit__ allow use as an async context manager."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "availability": "idle",
                "capabilities": {"backends": [], "tool_flags": []},
                "auth_enabled": False,
            },
        )

    async with PerformerHTTPClient(
        "http://performer.test",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    ) as client:
        status = await client.get_status()
    assert status.availability == "idle"


@pytest.mark.asyncio
async def test_stream_job_401_raises_auth_error() -> None:
    """stream_job raises PerformerAuthError on 401 response."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "unauthorized"})

    client = _client_with(httpx.MockTransport(handler))
    with pytest.raises(PerformerAuthError):
        async for _ in client.stream_job("job-1"):
            pass
    await client.aclose()


def test_utc_now_returns_datetime() -> None:
    """utc_now is a thin wrapper for monkeypatching in tests."""
    from datetime import datetime as _datetime
    result = utc_now()
    assert isinstance(result, _datetime)
