from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict


class ServiceRetryConfig(BaseModel):
    attempts: int = Field(default=3, ge=1, le=10)
    wait_initial_seconds: float = Field(default=2.0, ge=0.1, le=60.0)
    wait_max_seconds: float = Field(default=30.0, ge=1.0, le=300.0)
    wait_jitter_seconds: float = Field(default=1.0, ge=0.0, le=30.0)


class ServiceCircuitConfig(BaseModel):
    failure_threshold: int = Field(default=3, ge=1, le=20)
    recovery_window_seconds: float = Field(default=120.0, ge=10.0, le=3600.0)
    observation_window_seconds: float = Field(default=300.0, ge=10.0, le=3600.0)


class ResilienceConfig(BaseModel):
    github_retry: ServiceRetryConfig = Field(
        default_factory=lambda: ServiceRetryConfig(
            attempts=4, wait_initial_seconds=2.0, wait_max_seconds=30.0, wait_jitter_seconds=2.0
        )
    )
    slack_retry: ServiceRetryConfig = Field(
        default_factory=lambda: ServiceRetryConfig(
            attempts=3, wait_initial_seconds=1.0, wait_max_seconds=15.0, wait_jitter_seconds=1.0
        )
    )
    smtp_retry: ServiceRetryConfig = Field(
        default_factory=lambda: ServiceRetryConfig(
            attempts=3, wait_initial_seconds=2.0, wait_max_seconds=20.0, wait_jitter_seconds=1.5
        )
    )
    anthropic_retry: ServiceRetryConfig = Field(
        default_factory=lambda: ServiceRetryConfig(
            attempts=4, wait_initial_seconds=5.0, wait_max_seconds=30.0, wait_jitter_seconds=2.0
        )
    )
    agent_retry: ServiceRetryConfig = Field(
        default_factory=lambda: ServiceRetryConfig(
            attempts=3, wait_initial_seconds=2.0, wait_max_seconds=20.0, wait_jitter_seconds=1.0
        )
    )
    github_circuit: ServiceCircuitConfig = Field(
        default_factory=lambda: ServiceCircuitConfig(
            failure_threshold=3, recovery_window_seconds=120.0, observation_window_seconds=300.0
        )
    )
    slack_circuit: ServiceCircuitConfig = Field(
        default_factory=lambda: ServiceCircuitConfig(
            failure_threshold=5, recovery_window_seconds=120.0, observation_window_seconds=300.0
        )
    )
    smtp_circuit: ServiceCircuitConfig = Field(
        default_factory=lambda: ServiceCircuitConfig(
            failure_threshold=5, recovery_window_seconds=300.0, observation_window_seconds=600.0
        )
    )
    anthropic_circuit: ServiceCircuitConfig = Field(
        default_factory=lambda: ServiceCircuitConfig(
            failure_threshold=3, recovery_window_seconds=120.0, observation_window_seconds=300.0
        )
    )
    agent_circuit: ServiceCircuitConfig = Field(
        default_factory=lambda: ServiceCircuitConfig(
            failure_threshold=3, recovery_window_seconds=60.0, observation_window_seconds=180.0
        )
    )


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
    resilience: ResilienceConfig = Field(default_factory=ResilienceConfig)

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

    @model_validator(mode="after")
    def _validate_retry_backoff_caps(self) -> ProjectConfiguration:
        poll = float(self.poll_interval_seconds)
        retry_fields = (
            "github_retry", "slack_retry", "smtp_retry",
            "anthropic_retry", "agent_retry",
        )
        for name in retry_fields:
            cfg = getattr(self.resilience, name)
            if cfg.wait_max_seconds > poll:
                msg = (
                    f"resilience.{name}.wait_max_seconds ({cfg.wait_max_seconds}) "
                    f"exceeds poll_interval_seconds ({poll})"
                )
                raise ValueError(msg)
        return self

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

