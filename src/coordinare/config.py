from __future__ import annotations

import string
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

from coordinare.models.notification import ChannelType, EventType

# ---------------------------------------------------------------------------
# 015 — Webhook config model
# ---------------------------------------------------------------------------


class WebhookConfig(BaseModel):
    enabled: bool = False
    secret: SecretStr | None = None
    path: str = "/webhook/github"

    @field_validator("path", mode="before")
    @classmethod
    def _validate_path(cls, v: Any) -> str:
        path_str = str(v).strip()
        if not path_str:
            msg = "webhooks.path must not be empty"
            raise ValueError(msg)
        if any(ch.isspace() for ch in path_str):
            msg = "webhooks.path must not contain whitespace"
            raise ValueError(msg)
        if not path_str.startswith("/"):
            path_str = "/" + path_str
        return path_str

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
# 018 — Performer Personas config models
# ---------------------------------------------------------------------------

PERSONA_MAX_LENGTH = 8_000


class PersonaConfig(BaseModel):
    """Per-role behavioral instructions for the AI agent."""

    instructions: str = ""

    @field_validator("instructions", mode="before")
    @classmethod
    def _validate_instructions_length(cls, v: Any) -> str:
        if v is None:
            return ""
        if not isinstance(v, str):
            msg = f"instructions must be a string, got {type(v).__name__}"
            raise ValueError(msg)
        if len(v) > PERSONA_MAX_LENGTH or len(v.strip()) > PERSONA_MAX_LENGTH:
            msg = f"Instructions exceed maximum length ({PERSONA_MAX_LENGTH} chars)"
            raise ValueError(msg)
        return v


class PersonasConfig(BaseModel):
    """Container for all eight role personas."""

    advocate: PersonaConfig = Field(default_factory=PersonaConfig)
    assessor: PersonaConfig = Field(default_factory=PersonaConfig)
    architect: PersonaConfig = Field(default_factory=PersonaConfig)
    implementer: PersonaConfig = Field(default_factory=PersonaConfig)
    reviewer: PersonaConfig = Field(default_factory=PersonaConfig)
    security: PersonaConfig = Field(default_factory=PersonaConfig)
    qa: PersonaConfig = Field(default_factory=PersonaConfig)
    tech_writer: PersonaConfig = Field(default_factory=PersonaConfig)


# ---------------------------------------------------------------------------
# 025 — Card Prioritization config
# ---------------------------------------------------------------------------


class PriorityConfig(BaseModel):
    """Configuration for priority-aware card selection from TODO column."""

    field_name: str | None = None  # GitHub Project V2 custom field name (e.g. "Priority")
    priority_order: list[str] = Field(default_factory=list)  # Custom sort precedence (e.g. ["P0", "P1", "P2"])


# ---------------------------------------------------------------------------
# 028 — Stuck Card Alerts config
# ---------------------------------------------------------------------------


class StuckAlertConfig(BaseModel):
    """Configuration for stuck card detection and alerting."""

    threshold_seconds: int = Field(default=1800, ge=0)  # 30 min default; 0 = disabled
    per_phase_thresholds: dict[str, int] = Field(default_factory=dict)  # phase → seconds; 0 = disabled


# ---------------------------------------------------------------------------
# 019 — Performer Lifecycle config models
# ---------------------------------------------------------------------------


class PerformerRoleConfig(BaseModel):
    """Configuration for a single performer role's backend.

    Fields that are None fall back to the global ProjectConfiguration
    defaults (e.g. agent_transport, agent_executable, transport_timeout_seconds).
    """

    backend: str = "opencode"
    transport: str | None = None
    image: str | None = None
    executable: str | None = None
    host: str | None = None
    port: int | None = None
    timeout_seconds: int | None = None


