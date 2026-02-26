from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict


class ProjectConfiguration(BaseSettings):
    """Application configuration loaded from YAML and COORDINARE_* env vars."""

    model_config = SettingsConfigDict(env_prefix="COORDINARE_", extra="ignore")

    project_name: str
    github_org: str
    github_project_number: int
    github_token: SecretStr

    agent_transport: Literal["subprocess", "ssh", "kubernetes"] = "subprocess"
    agent_executable: str = ""
    transport_timeout_seconds: int = Field(default=30, ge=1, le=300)

    agent_host: str = ""
    agent_port: int = 22
    agent_user: str = ""
    agent_key_path: Path = Path("~/.ssh/id_ed25519")
    agent_command: str | None = None

    human_reviewers: list[str]

    notification_email: str = "coordinare@vividynamics.com"
    smtp_host: str
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None

    slack_webhook_url: SecretStr
    slack_channel: str

    poll_interval_seconds: int = Field(default=30, ge=10, le=300)
    blocked_reminder_hours: int = 24
    health_check_port: int = 8080
    output_mode: str = Field(default="human", pattern="^(human|structured)$")
    log_level: str = Field(default="info", pattern="^(debug|info|warning|error)$")
    heartbeat_interval_seconds: int = Field(default=30, ge=5, le=300)
    max_cycles: int | None = Field(default=None, ge=1)
    state_file_path: Path = Field(default=Path("./coordinare.state.json"))

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        # Explicitly give environment variables higher priority than YAML/init values.
        return env_settings, init_settings, dotenv_settings, file_secret_settings

    @classmethod
    def from_yaml(cls, path: Path | str) -> ProjectConfiguration:
        config_path = Path(path)
        raw: dict[str, Any] = {}
        if config_path.exists():
            loaded = yaml.safe_load(config_path.read_text())
            if isinstance(loaded, dict):
                raw = loaded
        return cls(**raw)

    @field_validator("human_reviewers")
    @classmethod
    def _validate_human_reviewers(cls, value: list[str]) -> list[str]:
        if not value:
            msg = "human_reviewers must contain at least one entry"
            raise ValueError(msg)
        return value

    @field_validator("github_token")
    @classmethod
    def _validate_github_token(cls, value: SecretStr) -> SecretStr:
        token = value.get_secret_value().strip()
        if not token:
            msg = "github_token must be non-empty"
            raise ValueError(msg)
        if token.startswith("${") and token.endswith("}"):
            msg = (
                "github_token must be a real token value; unresolved placeholder detected. "
                "Set COORDINARE_GITHUB_TOKEN or update config.yaml."
            )
            raise ValueError(msg)
        return value

    @field_validator("slack_webhook_url")
    @classmethod
    def _validate_slack_webhook_url(cls, value: SecretStr) -> SecretStr:
        webhook = value.get_secret_value().strip()
        if not webhook:
            msg = "slack_webhook_url must be non-empty"
            raise ValueError(msg)
        if webhook.startswith("${") and webhook.endswith("}"):
            msg = (
                "slack_webhook_url must be a real webhook value; unresolved placeholder detected. "
                "Set COORDINARE_SLACK_WEBHOOK_URL or update config.yaml."
            )
            raise ValueError(msg)
        return value

