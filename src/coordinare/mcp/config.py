"""Boot-time settings for the MCP server, loaded from the environment."""

from __future__ import annotations

import os
from typing import Literal

from pydantic import AnyHttpUrl, BaseModel, Field, SecretStr, field_validator, model_validator

_RESERVED_PORTS = {8080, 8090}


def _expand(value: str) -> str:
    """Expand ``${VAR}`` / ``$VAR`` placeholders the way config.yaml does."""
    return os.path.expandvars(value) if "$" in value else value


class McpServerConfig(BaseModel):
    """Boot-time settings for the MCP management server (data-model.md).

    Loaded from environment variables; CLI flags override in ``__main__.py``.
    """

    transport: Literal["http"] = "http"
    listen_host: str = "0.0.0.0"
    listen_port: int = 8091
    agent_token: SecretStr
    dashboard_url: str = "http://localhost:8090"
    health_url: str = "http://localhost:8080"
    dashboard_token: SecretStr
    read_timeout_seconds: float = Field(default=10.0, gt=0, lt=300)
    lifecycle_timeout_seconds: float = Field(default=60.0, gt=0, lt=600)
    server_name: str = "coordinare"

    @field_validator("agent_token")
    @classmethod
    def _agent_token_min_length(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) < 32:
            raise ValueError("agent_token must be at least 32 characters")
        return value

    @field_validator("dashboard_url", "health_url")
    @classmethod
    def _must_parse_as_url(cls, value: str) -> str:
        AnyHttpUrl(_expand(value))
        return value

    @model_validator(mode="after")
    def _listen_port_not_reserved(self) -> McpServerConfig:
        if self.listen_port in _RESERVED_PORTS:
            raise ValueError(
                "listen_port must differ from the health (8080) and dashboard (8090) ports",
            )
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
        kwargs: dict[str, str] = {}
        for field_name, env_name in env_names.items():
            value = os.environ.get(env_name)
            if value is not None:
                kwargs[field_name] = _expand(value)
        return cls(**kwargs)  # type: ignore[arg-type]


__all__ = ["McpServerConfig"]
