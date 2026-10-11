"""Spec 551 T027: lifecycle `control` contract — mapping, budgets, validation."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from coordinare.mcp.client import DashboardClient, ToolResult
from coordinare.mcp.config import McpServerConfig
from coordinare.mcp.tools.lifecycle_tools import ControlInput, control_handler

#: contracts/mcp-tools.md — every action's exact verb/path.
EXPECTED: dict[str, tuple[str, str]] = {
    "force_poll": ("POST", "/api/force-poll"),
    "cancel": ("POST", "/api/cancel"),
    "skip_role": ("POST", "/api/skip-role"),
    "restart_from": ("POST", "/api/restart-from/implementer"),
    "veto": ("POST", "/api/veto"),
    "dry_run": ("POST", "/api/dry-run/card-123"),
}


def make_client(handler) -> DashboardClient:
    config = McpServerConfig(
        agent_token=SecretStr("a" * 32), dashboard_token=SecretStr("d" * 32),
    )
    return DashboardClient(config, transport=httpx.MockTransport(handler))


class _SpyClient(DashboardClient):
    """Records which timeout budget each send used (T029)."""

    def __init__(self, config: McpServerConfig) -> None:
        super().__init__(config, transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, json={}),
        ))
        self.sent: list[tuple[str, str, float]] = []

    async def send_json(
        self, method: str, path: str, *, json: Any = None, timeout_seconds: float = 0.0,
    ) -> ToolResult:
        self.sent.append((method, path, timeout_seconds))
        return ToolResult(ok=True, data={})


def _spy() -> _SpyClient:
    return _SpyClient(McpServerConfig(
        agent_token=SecretStr("a" * 32), dashboard_token=SecretStr("d" * 32),
    ))


@pytest.mark.asyncio
@pytest.mark.parametrize("action", list(EXPECTED))
async def test_action_rest_mapping(action: str) -> None:
    method, path = EXPECTED[action]
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        return httpx.Response(200, json={"ok": True})

    client = make_client(handler)
    result = await control_handler(ControlInput.model_validate({
        "action": action, "card_id": "card-123", "role": "implementer",
    }), client)
    assert result.ok, result.error
    assert seen == [(method, path)], action


@pytest.mark.asyncio
async def test_control_uses_lifecycle_budget() -> None:
    client = _spy()
    await control_handler(
        ControlInput(action="force_poll"), client,
    )
    method, path, timeout = client.sent[0]
    assert (method, path) == ("POST", "/api/force-poll")
    assert timeout == client.lifecycle_budget
    assert timeout > client.read_budget or client.read_budget == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["skip_role", "veto", "dry_run"])
async def test_card_id_required_before_any_http(action: str) -> None:
    client = _spy()
    result = await control_handler(
        ControlInput(action=action), client,
    )
    assert not result.ok
    assert result.error is not None and result.error.kind == "validation"
    assert "card_id" in result.error.detail
    assert client.sent == []


@pytest.mark.asyncio
async def test_cancel_and_force_poll_need_no_card_id() -> None:
    """`/api/cancel` cancels the active card; neither route takes parameters."""
    client = _spy()
    for action in ("force_poll", "cancel"):
        result = await control_handler(ControlInput(action=action), client)
        assert result.ok, (action, result.error)


@pytest.mark.asyncio
async def test_restart_from_requires_role() -> None:
    client = _spy()
    result = await control_handler(
        ControlInput(action="restart_from", card_id="card-123"), client,
    )
    assert not result.ok
    assert result.error is not None and result.error.kind == "validation"
    assert "role" in result.error.detail
    assert client.sent == []
