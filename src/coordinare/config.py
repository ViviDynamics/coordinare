from __future__ import annotations

import string
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

from coordinare.models.notification import ChannelType, EventType

# ---------------------------------------------------------------------------
# 006 — Notification & Alerting config models
# ---------------------------------------------------------------------------


class ChannelConfig(BaseModel):
    name: str
    type: ChannelType

    # Slack-specific
    webhook_url: SecretStr | None = None

    # Email-specific
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
    smtp_sender: str = "coordinare@vividynamics.com"
    smtp_recipient: str | None = None

    # Rate limiting
    rate_limit: int = Field(default=0, ge=0)
    rate_window_seconds: int = Field(default=60, ge=1)

    # Deduplication
    dedup_window_seconds: int = Field(default=600, ge=0)

    # Retry
    retry_count: int = Field(default=5, ge=1)
    retry_delay_seconds: float = Field(default=2.0, ge=0.0)

    # Message template
    message_template: str = "{event_type}: {source}"
    subject_template: str | None = None

    @field_validator("message_template", "subject_template", mode="before")
    @classmethod
    def _validate_template_syntax(cls, v: str | None) -> str | None:
        if v is None:
            return v
        try:
            list(string.Formatter().parse(v))
        except (ValueError, KeyError) as exc:
            msg = f"Invalid template syntax: {exc}"
            raise ValueError(msg) from exc
        return v

    @model_validator(mode="after")
    def _validate_type_fields(self) -> ChannelConfig:
        if self.type == ChannelType.slack and not self.webhook_url:
            msg = "webhook_url required for slack channels"
            raise ValueError(msg)
        if self.type == ChannelType.email and (not self.smtp_host or not self.smtp_recipient):
            msg = "smtp_host and smtp_recipient required for email channels"
            raise ValueError(msg)
        return self


class RoutingEntry(BaseModel):
    event_type: EventType
    channels: list[str]
    enabled: bool = True


class NotificationsConfig(BaseModel):
    channels: list[ChannelConfig] = Field(default_factory=list)
    routing: list[RoutingEntry] = Field(default_factory=list)
    history_max_age_hours: int = Field(default=24, ge=1)
    prolonged_idle_threshold_seconds: int = Field(default=1800, ge=60)

    @model_validator(mode="after")
    def _validate_unique_channel_names(self) -> NotificationsConfig:
        names = [c.name for c in self.channels]
        if len(names) != len(set(names)):
            msg = "Channel names must be unique"
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _validate_routing_references(self) -> NotificationsConfig:
        channel_names = {c.name for c in self.channels}
        for entry in self.routing:
            for ch in entry.channels:
                if ch not in channel_names:
                    msg = f"Routing entry references unknown channel '{ch}'"
                    raise ValueError(msg)
        return self


# ---------------------------------------------------------------------------
# 007 — Customer advocate config model
# ---------------------------------------------------------------------------

_DEFAULT_SENSITIVE_KEYWORDS = [
    "billing", "payment", "legal", "security", "breach",
    "abuse", "harassment", "lawsuit", "GDPR", "refund",
]


class AdvocateConfig(BaseModel):
    enabled: bool = False
    confidence_threshold: float = Field(default=0.70, gt=0.0, le=1.0)
    sensitive_keywords: list[str] = Field(default_factory=lambda: list(_DEFAULT_SENSITIVE_KEYWORDS))
    doc_sources: list[str] = Field(default_factory=lambda: ["README.md"])
    # Branch ref used when fetching documentation files via the GitHub GraphQL API
    # (repository.object(expression: "<ref>:<path>")). Defaults to "HEAD" (the
    # repository's default branch). Override if docs live on a dedicated branch
    # (e.g. "docs", "stable").
    doc_branch: str = "HEAD"
    scoring_models: list[str] = Field(default_factory=lambda: ["claude"])
    handled_label: str = "advocate-handled"
    escalation_label: str = "needs-human"
    holding_comment_template: str = (
        "Thanks for reaching out — a team member will follow up shortly."
    )
    acknowledgement_template: str = (
        "Thanks for the feature request! We've noted it for our roadmap."
    )
    redirect_template: str = (
        "This doesn't seem related to the project. "
        "For support, please visit {support_channel_url}."
    )
    # Used only in GitHub comment bodies (via _post_comment), never emitted
    # directly into structured log fields, so the emoji is safe here.
    disclosure_template: str = (
        "\U0001f916 This response was generated automatically — "
        "please verify before acting on it."
    )
    support_channel_url: str = ""
    github_repo: str = ""

    @model_validator(mode="after")
    def _validate_github_repo_when_enabled(self) -> AdvocateConfig:
        if self.enabled and not self.github_repo.strip():
            msg = "advocate.github_repo must be non-empty when advocate.enabled is True"
            raise ValueError(msg)
        return self


# ---------------------------------------------------------------------------
# 005 — Resilience config models
# ---------------------------------------------------------------------------


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

    model_config = SettingsConfigDict(env_prefix="COORDINARE_", extra="ignore", env_ignore_empty=True)

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

    poll_interval_seconds: int = Field(default=30, ge=10, le=300)
    blocked_reminder_hours: int = 24
    health_check_port: int = 8080
    output_mode: str = Field(default="human", pattern="^(human|structured)$")
    log_level: str = Field(default="info", pattern="^(debug|info|warning|error)$")
    heartbeat_interval_seconds: int = Field(default=30, ge=5, le=300)
    max_cycles: int | None = Field(default=None, ge=1)
    state_file_path: Path = Field(default=Path("./coordinare.state.json"))
    resilience: ResilienceConfig = Field(default_factory=ResilienceConfig)
    notifications: NotificationsConfig = Field(default_factory=NotificationsConfig)
    advocate: AdvocateConfig = Field(default_factory=AdvocateConfig)

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


