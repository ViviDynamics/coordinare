"""Spec 552: the stdio transport session (FR2, FR3, FR5)."""

from __future__ import annotations

import json
import os
import sys
from typing import Any

import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.server.mcpserver import MCPServer
from pydantic import SecretStr

import coordinare.mcp.server as mcp_server
from coordinare.mcp.auth import credential_fingerprint
from coordinare.mcp.config import McpServerConfig
from coordinare.mcp.server import run_stdio
from coordinare.mcp.tools import REGISTRY


def _stdio_config(**overrides: object) -> McpServerConfig:
    kwargs: dict[str, object] = {
        "transport": "stdio",
        "agent_token": SecretStr("a" * 32),
        "dashboard_token": SecretStr("d" * 32),
    }
    kwargs.update(overrides)
    return McpServerConfig.model_validate(kwargs)


def test_run_stdio_builds_the_bare_server_and_sets_the_fingerprint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """run_stdio serves the shared registry with no bearer gate (FR2, FR3)."""
    seen: dict[str, Any] = {}

    async def fake_run_stdio_async(self: MCPServer) -> None:
        seen["server"] = self

    monkeypatch.setattr(MCPServer, "run_stdio_async", fake_run_stdio_async)
    run_stdio(_stdio_config())
    assert isinstance(seen["server"], MCPServer)
    assert (
        mcp_server._caller_fingerprint.get() == credential_fingerprint("a" * 32)
    )


@pytest.mark.asyncio
async def test_stdio_transport_smoke_lists_the_registry_and_maps_errors() -> None:
    """The real process speaks MCP over stdio: registry served, errors as data."""
    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "coordinare.mcp", "--transport", "stdio"],
        env={
            "PATH": os.environ["PATH"],
            "HOME": os.environ.get("HOME", "/"),
            "COORDINARE_MCP_TOKEN": "a" * 32,
            "COORDINARE_MCP_DASHBOARD_TOKEN": "d" * 32,
            "COORDINARE_MCP_DASHBOARD_URL": "http://localhost:8090",
        },
    )
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        tools = await session.list_tools()
        assert len(tools.tools) == len(REGISTRY) == 16
        result = await session.call_tool("get_config", {"scope": "all"})
        payload = json.loads(result.content[0].text)
        assert payload["ok"] is False
        assert payload["error"]["kind"] == "daemon_unreachable"
