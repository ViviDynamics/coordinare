"""Spec 553: OAuth resource-server configuration (FR1), default off."""

from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from coordinare.mcp.config import McpOAuthConfig, McpServerConfig


def _static_kwargs() -> dict[str, object]:
    return {
        "agent_token": SecretStr("x" * 32),
        "dashboard_token": SecretStr("y" * 32),
    }


def test_static_mode_is_the_default() -> None:
    config = McpServerConfig.model_validate(_static_kwargs())
    assert config.oauth.enabled is False
    assert config.oauth.required_scopes == []


def test_full_oauth_block_round_trips() -> None:
    config = McpServerConfig.model_validate({
        **_static_kwargs(),
        "oauth": {
            "enabled": True,
            "issuer": "https://as.example.com",
            "introspection_url": "https://as.example.com/introspect",
            "client_id": "coordinare-mcp",
            "client_secret": "s" * 32,
            "resource_server_url": "https://mcp.example.com/mcp",
            "required_scopes": ["coordinare:mcp"],
            "timeout_seconds": 3.0,
        },
    })
    assert config.oauth.enabled is True
    assert str(config.oauth.issuer) == "https://as.example.com/"
    assert str(config.oauth.introspection_url) == "https://as.example.com/introspect"
    assert config.oauth.client_id == "coordinare-mcp"
    assert config.oauth.client_secret is not None
    assert config.oauth.required_scopes == ["coordinare:mcp"]
    assert config.oauth.timeout_seconds == 3.0


def test_enabled_without_issuer_fails() -> None:
    with pytest.raises(ValidationError):
        McpOAuthConfig.model_validate({"enabled": True})


def test_static_token_optional_when_oauth_enabled() -> None:
    config = McpServerConfig.model_validate({
        "dashboard_token": SecretStr("y" * 32),
        "oauth": {"enabled": True, "issuer": "https://as.example.com"},
    })
    assert config.agent_token is None


def test_static_token_required_in_static_mode() -> None:
    with pytest.raises(ValidationError):
        McpServerConfig.model_validate({
            "dashboard_token": SecretStr("y" * 32),
            "oauth": {"enabled": False},
        })


def test_from_env_round_trips_oauth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COORDINARE_MCP_TOKEN", "x" * 32)
    monkeypatch.setenv("COORDINARE_MCP_DASHBOARD_TOKEN", "y" * 32)
    monkeypatch.setenv("COORDINARE_MCP_OAUTH_ENABLED", "true")
    monkeypatch.setenv("COORDINARE_MCP_OAUTH_ISSUER", "https://as.example.com")
    monkeypatch.setenv("COORDINARE_MCP_OAUTH_CLIENT_ID", "coordinare-mcp")
    monkeypatch.setenv("COORDINARE_MCP_OAUTH_CLIENT_SECRET", "s" * 32)
    monkeypatch.setenv("COORDINARE_MCP_OAUTH_REQUIRED_SCOPES", "coordinare:mcp,coordinare:write")
    monkeypatch.setenv("COORDINARE_MCP_OAUTH_TIMEOUT", "3")
    config = McpServerConfig.from_env()
    assert config.oauth.enabled is True
    assert str(config.oauth.issuer) == "https://as.example.com/"
    assert config.oauth.client_id == "coordinare-mcp"
    assert config.oauth.client_secret is not None
    assert config.oauth.required_scopes == ["coordinare:mcp", "coordinare:write"]
    assert config.oauth.timeout_seconds == 3.0


def test_from_env_without_oauth_vars_is_static(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("COORDINARE_MCP_TOKEN", "x" * 32)
    monkeypatch.setenv("COORDINARE_MCP_DASHBOARD_TOKEN", "y" * 32)
    config = McpServerConfig.from_env()
    assert config.oauth.enabled is False
    assert config.oauth.issuer is None
