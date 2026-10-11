"""Spec 551 T016: get_metrics contract test against the real health app (US1).

The real ``create_health_app`` serves ``/metrics`` in-process; the MCP server
proxies it verbatim. REST drift breaks this test in the same commit.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
from typing import Any
from unittest.mock import MagicMock

import uvicorn
from mcp.client.session import ClientSession
from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client
from pydantic import SecretStr

from coordinare.health import create_health_app
from coordinare.mcp.config import McpServerConfig
from coordinare.mcp.server import build_asgi_app


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class _Server:
    """uvicorn in a daemon thread for an ASGI app."""

    def __init__(self, app: Any) -> None:
        self.port = _free_port()
        self.server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="error"),
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
        return f"http://127.0.0.1:{self.port}"


def _make_config(health_url: str) -> McpServerConfig:
    return McpServerConfig(
        agent_token=SecretStr("a" * 32),
        dashboard_token=SecretStr("d" * 32),
        health_url=health_url,
    )


async def _call_get_metrics(mcp_url: str, token: str = "a" * 32) -> dict[str, Any]:
    def build_http_client() -> Any:
        return create_mcp_http_client(headers={"Authorization": f"Bearer {token}"})

    async with streamable_http_client(mcp_url + "/mcp", http_client=build_http_client()) as streams:
        read_stream, write_stream = streams
        async with ClientSession(read_stream, write_stream) as session:
            await session.initialize()
            result = await session.call_tool("get_metrics", {})
            assert result.content, "get_metrics returned no content"
            return json.loads(result.content[0].text)


def test_get_metrics_proxies_real_health_app() -> None:
    health_app = create_health_app(MagicMock())
    with _Server(health_app) as health_server, _Server(
        build_asgi_app(_make_config(health_server.url)),
    ) as mcp_server:
        payload = asyncio.run(_call_get_metrics(mcp_server.url))
        assert payload["ok"] is True
        assert payload.get("error") is None
        text = payload["data"]["text"]
        assert isinstance(text, str)
        assert text.strip()