class PerformersConfig(BaseModel):
    """Per-role performer backend configuration.

    A role is considered configured when its field is non-None.
    Roles that are None are skipped in the lifecycle sequence.
    """

    advocate: PerformerRoleConfig | None = None
    assessor: PerformerRoleConfig | None = None
    architect: PerformerRoleConfig | None = None
    implementer: PerformerRoleConfig | None = None
    reviewer: PerformerRoleConfig | None = None
    security: PerformerRoleConfig | None = None
    qa: PerformerRoleConfig | None = None
    tech_writer: PerformerRoleConfig | None = None


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

    # Auth mode — "pat" (default) or "app"
    github_auth: Literal["pat", "app"] = "pat"
    github_token: SecretStr | None = None

    # GitHub App credentials (required when github_auth == "app")
    github_app_id: int | None = None
    github_private_key_path: Path | None = None
    github_installation_id: int | None = None

    agent_transport: Literal["subprocess", "ssh", "kubernetes"] = "subprocess"
    agent_executable: str = ""
    transport_timeout_seconds: int = Field(default=30, ge=1, le=300)

    agent_host: str = ""
    agent_port: int = 22
    agent_user: str = ""
    agent_key_path: Path = Path("~/.ssh/id_ed25519")
    agent_command: str | None = None
    # Docker image used when agent_transport = "kubernetes" (or future Docker transport)
    performer_image: str = "coordinare-performer:full"

    human_reviewers: list[str]

    poll_interval_seconds: int = Field(default=30, ge=0, le=3600)
    webhooks: WebhookConfig = Field(default_factory=WebhookConfig)
    blocked_reminder_hours: int = 24
    health_check_port: int = 8080
    dashboard_port: int = Field(default=8090)
    dashboard_host: str = "127.0.0.1"
    output_mode: str = Field(default="human", pattern="^(human|structured)$")
    log_level: str = Field(default="info", pattern="^(debug|info|warning|error)$")
    heartbeat_interval_seconds: int = Field(default=30, ge=5, le=300)
    max_cycles: int | None = Field(default=None, ge=1)
    state_file_path: Path = Field(default=Path("./coordinare.state.json"))
    health_check_timeout_seconds: int = Field(default=2, ge=1, le=30)
    optional_subsystems: list[str] = Field(default_factory=list)
    resilience: ResilienceConfig = Field(default_factory=ResilienceConfig)
    notifications: NotificationsConfig = Field(default_factory=NotificationsConfig)
    advocate: AdvocateConfig = Field(default_factory=AdvocateConfig)
    personas: PersonasConfig = Field(default_factory=PersonasConfig)
    performers: PerformersConfig = Field(default_factory=PerformersConfig)
    priority: PriorityConfig = Field(default_factory=PriorityConfig)
    stuck_alerts: StuckAlertConfig = Field(default_factory=StuckAlertConfig)

    # Card assessment backend — determines how assess_card evaluates card sufficiency.
    # "anthropic_api": direct Anthropic SDK call (requires ANTHROPIC_API_KEY)
    # "claude_cli":    subprocess `claude --print "..."` (uses local CLI auth)
    # "none":          skip assessment, assume all cards are sufficient
    assessment_backend: Literal["anthropic_api", "claude_cli", "opencode", "none"] = "anthropic_api"

    # 011 — Agent Workspace Management
    # COORDINARE_WORKSPACE_ROOT: optional path to a persistent directory (e.g. a PVC
    # mount) under which all workspace containers are created. When unset, falls back
    # to the system temporary directory (tempfile.gettempdir()).
    workspace_root: Path | None = Field(default=None)

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
        import os
        config_path = Path(path)
        raw: dict[str, Any] = {}
        if config_path.exists():
            expanded = os.path.expandvars(config_path.read_text())
            loaded = yaml.safe_load(expanded)
            if isinstance(loaded, dict):
                raw = loaded
        return cls(**raw)

    @model_validator(mode="after")
    def _validate_retry_backoff_caps(self) -> ProjectConfiguration:
        if self.poll_interval_seconds == 0:
            return self
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

    @model_validator(mode="after")
    def _validate_auth_config(self) -> ProjectConfiguration:
        if self.github_auth == "pat":
            if not self.github_token or not self.github_token.get_secret_value().strip():
                msg = "github.auth=pat requires github.token to be set"
                raise ValueError(msg)
            token = self.github_token.get_secret_value().strip()
            if token.startswith("${") and token.endswith("}"):
                msg = (
                    "github_token must be a real token value; unresolved placeholder detected. "
                    "Set COORDINARE_GITHUB_TOKEN or update config.yaml."
                )
                raise ValueError(msg)
        elif self.github_auth == "app":
            missing = []
            if self.github_app_id is None:
                missing.append("github_app_id")
            if self.github_private_key_path is None:
                missing.append("github_private_key_path")
            if self.github_installation_id is None:
                missing.append("github_installation_id")
            if missing:
                msg = (
                    "github.auth=app requires github_app_id, github_private_key_path, "
                    f"and github_installation_id (missing: {', '.join(missing)})"
                )
                raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _validate_webhook_config(self) -> ProjectConfiguration:
        if self.webhooks.enabled and (not self.webhooks.secret or not self.webhooks.secret.get_secret_value().strip()):
            msg = "webhooks.secret is required when webhooks.enabled=true"
            raise ValueError(msg)
        return self

    @field_validator("human_reviewers")
    @classmethod
    def _validate_human_reviewers(cls, value: list[str]) -> list[str]:
        if not value:
            msg = "human_reviewers must contain at least one entry"
            raise ValueError(msg)
        return value


