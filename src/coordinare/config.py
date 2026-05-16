from __future__ import annotations

import re
import string
from collections import Counter
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, PydanticBaseSettingsSource, SettingsConfigDict

from coordinare.models.notification import ChannelType, EventType
from coordinare.models.performer_endpoint import (
    PerformerEndpointConfig,
    detect_duplicate_endpoints,
)

# ---------------------------------------------------------------------------
# 052 — Branch collision strategy
# ---------------------------------------------------------------------------


class BranchCollisionStrategy(StrEnum):
    delete = "delete"
    suffix = "suffix"


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
    """Container for all nine role personas."""

    advocate: PersonaConfig = Field(default_factory=PersonaConfig)
    assessor: PersonaConfig = Field(default_factory=PersonaConfig)
    architect: PersonaConfig = Field(default_factory=PersonaConfig)
    implementer: PersonaConfig = Field(default_factory=PersonaConfig)
    reviewer: PersonaConfig = Field(default_factory=PersonaConfig)
    security: PersonaConfig = Field(default_factory=PersonaConfig)
    qa: PersonaConfig = Field(default_factory=PersonaConfig)
    tech_writer: PersonaConfig = Field(default_factory=PersonaConfig)
    closer: PersonaConfig = Field(default_factory=PersonaConfig)


# ---------------------------------------------------------------------------
# 030 — Live Requirement Sync config
# ---------------------------------------------------------------------------

RequirementChangePolicy = Literal["ignore", "warn", "re-dispatch"]


class CostTrackingConfig(BaseModel):
    """034: Cost and token tracking configuration."""

    cost_per_million_tokens: float = Field(default=3.0, ge=0.0)
    cost_budget_per_card: float | None = Field(default=None, ge=0.0)


# ---------------------------------------------------------------------------
# 062 — Coordinare's internal "brain" — what the coordinare uses on the podium
# to interpret cards and make routing decisions (separate from the assessor performer).
# ---------------------------------------------------------------------------

ConductingBackendName = Literal[
    "anthropic_api", "openai_api", "claude_cli", "codex_cli", "opencode", "none"
]


class ConductingConfig(BaseModel):
    """Configuration for the coordinare's internal "brain" (card assessor + ad-hoc prompts).

    Mirrors the performer config surface: pick a backend, optionally pin a model,
    and tune max_tokens / temperature.  Unset fields fall back to backend defaults.
    """

    backend: ConductingBackendName = "anthropic_api"
    model: str | None = None
    max_tokens: int = Field(default=4096, ge=1, le=200_000)
    temperature: float | None = None
    # Reasoning effort — applied to opencode (--effort), codex (model_reasoning_effort),
    # and openai (reasoning_effort).  No-op for anthropic_api / claude_cli where
    # extended thinking isn't enabled.  Default low because the conducting brain
    # runs short one-shot prompts where extra reasoning budget is mostly waste.
    effort: Literal["low", "medium", "high"] | None = "low"
    # CLI overrides for subprocess backends (claude_cli / codex_cli / opencode).
    executable: str | None = None
    # OpenAI-compatible base URL override (lets you point openai_api at proxies / Azure).
    base_url: str | None = None

    @field_validator("temperature")
    @classmethod
    def _validate_temperature(cls, v: float | None) -> float | None:
        if v is not None and not (0.0 <= v <= 2.0):
            raise ValueError(f"temperature must be between 0.0 and 2.0, got {v}")
        return v


# ---------------------------------------------------------------------------
# 033 — Smart Health-Check Retry config
# ---------------------------------------------------------------------------


class HealthCheckConfig(BaseModel):
    """Configuration for health-check retry in dispatch_performer."""

    max_attempts: int = Field(default=3, ge=1, le=10)  # total attempts including first (1 = no retry)
    backoff_seconds: float = Field(default=1.0, ge=0.0, le=30.0)  # base backoff; exponential: backoff * 2^(attempt-1)


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
    per_phase_thresholds: dict[str, int] = Field(
        default_factory=lambda: {
            "monitoring_performer": 3600,  # 60 min — codex sessions routinely run 30-40 min
            "monitoring_agent": 3600,      # 60 min — same rationale
        },
    )  # phase → seconds; 0 = disabled
    cooldown_seconds: int = Field(default=1800, ge=0)  # min interval between repeated stuck alerts


