"""Boot-time settings for the MCP server, loaded from the environment."""

from __future__ import annotations

import os
from typing import Literal

from pydantic import AnyHttpUrl, BaseModel, Field, SecretStr, field_validator, model_validator

from coordinare.config import ProjectConfiguration

_RESERVED_PORTS = {8080, 8090}

_MIN_AGENT_TOKEN_CHARS = 32
_MIN_AGENT_CHARS_MSG = "agent_token must be at least 32 characters"
_AGENT_CREDENTIAL_REQUIRED_MSG = "agent_token is required unless the OAuth resource server is enabled"
_STDIO_CREDENTIAL_REQUIRED_MSG = "stdio transport requires the static agent token"
_OAUTH_ENABLED_REQUIRES_ISSUER_MSG = "oauth.enabled requires an issuer URL"
_LISTEN_PORT_RESERVED = (
    "listen_port must differ from the health (8080) and dashboard (8090) ports"
)


def _expand(value: str) -> str:
    """Expand ``${VAR}`` / ``$VAR`` placeholders the way config.yaml does."""
    return os.path.expandvars(value) if "$" in value else value


class McpOAuthConfig(BaseModel):
    """OAuth 2.1 resource-server settings (553, default off).

    When ``enabled`` with an issuer, the static agent token boundary is
    replaced by RFC 7662 introspection against the authorization server.
    """

    enabled: bool = False
    issuer: AnyHttpUrl | None = None
    introspection_url: AnyHttpUrl | None = None
    client_id: str | None = None
    client_secret: SecretStr | None = None
    resource_server_url: AnyHttpUrl | None = None
    required_scopes: list[str] = Field(default_factory=list)
    timeout_seconds: float = Field(default=5.0, gt=0, lt=300)

    @model_validator(mode="after")
    def _enabled_requires_issuer(self) -> McpOAuthConfig:
        if self.enabled and self.issuer is None:
            raise ValueError(_OAUTH_ENABLED_REQUIRES_ISSUER_MSG)
        return self

    @classmethod
    def from_env(cls) -> McpOAuthConfig:
        """Build the OAuth block from ``COORDINARE_MCP_OAUTH_*`` variables."""
        env_names = {
            "enabled": "COORDINARE_MCP_OAUTH_ENABLED",
            "issuer": "COORDINARE_MCP_OAUTH_ISSUER",
            "introspection_url": "COORDINARE_MCP_OAUTH_INTROSPECTION_URL",
            "client_id": "COORDINARE_MCP_OAUTH_CLIENT_ID",
            "client_secret": "COORDINARE_MCP_OAUTH_CLIENT_SECRET",
            "resource_server_url": "COORDINARE_MCP_OAUTH_RESOURCE_SERVER_URL",
            "required_scopes": "COORDINARE_MCP_OAUTH_REQUIRED_SCOPES",
            "timeout_seconds": "COORDINARE_MCP_OAUTH_TIMEOUT",
        }
        kwargs: dict[str, object] = {}
        for field_name, env_name in env_names.items():
            raw = os.environ.get(env_name)
            if raw is None or raw == "":
                continue
            if field_name == "enabled":
                kwargs["enabled"] = raw.strip().lower() in ("1", "true", "yes")
            elif field_name == "required_scopes":
                kwargs["required_scopes"] = [part for part in raw.split(",") if part]
            elif field_name == "timeout_seconds":
                kwargs["timeout_seconds"] = float(raw)
            else:
                kwargs[field_name] = raw
        return cls.model_validate(kwargs)


class McpServerConfig(BaseModel):
    """Boot-time settings for the MCP management server (data-model.md).

    Loaded from environment variables; CLI flags override in ``__main__.py``.
    """

    transport: Literal["http", "stdio"] = "http"
    #: Same bind-all default as the daemon's own health server, so the sidecar
    #: is reachable from its peers without per-deployment overrides.
    listen_host: str = ProjectConfiguration.model_fields["health_check_host"].default
    listen_port: int = 8091
    agent_token: SecretStr | None = None
    dashboard_url: str = "http://localhost:8090"
    health_url: str = "http://localhost:8080"
    dashboard_token: SecretStr
    read_timeout_seconds: float = Field(default=10.0, gt=0, lt=300)
    lifecycle_timeout_seconds: float = Field(default=60.0, gt=0, lt=600)
    server_name: str = "coordinare"
    oauth: McpOAuthConfig = McpOAuthConfig()

    @field_validator("agent_token")
    @classmethod
    def _agent_token_min_length(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None and len(value.get_secret_value()) < _MIN_AGENT_TOKEN_CHARS:
            raise ValueError(_MIN_AGENT_CHARS_MSG)
        return value

    @model_validator(mode="after")
    def _agent_token_needed_per_mode(self) -> McpServerConfig:
        if self.agent_token is None and not self.oauth.enabled:
            raise ValueError(_AGENT_CREDENTIAL_REQUIRED_MSG)
        if self.agent_token is None and self.transport == "stdio":
            # OAuth is an HTTP concept; stdio keeps the local static-token trust.
            raise ValueError(_STDIO_CREDENTIAL_REQUIRED_MSG)
        return self

    @field_validator("dashboard_url", "health_url")
    @classmethod
    def _must_parse_as_url(cls, value: str) -> str:
        AnyHttpUrl(_expand(value))
        return value

    @model_validator(mode="after")
    def _listen_port_not_reserved(self) -> McpServerConfig:
        if self.listen_port in _RESERVED_PORTS:
            raise ValueError(_LISTEN_PORT_RESERVED)
        return self

    @classmethod
    def from_env(cls) -> McpServerConfig:
        """Build the config from ``COORDINARE_MCP_*`` environment variables."""
        env_names = {
            "transport": "COORDINARE_MCP_TRANSPORT",
            "listen_host": "COORDINARE_MCP_HOST",
            "listen_port": "COORDINARE_MCP_PORT",
            "agent_token": "COORDINARE_MCP_TOKEN",
            "dashboard_url": "COORDINARE_MCP_DASHBOARD_URL",
            "health_url": "COORDINARE_MCP_HEALTH_URL",
            "dashboard_token": "COORDINARE_MCP_DASHBOARD_TOKEN",
            "read_timeout_seconds": "COORDINARE_MCP_READ_TIMEOUT",
            "lifecycle_timeout_seconds": "COORDINARE_MCP_LIFECYCLE_TIMEOUT",
            "server_name": "COORDINARE_MCP_SERVER_NAME",
        }
        kwargs: dict[str, object] = {}
        for field_name, env_name in env_names.items():
            value = os.environ.get(env_name)
            if value is not None:
                kwargs[field_name] = _expand(value)
        kwargs["oauth"] = McpOAuthConfig.from_env()
        return cls.model_validate(kwargs)


__all__ = ["McpOAuthConfig", "McpServerConfig"]
