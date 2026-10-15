"""Spec 551 T016: get_metrics contract test against the real health app (US1).

The real ``create_health_app`` serves ``/metrics`` in-process; the MCP server
proxies it verbatim. REST drift breaks this test in the same commit.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from pydantic import SecretStr

from coordinare.health import create_health_app
from coordinare.mcp.client import DashboardClient, set_client
from coordinare.mcp.config import McpServerConfig
from coordinare.mcp.server import build_asgi_app


def _make_config(health_url: str) -> McpServerConfig:
    return McpServerConfig(
        agent_token=SecretStr("a" * 32),
        dashboard_token=SecretStr("d" * 32),
        health_url=health_url,
    )


async def _call_get_metrics(app: Any, token: str = "a" * 32) -> dict[str, Any]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), headers={"Authorization": f"Bearer {token}"},
    ) as http, streamable_http_client("http://127.0.0.1:8091/mcp", http_client=http) as streams:
        read_stream, write_stream = streams
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            result = await session.call_tool("get_metrics", {})
            assert result.content, "get_metrics returned no content"
            return json.loads(result.content[0].text)


@pytest.mark.asyncio
async def test_get_metrics_proxies_real_health_app() -> None:
    health_app = create_health_app(MagicMock())
    config = _make_config("http://health.test")
    app = build_asgi_app(config)
    set_client(DashboardClient(config, transport=httpx.ASGITransport(app=health_app)))
    async with app.router.lifespan_context(app):
        payload = await _call_get_metrics(app)
    assert payload["ok"] is True
    assert payload.get("error") is None
    text = payload["data"]["text"]
    assert isinstance(text, str)
    assert text.strip()
