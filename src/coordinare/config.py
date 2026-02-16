from __future__ import annotations

from pathlib import Path
from typing import Any

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

    agent_host: str
    agent_port: int = 22
    agent_user: str
    agent_key_path: Path = Path("~/.ssh/id_ed25519")
    agent_command: str

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
        if not value.get_secret_value().strip():
            msg = "github_token must be non-empty"
            raise ValueError(msg)
        return value

    @field_validator("agent_command")
    @classmethod
    def _validate_agent_command(cls, value: str) -> str:
        if "{card_context}" not in value:
            msg = "agent_command must contain the {card_context} placeholder"
            raise ValueError(msg)
        return value
