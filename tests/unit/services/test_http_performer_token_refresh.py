"""US5 / Spec 073 Phase 9: HTTP performer must forward refreshed github_token.

Coordinare side: when ``monitor_performer`` passes a refreshed token via the
``payload`` kwarg to ``HTTPPerformerService.check_status``, the service must
PATCH it to ``/jobs/{job_id}/secrets`` on the performer before polling status,
so the in-flight job picks up the new token before ``push_branch`` runs.

Regression for #70 — GitHub App tokens expire after 1h and the HTTP path
previously dropped the refreshed value on the floor.
"""

from __future__ import annotations

from datetime import datetime

import httpx
import pytest

from coordinare.models.performer_endpoint import PerformerEndpointConfig
from coordinare.services.http_performer_service import HTTPPerformerService
from coordinare.transport.http_transport import PerformerHTTPClient


def _ephemeral_config() -> PerformerEndpointConfig:
    return PerformerEndpointConfig.model_validate(
        {
            "id": "perf-e1",
            "mode": "ephemeral",
            "roles": ["implementing"],
            "image": "performer:base",
        }
    )


def _client(handler) -> PerformerHTTPClient:
    return PerformerHTTPClient(
        "http://127.0.0.1:8080",
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )


@pytest.mark.asyncio
async def test_check_status_forwards_refreshed_github_token() -> None:
    recorded: list[tuple[str, str, dict | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = None
        if request.content:
            import json as _json

            try:
                body = _json.loads(request.content)
            except Exception:
                body = None
        recorded.append((request.method, request.url.path, body))
        if request.method == "PATCH":
            return httpx.Response(204)
        # GET /jobs/{id}
        return httpx.Response(
            200,
            json={
                "job_id": "job-T",
                "state": "running",
                "started_at": "2026-05-26T00:00:00Z",
            },
        )

    svc = HTTPPerformerService(_ephemeral_config(), client=_client(handler))

    await svc.check_status(
        "job-T", payload={"github_token": "ghs_refreshed_xyz"}
    )

    patches = [r for r in recorded if r[0] == "PATCH"]
    assert len(patches) == 1, f"expected exactly one PATCH, got {recorded}"
    _method, path, body = patches[0]
    assert path == "/jobs/job-T/secrets"
    assert body == {"secrets": {"github_token": "ghs_refreshed_xyz"}}

    # PATCH must precede GET so the running job sees the token before push.
    patch_idx = next(i for i, r in enumerate(recorded) if r[0] == "PATCH")
    get_idx = next(i for i, r in enumerate(recorded) if r[0] == "GET")
    assert patch_idx < get_idx


@pytest.mark.asyncio
async def test_check_status_without_payload_does_not_patch() -> None:
    recorded: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        recorded.append(request.method)
        return httpx.Response(
            200,
            json={
                "job_id": "job-T",
                "state": "running",
                "started_at": "2026-05-26T00:00:00Z",
            },
        )

    svc = HTTPPerformerService(_ephemeral_config(), client=_client(handler))

    await svc.check_status("job-T")

    assert "PATCH" not in recorded


@pytest.mark.asyncio
async def test_check_status_patch_failure_does_not_block_status_poll() -> None:
    """A failed token PATCH must log + continue; status poll must still run."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PATCH":
            return httpx.Response(500, json={"detail": "boom"})
        return httpx.Response(
            200,
            json={
                "job_id": "job-T",
                "state": "running",
                "started_at": "2026-05-26T00:00:00Z",
            },
        )

    svc = HTTPPerformerService(_ephemeral_config(), client=_client(handler))

    # Should not raise — refresh is best-effort.
    result = await svc.check_status(
        "job-T", payload={"github_token": "ghs_new"}
    )
    assert result["status"] in {"ok", "in_progress", "running", "working"}


# ---------------------------------------------------------------------------
# 088 US6 (FR-012): retry-once-then-degrade on secret-refresh failure
# ---------------------------------------------------------------------------


def _running_job_response() -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "job_id": "job-T",
            "state": "running",
            "started_at": "2026-05-26T00:00:00Z",
        },
    )


@pytest.mark.asyncio
async def test_check_status_patch_failure_retries_once_then_degrades() -> None:
    """088 US6: a failed token PATCH is retried once immediately; a second
    failure marks the session degraded (``secret_refresh_failed_at``) and
    emits ``http_performer.secret_refresh_degraded`` at error level — but the
    status poll still runs (the session continues, it is not killed)."""
    import structlog.testing

    patch_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal patch_count
        if request.method == "PATCH":
            patch_count += 1
            return httpx.Response(500, json={"detail": "boom"})
        return _running_job_response()

    svc = HTTPPerformerService(_ephemeral_config(), client=_client(handler))

    with structlog.testing.capture_logs() as cap_logs:
        result = await svc.check_status(
            "job-T", payload={"github_token": "ghs_new"}
        )

    assert patch_count == 2, "refresh failure must be retried exactly once"

    degraded = [
        e for e in cap_logs
        if e["event"] == "http_performer.secret_refresh_degraded"
    ]
    assert len(degraded) == 1, f"expected one degraded event, got {cap_logs}"
    assert degraded[0]["log_level"] == "error"
    assert degraded[0]["performer_id"] == "perf-e1"

    failed_at = svc.secret_refresh_failed_at("job-T")
    assert isinstance(failed_at, datetime)

    # Session continues — the status poll still ran and reported working.
    assert result["status"] == "working"


@pytest.mark.asyncio
async def test_check_status_patch_transient_failure_retry_success_is_clean() -> None:
    """088 US6: a single transient PATCH failure followed by a retry success
    leaves the session clean — no degraded flag, no degraded event."""
    import structlog.testing

    patch_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal patch_count
        if request.method == "PATCH":
            patch_count += 1
            if patch_count == 1:
                return httpx.Response(500, json={"detail": "transient"})
            return httpx.Response(204)
        return _running_job_response()

    svc = HTTPPerformerService(_ephemeral_config(), client=_client(handler))

    with structlog.testing.capture_logs() as cap_logs:
        result = await svc.check_status(
            "job-T", payload={"github_token": "ghs_new"}
        )

    assert patch_count == 2, "first failure must trigger exactly one retry"
    assert svc.secret_refresh_failed_at("job-T") is None
    assert not [
        e for e in cap_logs
        if e["event"] == "http_performer.secret_refresh_degraded"
    ]
    assert result["status"] == "working"


@pytest.mark.asyncio
async def test_check_status_patch_success_does_not_retry() -> None:
    """088 US6: a first-attempt PATCH success must not produce a second PATCH."""
    patch_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal patch_count
        if request.method == "PATCH":
            patch_count += 1
            return httpx.Response(204)
        return _running_job_response()

    svc = HTTPPerformerService(_ephemeral_config(), client=_client(handler))
    await svc.check_status("job-T", payload={"github_token": "ghs_new"})

    assert patch_count == 1
    assert svc.secret_refresh_failed_at("job-T") is None