# ---------------------------------------------------------------------------
# 019 — Performer Lifecycle config models
# ---------------------------------------------------------------------------


class PerformerRoleConfig(BaseModel):
    """Configuration for a single performer role's backend.

    Fields that are None fall back to the global ProjectConfiguration
    defaults (e.g. agent_transport, agent_executable, transport_timeout_seconds).
    """

    backend: str = "opencode"
    model: str | None = None  # 037: specific model within backend (e.g. claude-sonnet-4-20250514)
    transport: str | None = None
    image: str | None = None
    executable: str | None = None
    host: str | None = None
    port: int | None = None
    timeout_seconds: int | None = None
    # 048: Maximum concurrent instances of this role.  Clamped to 1 for
    # assessor/closer (SINGLETON_STAGES).  0 disables the role entirely.
    max_concurrency: int = Field(default=1, ge=0)
    # 055: Backend-agnostic tuning knobs — translated to backend-specific params at dispatch.
    # effort:      low / medium / high  (opencode → --effort; anthropic → thinking budget)
    # temperature: 0.0-1.0             (lower = more deterministic; None = backend default)
    # max_tokens:  max output tokens    (primary cost-control lever)
    #   None / omitted → inherit from default (or backend default if no default set)
    #   0              → unlimited (explicitly remove cap, overrides any inherited default)
    #   N > 0          → cap at N tokens
    effort: Literal["low", "medium", "high"] | None = None
    temperature: float | None = None
    max_tokens: int | None = None

    @field_validator("temperature")
    @classmethod
    def _validate_temperature(cls, v: float | None) -> float | None:
        if v is not None and not (0.0 <= v <= 1.0):
            raise ValueError(f"temperature must be between 0.0 and 1.0, got {v}")
        return v

    @field_validator("max_tokens")
    @classmethod
    def _validate_max_tokens(cls, v: int | None) -> int | None:
        if v is not None and v < 0:
            raise ValueError(f"max_tokens must be >= 0 (use 0 for unlimited), got {v}")
        return v


class PerformersConfig(BaseModel):
    """Per-role performer backend configuration.

    A role is considered configured when its field is non-None.
    Roles that are None are skipped in the lifecycle sequence.

    The optional ``default`` entry supplies base values inherited by every
    configured role.  Role-level fields that are explicitly set override the
    default; fields left unset inherit from it.  This lets operators define a
    single backend / effort / max_tokens baseline and only spell out
    per-role exceptions.
    """

    default: PerformerRoleConfig | None = None
    advocate: PerformerRoleConfig | None = None
    assessor: PerformerRoleConfig | None = None
    architect: PerformerRoleConfig | None = None
    implementer: PerformerRoleConfig | None = None
    reviewer: PerformerRoleConfig | None = None
    security: PerformerRoleConfig | None = None
    qa: PerformerRoleConfig | None = None
    tech_writer: PerformerRoleConfig | None = None
    closer: PerformerRoleConfig | None = None

    def resolved_role(self, role_name: str) -> PerformerRoleConfig | None:
        """Return the effective config for a role, merging default + role overrides.

        Returns None when the role is not configured (skipped in the lifecycle).
        Explicitly-set role fields take precedence over the default; unset fields
        fall back to the default.

        max_tokens=0 in a role config is the "unlimited" sentinel: it overrides a
        default max_tokens and resolves to None (omitted from the backend payload).
        """
        role = getattr(self, role_name, None)
        if role is None:
            return None
        if self.default is None:
            resolved = role
        else:
            base = self.default.model_dump()
            for field in role.model_fields_set:
                base[field] = getattr(role, field)
            resolved = PerformerRoleConfig(**base)
        # Treat max_tokens=0 as the unlimited sentinel — convert to None so the
        # backend payload key is omitted (backend uses its own default = unlimited).
        if resolved.max_tokens == 0:
            resolved = resolved.model_copy(update={"max_tokens": None})
        return resolved


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
    wait_exp_base: float = Field(default=1.618, ge=1.0, le=5.0)  # golden ratio ≈ fibonacci growth


