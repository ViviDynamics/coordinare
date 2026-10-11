"""Spec 551 T018/T021: config tool contract against the real dashboard app.

Read scopes hit the real ``create_dashboard_app`` (temp ``config.yaml``); the
``update_config`` action→REST table is pinned with a recording MockTransport; a
stale ``base_hash`` must surface as ``conflict`` naming ``get_config``; and
``validate_config`` must not touch the on-disk config.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
import yaml
from pydantic import SecretStr

from coordinare.config import CoordinareConfiguration
from coordinare.config_validation import coerce_multi_symphony_raw
from coordinare.dashboard import DashboardStore, create_dashboard_app
from coordinare.mcp.client import DashboardClient
from coordinare.mcp.config import McpServerConfig
from coordinare.mcp.tools.config_tools import (
    GetConfigInput,
    UpdateConfigInput,
    ValidateConfigInput,
    get_config_handler,
    update_config_handler,
    validate_config_handler,
)

TOKEN = "d" * 32


def make_client(handler) -> DashboardClient:
    config = McpServerConfig(
        agent_token=SecretStr("a" * 32), dashboard_token=SecretStr(TOKEN),
    )
    return DashboardClient(config, transport=httpx.MockTransport(handler))


@pytest.fixture
def app_client(temp_config_path: Path) -> DashboardClient:
    raw = yaml.safe_load(temp_config_path.read_text())
    validated_raw = {**raw, "github_token": "ghp_fixturetoken"}
    cfg = CoordinareConfiguration(**coerce_multi_symphony_raw(validated_raw))
    daemon = MagicMock()
    daemon.state = {"coordinare_config": cfg, "config_version": 7}
    daemon.running = True
    app = create_dashboard_app(
        DashboardStore(), daemon, MagicMock(), MagicMock(), config_path=temp_config_path,
    )
    config = McpServerConfig(
        agent_token=SecretStr("a" * 32), dashboard_token=SecretStr(TOKEN),
    )
    return DashboardClient(config, transport=httpx.ASGITransport(app=app))


def _update_input(action: str, **kwargs: Any) -> UpdateConfigInput:
    kwargs.setdefault("id", "global")
    kwargs.setdefault("item_id", "openai")
    kwargs.setdefault("index", 0)
    return UpdateConfigInput.model_validate({"action": action, **kwargs})


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "scope", ["all", "effective", "global", "routing"],
)
async def test_get_config_read_scopes_hit_real_app(
    app_client: DashboardClient, scope: str,
) -> None:
    result = await get_config_handler(GetConfigInput(scope=scope), app_client)
    assert result.ok, result.error
    assert result.data is not None


@pytest.mark.asyncio
async def test_get_config_all_carry_hashes(app_client: DashboardClient) -> None:
    result = await get_config_handler(GetConfigInput(scope="all"), app_client)
    assert result.ok and "content_hashes" in (result.data or {})


@pytest.mark.asyncio
async def test_update_config_rest_mapping_table() -> None:
    """Every action maps to the exact verb/path from contracts/mcp-tools.md."""
    expected: dict[str, tuple[str, str]] = {
        "put_global": ("PUT", "/api/config/global"),
        "put_section": ("PUT", "/api/config/section/global"),
        "catalog_create": ("POST", "/api/config/catalog/global"),
        "catalog_update": ("PUT", "/api/config/catalog/global/openai"),
        "catalog_delete": ("DELETE", "/api/config/catalog/global/openai"),
        "routing_create": ("POST", "/api/config/routing/entry"),
        "routing_update": ("PUT", "/api/config/routing/entry/0"),
        "routing_delete": ("DELETE", "/api/config/routing/entry/0"),
        "reload": ("POST", "/api/config/reload"),
    }
    seen: set[tuple[str, str]] = set()

    def handler(request: httpx.Request) -> httpx.Response:
        seen.add((request.method, request.url.path))
        return httpx.Response(200, json={"ok": True})

    client = make_client(handler)
    for action in expected:
        result = await update_config_handler(
            _update_input(action, base_hash="h", payload={"k": "v"}), client,
        )
        assert result.ok, f"{action}: {result.error}"
    assert seen == set(expected.values()), seen


@pytest.mark.asyncio
async def test_stale_base_hash_is_conflict_naming_get_config(
    app_client: DashboardClient,
) -> None:
    result = await update_config_handler(
        _update_input("put_global", base_hash="stale", payload={"k": "v"}),
        app_client,
    )
    assert not result.ok
    assert result.error is not None and result.error.kind == "conflict"
    assert "get_config" in result.error.recovery_hint


@pytest.mark.asyncio
async def test_validate_config_writes_nothing(
    app_client: DashboardClient, temp_config_path: Path,
) -> None:
    before = temp_config_path.read_bytes()
    full_config = yaml.safe_load(temp_config_path.read_text())
    # A validator rejects unresolved ${VAR} pat placeholders (expansion happens
    # only at load), so hand the preflight a resolvable token.
    full_config["github_token"] = "ghp_fixturetoken"
    result = await validate_config_handler(
        ValidateConfigInput(payload=full_config), app_client,
    )
    assert result.ok and (result.data or {}).get("passed") is True
    assert temp_config_path.read_bytes() == before
