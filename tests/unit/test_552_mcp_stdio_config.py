"""Spec 552: stdio transport configuration (FR1)."""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from coordinare.mcp.config import McpServerConfig


def _config(**overrides: object) -> McpServerConfig:
    kwargs: dict[str, object] = {
        "agent_token": SecretStr("x" * 32),
        "dashboard_token": SecretStr("y" * 32),
    }
    kwargs.update(overrides)
    return McpServerConfig.model_validate(kwargs)


def test_stdio_transport_accepted() -> None:
    config = _config(transport="stdio")
    assert config.transport == "stdio"
    assert config.listen_port == 8091


def test_stdio_transport_round_trips_from_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("COORDINARE_MCP_TRANSPORT", "stdio")
    monkeypatch.setenv("COORDINARE_MCP_TOKEN", "x" * 40)
    monkeypatch.setenv("COORDINARE_MCP_DASHBOARD_TOKEN", "y" * 40)
    config = McpServerConfig.from_env()
    assert config.transport == "stdio"
