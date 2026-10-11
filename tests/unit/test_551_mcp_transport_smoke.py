"""Spec 551 T013: transport smoke test over streamable HTTP (US1).

In-process uvicorn serving the MCP ASGI app on loopback; the official ``mcp``
client connects with a bearer token and lists tools. Bad credentials get 401.
"""

from __future__ import annotations

import asyncio
import socket
import threading
from typing import Any

import pytest
import uvicorn
from mcp.client.session import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client
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


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _Server:
    """uvicorn in a daemon thread; started/stopped around each test."""

    def __init__(self, config: McpServerConfig) -> None:
        self.port = _free_port()
        self.app = build_asgi_app(config)
        self.server = uvicorn.Server(
            uvicorn.Config(
                self.app,
                host="127.0.0.1",
                port=self.port,
                log_level="error",
                lifespan="on",
            ),
        )
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    def __enter__(self) -> _Server:
        self.thread.start()
        for _ in range(100):
            if self.server.started:
                return self
            threading.Event().wait(0.05)
        raise RuntimeError("uvicorn did not start")

    def __exit__(self, *args: Any) -> None:
        self.server.should_exit = True
        self.thread.join(timeout=5)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/mcp"


async def _list_tools(url: str, token: str | None) -> list[Any]:
    headers = {"Authorization": f"Bearer {token}"} if token else None

    def build_http_client() -> Any:
        return create_mcp_http_client(headers=headers)

    async with streamable_http_client(url, http_client=build_http_client()) as streams:
        read_stream, write_stream = streams
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            tools = await session.list_tools()
            return tools.tools


def test_lists_tools_with_valid_token() -> None:
    with _Server(_make_config()) as server:
        tools = asyncio.run(_list_tools(server.url, AGENT_TOKEN))
        assert any(tool.name == "get_metrics" for tool in tools)


@pytest.mark.parametrize("token", [None, "wrong-token-value-0123456789abcd"])
def test_bad_credentials_refused_with_401(token: str | None) -> None:
    import httpx

    with _Server(_make_config()) as server:
        # Wire level: the 401 carries the structured body agents surface.
        response = httpx.post(
            server.url, headers={"Authorization": f"Bearer {token}"} if token else {},
        )
        assert response.status_code == 401
        assert response.json()["error"]["kind"] == "auth_failed"
        # Protocol level: the official client surfaces the refusal as an MCPError.
        with pytest.raises(Exception) as exc_info:
            asyncio.run(_list_tools(server.url, token))
        leaves = _flatten(exc_info.value)
        assert any(isinstance(e, MCPError) and e.error.code == -32603 for e in leaves)
