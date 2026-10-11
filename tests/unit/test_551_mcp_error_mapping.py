"""Spec 551: DashboardClient error-mapping table and ToolResult fidelity."""

from __future__ import annotations

import httpx
import pytest
from pydantic import SecretStr

from coordinare.mcp.client import DashboardClient
from coordinare.mcp.config import McpServerConfig

TOKEN = "d" * 32


def make_config() -> McpServerConfig:
    return McpServerConfig(
        agent_token=SecretStr("a" * 32),
        dashboard_token=SecretStr(TOKEN),
    )


def make_client(handler) -> DashboardClient:
    return DashboardClient(make_config(), transport=httpx.MockTransport(handler))


async def test_success_maps_json_body() -> None:
    client = make_client(
        lambda request: httpx.Response(200, json={"hello": "world"}),
    )
    result = await client.get_json("/api/state", timeout_seconds=5.0)
    assert result.ok is True
    assert result.data == {"hello": "world"}
    assert result.error is None


async def test_bearer_token_sent_on_internal_hop() -> None:
    seen_headers = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen_headers["auth"] = request.headers.get("Authorization")
        return httpx.Response(200, json={})

    client = make_client(handler)
    await client.get_json("/api/state", timeout_seconds=5.0)
    assert seen_headers["auth"] == f"Bearer {TOKEN}"


@pytest.mark.parametrize("status", [401, 403])
async def test_auth_failed(status: int) -> None:
    client = make_client(lambda request: httpx.Response(status, text="no"))
    result = await client.get_json("/api/state", timeout_seconds=5.0)
    assert result.ok is False
    assert result.error is not None
    assert result.error.kind == "auth_failed"
    assert result.error.status == status


async def test_conflict_body_verbatim_with_recovery_hint() -> None:
    body = "config changed under you"
    client = make_client(lambda request: httpx.Response(409, text=body))
    result = await client.send_json(
        "PUT", "/api/config", json={"x": 1}, timeout_seconds=5.0,
    )
    assert result.ok is False
    assert result.error is not None
    assert result.error.kind == "conflict"
    assert result.error.status == 409
    assert result.error.detail == body
    assert "get_config" in result.error.recovery_hint


@pytest.mark.parametrize("status", [400, 422])
async def test_validation_field_detail_verbatim(status: int) -> None:
    body = '{"detail": [{"loc": ["port"], "msg": "bad"}]}'
    client = make_client(lambda request: httpx.Response(status, text=body))
    result = await client.send_json(
        "PUT", "/api/config", json={"port": -1}, timeout_seconds=5.0,
    )
    assert result.error is not None
    assert result.error.kind == "validation"
    assert result.error.status == status
    assert result.error.detail == body


async def test_daemon_error_truncates_body() -> None:
    body = "boom" * 500
    client = make_client(lambda request: httpx.Response(503, text=body))
    result = await client.get_json("/api/state", timeout_seconds=5.0)
    assert result.error is not None
    assert result.error.kind == "daemon_error"
    assert result.error.status == 503
    assert len(result.error.detail) < len(body)
    assert result.error.detail.startswith("boom")


async def test_timeout_maps_to_daemon_unreachable_status_zero() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out")

    client = make_client(handler)
    result = await client.get_json("/api/state", timeout_seconds=5.0)
    assert result.ok is False
    assert result.error is not None
    assert result.error.kind == "daemon_unreachable"
    assert result.error.status == 0


async def test_connection_error_maps_to_daemon_unreachable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    client = make_client(handler)
    result = await client.get_json("/api/state", timeout_seconds=5.0)
    assert result.error is not None
    assert result.error.kind == "daemon_unreachable"
    assert result.error.status == 0


async def test_get_text_returns_metrics_text() -> None:
    client = make_client(lambda request: httpx.Response(200, text="m 1\n"))
    result = await client.get_text("/metrics", timeout_seconds=5.0)
    assert result.ok is True
    assert result.data is not None
    assert result.data["text"] == "m 1\n"