class ServiceCircuitConfig(BaseModel):
    failure_threshold: int = Field(default=3, ge=1, le=20)
    recovery_window_seconds: float = Field(default=120.0, ge=10.0, le=3600.0)
    observation_window_seconds: float = Field(default=300.0, ge=10.0, le=3600.0)


class ResilienceConfig(BaseModel):
    github_retry: ServiceRetryConfig = Field(
        default_factory=lambda: ServiceRetryConfig(
            attempts=5, wait_initial_seconds=3.0, wait_max_seconds=30.0, wait_jitter_seconds=2.0
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
            failure_threshold=5, recovery_window_seconds=180.0, observation_window_seconds=300.0
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


# ---------------------------------------------------------------------------
# 051 — Performer environment isolation
# ---------------------------------------------------------------------------


class BotIdentityConfig(BaseModel):
    """Git author/committer identity used for all performer commits."""

    name: str = "Coordinare Bot"
    email: str = "coordinare@localhost"


# ---------------------------------------------------------------------------
# 063 — LLM-driven service inference
# ---------------------------------------------------------------------------


class ServiceInferenceConfig(BaseModel):
    """Budgets and gates for the LLM service-inference pass.

    Phase 2 (LLM agent) ships with ``enabled=False`` by default. Operators
    flip the gate once the deterministic manual-override path (Phase 1) has
    been exercised on real symphonies and the inference cost envelope is
    understood.

    Field-to-phase mapping:

    * ``enabled`` — Phase 2: master switch. When False, only the Phase 1
      manual-override path runs and an absent override yields an empty manifest.
    * ``retry_budget`` — Phase 2: how many agent → validate loops the
      orchestrator runs before writing ``services.json.rejected``.
    * ``max_tool_calls`` — Phase 2: hard cap on the agent's sandbox tool
      invocations per attempt (cost & loop-prevention guard).
    * ``max_tokens`` — Phase 2: per-LLM-call token budget, surfaced in
      structured logs for cost telemetry.
    * ``web_search_enabled`` — Phase 2 gate, Phase 4 backend: when False the
      ``web_search`` tool returns a deterministic empty result. The real
      backend lands in Phase 4 alongside broader web-search integration.
    """

    enabled: bool = False
    retry_budget: int = Field(default=3, ge=0, le=10)
    max_tool_calls: int = Field(default=50, ge=1, le=500)
    max_tokens: int = Field(default=100_000, ge=1)
    web_search_enabled: bool = False


class EnvCacheConfig(BaseModel):
    """Env-cache bootstrap configuration (spec 063+).

    Phases:
      * Phase 1 — Deterministic manual-override (``.coordinare/score.json``).
      * Phase 2 — LLM-driven inference (this file's ``inference`` block).
      * Phase 3 — Cache invalidation via SHA of manifest ``cache_inputs``.
      * Phase 4 — Runtime-health self-healing & web-search backend.
    """

    inference: ServiceInferenceConfig = Field(default_factory=ServiceInferenceConfig)


class ProjectConfiguration(BaseSettings):
    """Application configuration loaded from YAML and COORDINARE_* env vars."""

    model_config = SettingsConfigDict(env_prefix="COORDINARE_", extra="ignore", env_ignore_empty=True)

    project_name: str = ""
    github_org: str
    github_project_number: int = 0

    # 036 — GitHub Enterprise: configurable API base URL and GraphQL endpoint
    github_api_url: str = "https://api.github.com"
    github_graphql_url: str = "https://api.github.com/graphql"

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
    trusted_bot_reviewers: list[str] = Field(default_factory=list)  # bot logins whose reviews are actionable

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
    # 050 — Only dispatch cards assigned to this GitHub login. None = no filter.
    assignee_filter: str | None = Field(default=None)
    # 051 — Git commit identity and subprocess env pass-through
    bot_identity: BotIdentityConfig = Field(default_factory=BotIdentityConfig)
    env_passthrough: list[str] = Field(default_factory=list)
    stuck_alerts: StuckAlertConfig = Field(default_factory=StuckAlertConfig)
    health_check: HealthCheckConfig = Field(default_factory=HealthCheckConfig)
    requirement_change_policy: RequirementChangePolicy = "warn"  # 030: ignore | warn | re-dispatch
    cost_tracking: CostTrackingConfig = Field(default_factory=CostTrackingConfig)

    # 035 — Multi-Card Parallelism
    max_concurrent_cards: int = Field(default=1, ge=1, le=20)

    # 045 — Maximum number of reviewer/security/qa → implementer feedback
    # cycles before blocking the card for human intervention.  Each
    # ``changes_requested`` / ``security_failed`` / ``qa_failed`` that routes
    # back to the implementer counts as one cycle.  Replaces the per-
    # Performance REVIEWER_MAX_CYCLES etc. which reset on every dispatch
    # and never actually bounded anything.  Set to 0 to disable the bound
    # (not recommended — a flaky reviewer can loop until token budget is
    # exhausted).
    max_feedback_cycles: int = Field(default=5, ge=0, le=50)

    # 062 — Coordinare's internal-brain config (backend / model / max_tokens / temperature).
    # Drives the card-assessor node and any ad-hoc reasoning calls.  Distinct from the
    # ``assessor`` performer (which executes a separate, heavier code-review role).
    conducting: ConductingConfig = Field(default_factory=ConductingConfig)

    # 011 — Agent Workspace Management
    # COORDINARE_WORKSPACE_ROOT: optional path to a persistent directory (e.g. a PVC
    # mount) under which all workspace containers are created. When unset, falls back
    # to the system temporary directory (tempfile.gettempdir()).
    workspace_root: Path | None = Field(default=None)

    # 052 — Stale Branch Cleanup
    stale_branch_cleanup: bool = True
    branch_collision_strategy: BranchCollisionStrategy = BranchCollisionStrategy.delete

    # 053 — PR churn guard
    # Maximum number of CLOSED (unmerged) PRs linked to a card's issue before
    # coordinare blocks fresh implementer dispatches. 0 disables this guard.
    max_closed_pr_attempts_per_issue: int = Field(default=0, ge=0, le=100)

    # 055 — QA visual testing
    qa_docker_enabled: bool = True
    qa_playwright_image: str = "mcr.microsoft.com/playwright:v1.44.0-jammy"
    qa_screenshot_timeout_s: int = Field(default=120, ge=10, le=600)
    qa_screenshot_upload_retries: int = Field(default=3, ge=0, le=10)

    # 056 — Containerized performer registrations.
    # Each entry registers an ephemeral or persistent performer endpoint;
    # subprocess-mode entries coexist for backwards compatibility but carry
    # only id + roles (no image/endpoint/auth_token).
    performer_endpoints: list[PerformerEndpointConfig] = Field(default_factory=list)

    # 060 — Performer environment caching.
    # Root directory under which per-symphony env-cache subdirectories are created.
    # Expanded to an absolute path at load time.
    # Lives here on ProjectConfiguration (the global-defaults layer) rather than in a
    # separate GlobalConfig model because per-symphony configs already overlay this via
    # effective_config() — there is no architectural need for another indirection.
    env_cache_root: Path = Path("~/.coordinare/env-caches/")

    # 063 — LLM-driven service inference for env-cache bootstrap.
    env_cache: EnvCacheConfig = Field(default_factory=lambda: EnvCacheConfig())

    @field_validator("env_cache_root", mode="after")
    @classmethod
    def _expand_env_cache_root(cls, v: Path) -> Path:
        return v.expanduser()

    @model_validator(mode="after")
    def _validate_performer_endpoints(self) -> ProjectConfiguration:
        ids = [cfg.id for cfg in self.performer_endpoints]
        dup_ids = {i for i, n in Counter(ids).items() if n > 1}
        if dup_ids:
            raise ValueError(
                f"performer_endpoints contains duplicate id(s): {sorted(dup_ids)}"
            )
        dup_endpoints = detect_duplicate_endpoints(self.performer_endpoints)
        if dup_endpoints:
            raise ValueError(
                f"performer_endpoints contains duplicate endpoint URL(s): {dup_endpoints}"
            )
        return self

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
        # 062 — assessment_backend was replaced by the structured ``conducting`` block.
        # pydantic's extra="ignore" would silently drop the old flat key, leaving the
        # deployment on defaults; fail loudly with a migration hint instead.  Check
        # both YAML and the env-var leg (COORDINARE_ASSESSMENT_BACKEND) since either
        # path silently no-ops without this guard.
        legacy_env = os.environ.get("COORDINARE_ASSESSMENT_BACKEND")
        if "assessment_backend" in raw or legacy_env:
            old = raw.get("assessment_backend", legacy_env)
            source = "config.yaml" if "assessment_backend" in raw else "COORDINARE_ASSESSMENT_BACKEND env var"
            raise ValueError(
                f"{source} uses the legacy 'assessment_backend' key, which was replaced "
                "in spec 062 by the structured 'conducting:' block. Migrate to:\n"
                "  conducting:\n"
                f"    backend: {old!r}\n"
                "    model: <model-id>\n"
                "    max_tokens: 4096\n"
                "    temperature: 0.0\n"
                "See config.example.yaml for the full schema."
            )
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

    @field_validator("github_api_url", "github_graphql_url", mode="before")
    @classmethod
    def _validate_github_urls(cls, v: Any) -> str:
        url = str(v).strip()
        if any(ch.isspace() for ch in url):
            msg = "GitHub URL must not contain whitespace"
            raise ValueError(msg)
        url = url.rstrip("/")
        parsed = urlparse(url)
        # Redact credentials/query/fragment from error messages
        safe = f"{parsed.scheme}://{parsed.hostname or ''}{parsed.path or ''}"
        if parsed.scheme not in {"http", "https"}:
            msg = f"GitHub URL must use http or https scheme, got {parsed.scheme!r}: {safe}"
            raise ValueError(msg)
        if not parsed.netloc:
            msg = f"GitHub URL must include a host (netloc), got: {safe!r}"
            raise ValueError(msg)
        if parsed.username is not None or parsed.password is not None:
            msg = f"GitHub URL must not include embedded credentials: {safe!r}"
            raise ValueError(msg)
        if parsed.query or parsed.fragment:
            msg = f"GitHub URL must not include query parameters or fragments: {safe!r}"
            raise ValueError(msg)
        # Restrict http to localhost/loopback to prevent credential leaks
        if parsed.scheme == "http":
            host = parsed.hostname or ""
            if host not in ("localhost", "127.0.0.1", "::1"):
                msg = f"GitHub URL with http scheme is only allowed for localhost, got host={host!r}: {safe}"
                raise ValueError(msg)
        return url

    @field_validator("human_reviewers")
    @classmethod
    def _validate_human_reviewers(cls, value: list[str]) -> list[str]:
        if not value:
            msg = "human_reviewers must contain at least one entry"
            raise ValueError(msg)
        return value


# ---------------------------------------------------------------------------
# 057 — Symphony Management & Multi-Project Orchestration
# ---------------------------------------------------------------------------


class SymphonyConfig(BaseModel):
    """A named project orchestration configuration."""

    model_config = ConfigDict(extra="forbid")

    name: str
    github_project_number: int = Field(ge=1)
    enabled: bool = True
    overrides: dict[str, Any] | None = None   # partial ProjectConfiguration fields merged with global
    personas: dict[str, Any] | None = None    # per-symphony persona overrides

    # 060 — Performer environment caching per symphony.
    env_bootstrap_performer_id: str | None = None
    env_spec_files: list[str] = Field(default_factory=lambda: ["README.md"])

    @field_validator("env_spec_files")
    @classmethod
    def _validate_env_spec_files(cls, v: list[str]) -> list[str]:
        for entry in v:
            if entry.startswith("/"):
                msg = f"env_spec_files entries must be relative paths, got: {entry!r}"
                raise ValueError(msg)
        return v

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        if not re.match(r"^[a-z0-9]([a-z0-9\-]*[a-z0-9])?$", v):
            msg = "name must be alphanumeric + dash, start/end with alphanumeric"
            raise ValueError(msg)
        return v

    def effective_config(self, global_config: ProjectConfiguration) -> ProjectConfiguration:
        """Resolve effective config by merging global with overrides."""
        # Use exclude_unset=True so that fields which were never explicitly set
        # (i.e. pure schema defaults) are omitted from the dict.  When
        # ProjectConfiguration(**base_dict) is reconstructed below, pydantic will
        # only mark those omitted fields as unset — preserving the model_fields_set
        # state that resolved_role() relies on to distinguish explicit overrides
        # from inherited defaults (fixing the backend-inheritance bug).
        base_dict = global_config.model_dump(exclude_unset=True)
        if self.overrides:
            valid_fields = set(ProjectConfiguration.model_fields)
            # github_project_number is a reserved per-symphony field; it cannot be
            # set via overrides because the symphony's own value always takes precedence,
            # so any override value would be silently discarded.
            reserved_keys = frozenset({"github_project_number"})
            reserved_used = set(self.overrides) & reserved_keys
            if reserved_used:
                msg = (
                    f"Override keys {sorted(reserved_used)} are reserved per-symphony fields; "
                    "set them directly on the symphony configuration instead of via overrides."
                )
                raise ValueError(msg)
            unknown = set(self.overrides) - valid_fields
            if unknown:
                msg = f"Unknown override keys: {sorted(unknown)}"
                raise ValueError(msg)
            base_dict.update(self.overrides)
        # Per-symphony fields always take precedence over global defaults.
        # project_name: use symphony name unless the global config already has one
        # (preserves backward compatibility with legacy single-project configs).
        base_dict["github_project_number"] = self.github_project_number
        if not base_dict.get("project_name"):
            base_dict["project_name"] = self.name
        return ProjectConfiguration(**base_dict)


class OrchestraConfig(BaseModel):
    """Shared performer pool configuration."""

    model_config = ConfigDict(extra="forbid")

    mode: Literal["shared_pool"] = "shared_pool"
    performers: list[Any] = Field(default_factory=list)
    allocation_strategy: Literal["round_robin", "priority_order"] = "priority_order"


class CoordinareConfiguration(BaseModel):
    """Root configuration combining global defaults + multiple symphonies + orchestra."""

    model_config = ConfigDict(extra="forbid")

    global_config: ProjectConfiguration
    symphonies: list[SymphonyConfig]
    orchestra: OrchestraConfig = Field(default_factory=OrchestraConfig)

    @field_validator("symphonies")
    @classmethod
    def validate_non_empty(cls, v: list[SymphonyConfig]) -> list[SymphonyConfig]:
        if not v:
            msg = "at least one symphony is required"
            raise ValueError(msg)
        return v

    @field_validator("symphonies")
    @classmethod
    def validate_unique_names(cls, v: list[SymphonyConfig]) -> list[SymphonyConfig]:
        names = [s.name for s in v]
        if len(names) != len(set(names)):
            msg = "symphony names must be unique"
            raise ValueError(msg)
        return v

    @field_validator("symphonies")
    @classmethod
    def validate_unique_boards(cls, v: list[SymphonyConfig]) -> list[SymphonyConfig]:
        boards = [s.github_project_number for s in v]
        if len(boards) != len(set(boards)):
            msg = "symphony project numbers must be unique"
            raise ValueError(msg)
        return v

    @model_validator(mode="after")
    def validate_env_bootstrap_performer_ids(self) -> CoordinareConfiguration:
        known_ids = {p.id for p in self.global_config.performer_endpoints if hasattr(p, "id")}
        for symphony in self.symphonies:
            if (
                symphony.env_bootstrap_performer_id is not None
                and symphony.env_bootstrap_performer_id not in known_ids
            ):
                msg = (
                    f"Symphony '{symphony.name}' env_bootstrap_performer_id "
                    f"'{symphony.env_bootstrap_performer_id}' not found in performer_endpoints"
                )
                raise ValueError(msg)
        return self

