"""Contract test for GET /status (spec 056, T013)."""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from performer.server import create_app
from performer.server.models import PerformerStatus


@pytest.mark.asyncio
async def test_status_returns_valid_payload() -> None:
    app = create_app(expected_token=None, version="test-1.0")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/status")
    assert response.status_code == 200
    body = response.json()

    parsed = PerformerStatus.model_validate(body)
    assert parsed.availability in {"starting", "idle", "busy", "draining"}
    assert isinstance(parsed.auth_enabled, bool)
    assert parsed.auth_enabled is False
    assert parsed.capabilities is not None


@pytest.mark.asyncio
async def test_status_reflects_auth_enabled() -> None:
    app = create_app(expected_token="secret")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(
            "/status", headers={"Authorization": "Bearer secret"}
        )
    assert response.status_code == 200
    assert response.json()["auth_enabled"] is True
