"""Spec 551: McpServerConfig boot settings for the MCP management layer."""

from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from coordinare.mcp.config import McpServerConfig


def test_defaults_match_data_model() -> None:
    config = McpServerConfig(
        agent_token=SecretStr("x" * 32),
        dashboard_token=SecretStr("y" * 32),
    )
    assert config.transport == "http"
    assert config.listen_host == "0.0.0.0"
    assert config.listen_port == 8091
    assert config.dashboard_url == "http://localhost:8090"
    assert config.health_url == "http://localhost:8080"
    assert config.read_timeout_seconds == 10.0
    assert config.lifecycle_timeout_seconds == 60.0
    assert config.server_name == "coordinare"


def test_tokens_are_secret() -> None:
    config = McpServerConfig(
        agent_token=SecretStr("x" * 32),
        dashboard_token=SecretStr("y" * 32),
    )
    assert "x" * 32 not in repr(config)
    assert "y" * 32 not in str(config.agent_token)


def test_agent_token_minimum_length_enforced() -> None:
    with pytest.raises(ValidationError):
        McpServerConfig(
            agent_token=SecretStr("short"),
            dashboard_token=SecretStr("y" * 32),
        )


def test_listen_port_must_differ_from_dashboard_and_health() -> None:
    with pytest.raises(ValidationError):
        McpServerConfig(
            agent_token=SecretStr("x" * 32),
            dashboard_token=SecretStr("y" * 32),
            listen_port=8090,
        )
    with pytest.raises(ValidationError):
        McpServerConfig(
            agent_token=SecretStr("x" * 32),
            dashboard_token=SecretStr("y" * 32),
            listen_port=8080,
        )


def test_timeout_bounds_enforced() -> None:
    with pytest.raises(ValidationError):
        McpServerConfig(
            agent_token=SecretStr("x" * 32),
            dashboard_token=SecretStr("y" * 32),
            read_timeout_seconds=0,
        )
    with pytest.raises(ValidationError):
        McpServerConfig(
            agent_token=SecretStr("x" * 32),
            dashboard_token=SecretStr("y" * 32),
            lifecycle_timeout_seconds=600,
        )


def test_transport_rejects_stdio_in_phase_1() -> None:
    with pytest.raises(ValidationError):
        McpServerConfig(
            transport="stdio",  # type: ignore[arg-type]
            agent_token=SecretStr("x" * 32),
            dashboard_token=SecretStr("y" * 32),
        )


def test_dashboard_url_must_parse() -> None:
    config = McpServerConfig(
        agent_token=SecretStr("x" * 32),
        dashboard_token=SecretStr("y" * 32),
        dashboard_url="https://dashboard.example.com",
    )
    assert config.dashboard_url == "https://dashboard.example.com"
    with pytest.raises(ValidationError):
        McpServerConfig(
            agent_token=SecretStr("x" * 32),
            dashboard_token=SecretStr("y" * 32),
            dashboard_url="not a url",
        )


def test_from_env_reads_and_expands(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COORDINARE_MCP_TOKEN", "a" * 32)
    monkeypatch.setenv("COORDINARE_MCP_DASHBOARD_TOKEN", "b" * 32)
    monkeypatch.setenv("COORDINARE_MCP_DASHBOARD_URL", "${DASH_URL}")
    monkeypatch.setenv("DASH_URL", "http://dash:8090")
    config = McpServerConfig.from_env()
    assert config.agent_token.get_secret_value() == "a" * 32
    assert config.dashboard_token.get_secret_value() == "b" * 32
    assert config.dashboard_url == "http://dash:8090"


def test_from_env_defaults_when_optional_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COORDINARE_MCP_TOKEN", "a" * 32)
    monkeypatch.setenv("COORDINARE_MCP_DASHBOARD_TOKEN", "b" * 32)
    monkeypatch.delenv("COORDINARE_MCP_DASHBOARD_URL", raising=False)
    config = McpServerConfig.from_env()
    assert config.dashboard_url == "http://localhost:8090"
    assert config.listen_port == 8091
