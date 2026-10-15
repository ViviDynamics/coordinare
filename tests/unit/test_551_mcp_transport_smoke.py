"""Spec 551 T013: transport smoke test over streamable HTTP (US1).

The official ``mcp`` client connects to the ASGI app in process with a bearer
token and lists tools. Bad credentials get 401. No sockets are required.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.shared.exceptions import MCPError
from pydantic import SecretStr

from coordinare.mcp.config import McpServerConfig
from coordinare.mcp.server import build_asgi_app

AGENT_TOKEN = "a" * 32
DASH_TOKEN = "d" * 32


def _flatten(exc: BaseException) -> list[BaseException]:
    if isinstance(exc, ExceptionGroup):
        return [e for sub in exc.exceptions for e in _flatten(sub)]
    return [exc]


def _make_config(**overrides: Any) -> McpServerConfig:
    values: dict[str, Any] = {
        "agent_token": SecretStr(AGENT_TOKEN),
        "dashboard_token": SecretStr(DASH_TOKEN),
    }
    values.update(overrides)
    return McpServerConfig(**values)


async def _list_tools(app: Any, token: str | None) -> list[Any]:
    headers = {"Authorization": f"Bearer {token}"} if token else None

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), headers=headers,
    ) as http, streamable_http_client("http://127.0.0.1:8091/mcp", http_client=http) as streams:
        read_stream, write_stream = streams
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            tools = await session.list_tools()
            return tools.tools


@pytest.mark.asyncio
async def test_lists_tools_with_valid_token() -> None:
    app = build_asgi_app(_make_config())
    async with app.router.lifespan_context(app):
        tools = await _list_tools(app, AGENT_TOKEN)
    assert any(tool.name == "get_metrics" for tool in tools)
    assert any(tool.name == "query_events" for tool in tools)


@pytest.mark.asyncio
@pytest.mark.parametrize("token", [None, "wrong-token-value-0123456789abcd"])
async def test_bad_credentials_refused_with_401(token: str | None) -> None:
    app = build_asgi_app(_make_config())
    async with app.router.lifespan_context(app), httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8091",
    ) as client:
        response = await client.post(
            "/mcp", headers={"Authorization": f"Bearer {token}"} if token else {},
        )
        assert response.status_code == 401
        assert response.json()["error"]["kind"] == "auth_failed"
        with pytest.raises(Exception) as exc_info:
            await _list_tools(app, token)
        leaves = _flatten(exc_info.value)
        assert any(isinstance(e, MCPError) and e.error.code == -32603 for e in leaves)
