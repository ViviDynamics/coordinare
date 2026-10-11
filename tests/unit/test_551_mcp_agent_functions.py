"""Spec 551 — the agent-facing module functions and client edge branches.

The tool registry calls the module-level functions (``get_config``,
``upsert_symphony``, ...), which resolve the shared client from
:meth:`coordinare.mcp.client.set_client` and delegate to the handlers the
contract tests exercise directly. These tests pin that last hop — plus the
client's own edge branches the registry test never reaches.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from coordinare.mcp import tools as mcp_tools
from coordinare.mcp.client import (
    DashboardClient,
    ToolResult,
    get_client,
    set_client,
)
from coordinare.mcp.config import McpServerConfig
from coordinare.mcp.tools import config_tools, persona_tools, state_tools
from coordinare.mcp.tools.symphony_tools import (
    delete_symphony,
    get_symphony,
    list_symphonies,
    upsert_symphony,
    validate_symphony,
)


def _config() -> McpServerConfig:
    return McpServerConfig(
        agent_token=SecretStr("a" * 32), dashboard_token=SecretStr("d" * 32),
    )


class _SpyClient(DashboardClient):
    def __init__(self) -> None:
        super().__init__(_config(), transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json={}),
        ))
        self.calls: list[tuple[str, str]] = []

    async def get_json(self, path: str, timeout_seconds: float = 0.0) -> ToolResult:
        self.calls.append(("GET", path))
        return ToolResult(ok=True, data={"path": path})

    async def send_json(
        self, method: str, path: str, *, json: Any = None, timeout_seconds: float = 0.0,
    ) -> ToolResult:
        self.calls.append((method, path))
        return ToolResult(ok=True, data={"path": path})

    async def get_stream_head(self, path: str, timeout_seconds: float = 0.0) -> ToolResult:
        self.calls.append(("STREAM", path))
        return ToolResult(ok=True, data={"text": "head"})

    async def get_text(self, path: str, timeout_seconds: float = 0.0) -> ToolResult:
        self.calls.append(("TEXT", path))
        return ToolResult(ok=True, data={"text": "# metrics"})


@pytest.fixture
def spy() -> _SpyClient:
    client = _SpyClient()
    set_client(client)
    return client


def test_get_client_without_boot_raises() -> None:
    import coordinare.mcp.client as client_module

    previous, client_module._client = client_module._client, None
    try:
        with pytest.raises(RuntimeError, match="not configured"):
            get_client()
    finally:
        client_module._client = previous


# --- config -----------------------------------------------------------------


async def test_get_config_agent_function(spy: _SpyClient) -> None:
    assert (await config_tools.get_config("all"))["data"]["path"] == "/api/config/all"
    assert spy.calls == [("GET", "/api/config/all")]


async def test_get_config_scope_requiring_id_without_id(spy: _SpyClient) -> None:
    result = await config_tools.get_config("section")
    assert not result["ok"] and result["error"]["kind"] == "validation"
    assert spy.calls == []


async def test_update_config_missing_path_placeholder(spy: _SpyClient) -> None:
    result = await config_tools.update_config(
        "put_section", base_hash="h", payload={"k": "v"},
    )
    assert not result["ok"] and result["error"]["kind"] == "validation"
    assert spy.calls == []


async def test_update_config_agent_function(spy: _SpyClient) -> None:
    result = await config_tools.update_config(
        "put_section", id="roles", base_hash="h", payload={"k": "v"},
    )
    assert result["ok"]
    assert spy.calls == [("PUT", "/api/config/section/roles")]


async def test_validate_config_agent_function(spy: _SpyClient) -> None:
    result = await config_tools.validate_config({"github_token": "x"})
    assert result["ok"]
    assert spy.calls == [("POST", "/api/config/validate")]


# --- symphonies --------------------------------------------------------------


async def test_list_symphonies_agent_function(spy: _SpyClient) -> None:
    assert (await list_symphonies())["data"]["path"] == "/api/symphonies"


async def test_get_symphony_agent_function(spy: _SpyClient) -> None:
    assert (await get_symphony("alpha"))["data"]["path"] == "/api/symphonies/alpha"


async def test_upsert_symphony_agent_function(spy: _SpyClient) -> None:
    result = await upsert_symphony("alpha", {"enabled": True})
    assert result["ok"]
    assert spy.calls == [("PUT", "/api/symphonies/alpha")]


async def test_validate_symphony_agent_function(spy: _SpyClient) -> None:
    result = await validate_symphony("alpha", {"enabled": True})
    assert result["ok"]
    assert spy.calls == [("POST", "/api/symphonies/alpha/validate")]


async def test_delete_symphony_agent_function(spy: _SpyClient) -> None:
    result = await delete_symphony("alpha")
    assert result["ok"]
    assert spy.calls == [("DELETE", "/api/symphonies/alpha")]


# --- personas & state --------------------------------------------------------


async def test_persona_agent_functions(spy: _SpyClient) -> None:
    assert (await persona_tools.get_personas())["ok"]
    assert (await persona_tools.upsert_persona("implementer", "do it"))["ok"]
    assert (await persona_tools.delete_persona("implementer"))["ok"]
    assert [m for m, _ in spy.calls] == ["GET", "PUT", "DELETE"]


async def test_state_agent_functions(spy: _SpyClient) -> None:
    assert (await state_tools.get_state())["ok"]
    assert (await state_tools.get_performer_logs())["data"]["text"] == "head"
    assert (await state_tools.get_activity(card_id="card-1"))["ok"]
    assert (await state_tools.get_metrics())["ok"]
    assert [m for m, _ in spy.calls] == ["GET", "STREAM", "GET", "TEXT"]


# --- registry helpers ---------------------------------------------------------


def test_find_tool_returns_none_for_unknown_name() -> None:
    assert mcp_tools.find_tool("no_such_tool") is None


@pytest.mark.parametrize(
    "path",
    ["/", "/performers", "/static/x.js", "/events", "/oidc/login", "/health"],
)
def test_is_deferred_path_covers_pages_and_static(path: str) -> None:
    assert mcp_tools.is_deferred(path)


# --- client edge branches ------------------------------------------------------


def _client_with(handler) -> DashboardClient:
    return DashboardClient(_config(), transport=httpx.MockTransport(handler))


async def test_stream_head_maps_non_200() -> None:
    client = _client_with(lambda _request: httpx.Response(500, text="boom"))
    result = await client.get_stream_head("/api/performer-logs", timeout_seconds=1.0)
    assert not result.ok
    assert result.error is not None and result.error.kind == "daemon_error"


async def test_stream_head_byte_cap_breaks_the_read() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"x" * 4096, headers={"content-type": "text/plain"})

    client = _client_with(handler)
    result = await client.get_stream_head("/api/performer-logs", timeout_seconds=1.0)
    assert result.ok
    assert len((result.data or {})["text"]) == 4096


async def test_stream_head_connect_error_is_unreachable() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route")

    client = _client_with(handler)
    result = await client.get_stream_head("/api/performer-logs", timeout_seconds=1.0)
    assert not result.ok
    assert result.error is not None and result.error.kind == "daemon_unreachable"


async def test_non_json_error_body_becomes_text() -> None:
    client = _client_with(lambda _request: httpx.Response(502, text="bad gateway"))
    result = await client.get_json("/api/state", timeout_seconds=1.0)
    assert not result.ok
    assert result.error is not None and result.error.kind == "daemon_error"
