"""Spec 551 T033: upsert_symphony bootstrap-chaining orchestration (mocked client)."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from coordinare.mcp.client import DashboardClient, ToolResult
from coordinare.mcp.config import McpServerConfig
from coordinare.mcp.tools.symphony_tools import (
    UpsertSymphonyInput,
    upsert_symphony_handler,
)


class _RecordingClient(DashboardClient):
    """Records send_json calls; scripted responses per (method, path)."""

    def __init__(self, script: dict[tuple[str, str], ToolResult]) -> None:
        super().__init__(McpServerConfig(
            agent_token=SecretStr("a" * 32), dashboard_token=SecretStr("d" * 32),
        ), transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json={}),
        ))
        self.script = script
        self.calls: list[tuple[str, str]] = []

    async def send_json(
        self, method: str, path: str, *, json: Any = None, timeout_seconds: float = 0.0,
    ) -> ToolResult:
        self.calls.append((method, path))
        return self.script[(method, path)]


def _client() -> _RecordingClient:
    return _RecordingClient({
        ("PUT", "/api/symphonies/alpha"): ToolResult(ok=True, data={"applied": "hot_reloaded"}),
        ("POST", "/api/symphonies"): ToolResult(ok=True, data={"created": True}),
        ("POST", "/api/symphonies/alpha/env-bootstrap"): ToolResult(
            ok=True, data={"bootstrapped": "env"},
        ),
        ("POST", "/api/symphonies/alpha/wiki-init"): ToolResult(
            ok=True, data={"bootstrapped": "wiki"},
        ),
    })


def _input(**kwargs: Any) -> UpsertSymphonyInput:
    base: dict[str, Any] = {"name": "alpha", "spec": {"enabled": True}}
    return UpsertSymphonyInput.model_validate({**base, **kwargs})


@pytest.mark.asyncio
async def test_no_bootstrap_writes_only() -> None:
    client = _client()
    result = await upsert_symphony_handler(_input(), client)
    assert result.ok
    assert client.calls == [("PUT", "/api/symphonies/alpha")]


@pytest.mark.asyncio
async def test_env_bootstrap_chains_after_write_in_order() -> None:
    client = _client()
    result = await upsert_symphony_handler(_input(bootstrap="env"), client)
    assert result.ok
    assert client.calls == [
        ("PUT", "/api/symphonies/alpha"),
        ("POST", "/api/symphonies/alpha/env-bootstrap"),
    ]
    assert (result.data or {}).get("bootstrap") == {"bootstrapped": "env"}


@pytest.mark.asyncio
async def test_wiki_bootstrap_chains_after_write_in_order() -> None:
    client = _client()
    result = await upsert_symphony_handler(_input(bootstrap="wiki"), client)
    assert result.ok
    assert client.calls == [
        ("PUT", "/api/symphonies/alpha"),
        ("POST", "/api/symphonies/alpha/wiki-init"),
    ]


@pytest.mark.asyncio
async def test_write_failure_aborts_chain() -> None:
    client = _RecordingClient({
        ("PUT", "/api/symphonies/alpha"): ToolResult.failure(
            "conflict", 409, "stale", "re-read via get_config",
        ),
    })
    result = await upsert_symphony_handler(_input(bootstrap="env"), client)
    assert not result.ok
    assert client.calls == [("PUT", "/api/symphonies/alpha")]


@pytest.mark.asyncio
async def test_bootstrap_failure_surfaces_bootstrap_result() -> None:
    client = _RecordingClient({
        ("PUT", "/api/symphonies/alpha"): ToolResult(ok=True, data={}),
        ("POST", "/api/symphonies/alpha/env-bootstrap"): ToolResult.failure(
            "daemon_error", 500, "bootstrap exploded", "check the daemon log",
        ),
    })
    result = await upsert_symphony_handler(_input(bootstrap="env"), client)
    assert not result.ok
    assert result.error is not None and result.error.kind == "daemon_error"
    assert client.calls == [
        ("PUT", "/api/symphonies/alpha"),
        ("POST", "/api/symphonies/alpha/env-bootstrap"),
    ]


@pytest.mark.asyncio
async def test_put_404_falls_back_to_post_create() -> None:
    client = _RecordingClient({
        ("PUT", "/api/symphonies/alpha"): ToolResult.failure(
            "validation", 404, "not found", "",
        ),
        ("POST", "/api/symphonies"): ToolResult(ok=True, data={"created": True}),
    })
    result = await upsert_symphony_handler(_input(), client)
    assert result.ok
    assert client.calls == [("PUT", "/api/symphonies/alpha"), ("POST", "/api/symphonies")]
