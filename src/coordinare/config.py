from __future__ import annotations

import re
import string
from collections import Counter
from enum import StrEnum
from pathlib import Path, PurePosixPath
from typing import Any, Literal
from urllib.parse import urlparse

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    RootModel,
    SecretStr,
    field_validator,
    model_validator,
)
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
    # 069 FR-004: minimum wall-clock interval between `card_blocked`
    # re-emissions for the same card.  Gates `notify()` against the per-session
    # `last_blocked_slack_delivered_at` watermark so cycle-rate dispatch loops
    # can't bypass dedup once the per-channel window expires.
    card_blocked_reminder_cooldown_seconds: int = Field(default=3600, ge=0)
    # 071 FR-005: cap on total inlined CI log content per PR-checks BOUNCE body.
    # `0` disables log inlining and falls back to the name-only bounce body.
    pr_checks_bounce_log_max_chars: int = Field(default=6000, ge=0)

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


class ScopeTierBehavior(BaseModel):
    """074 — Per-tier behavior for a persona at a given scope depth."""

    max_tool_calls: int | None = Field(default=None, ge=1, le=500)
    prompt_addon: str = Field(default="", max_length=4096)


class ScopeBehavior(BaseModel):
    """074 — Per-persona tier-specific behavior (skim/normal/full)."""

    model_config = ConfigDict(extra="forbid")

    skim: ScopeTierBehavior | None = None
    normal: ScopeTierBehavior | None = None
    full: ScopeTierBehavior | None = None


class PersonaConfig(BaseModel):
    """Per-role behavioral instructions for the AI agent."""

    instructions: str = ""
    # 074 — Persona scope tiering (opt-in per persona; FR-010 additive default).
    scope_behavior: ScopeBehavior | None = None

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
    # Name of the env var holding the bearer token for openai_api.  Defaults to
    # OPENAI_API_KEY; set to e.g. "LITELLM_MASTER_KEY" when pointing base_url at
    # a proxy that uses a different env var.
    api_key_env: str | None = None

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
    # 138: silence before the dashboard marks a card quiet. 0 disables. Must stay
    # below the smallest stuck threshold (FR-029) — at 300 s it fires 6x sooner
    # than the fastest stuck alert and 12x sooner than the monitoring-phase one.
    quiet_threshold_seconds: int = Field(default=300, ge=0)


# ---------------------------------------------------------------------------
# 080 — Dual-model orchestration config catalogs
#
# Root-level, reference-by-name catalogs that unify all model selection:
#   performer.mode -> modes[] -> model_endpoints[] -> endpoints[]
# `endpoint.kind` splits native (vendor cloud, no override/proxy) from
# self-hosted (provider override / proxy-eligible). `mode.strategy` decides
# single-model vs the dual-model planner/executor proxy.
# ---------------------------------------------------------------------------

_NATIVE_ENDPOINT_KINDS = frozenset({"openai", "anthropic"})
_SELF_HOSTED_ENDPOINT_KINDS = frozenset({"litellm", "ollama", "vllm"})

# 083 US3 — weak-judge denylist. The `security` role is the authoritative review
# lever; binding it to one of these models is a load-time error (fail-closed).
# These models fail contract-bound roles across coding agents (MEMORY.md
# project_qwen_coder_limitations) and must never gate security. Matched against
# the bare model name (provider prefixes like "spark/" are stripped first).
_SECURITY_MODEL_DENYLIST = frozenset({
    "qwen3.6:35b",
    "qwq:32b",
    "qwen2.5:14b-instruct",
    "qwen2.5:32b",
    "qwen3-coder:30b",
})

# Default regex for the think_once "error marker" (FR-016); scans incoming
# tool results only.
DEFAULT_THINK_ONCE_ERROR_PATTERN = r"(?i)\b(error|exception|traceback|fatal|exit code [1-9])\b"


class Endpoint(BaseModel):
    """A model-serving location (080).

    ``kind`` decides behavior: native vendor kinds (openai/anthropic) use the
    harness's own client with no provider override and are never proxied;
    self-hosted kinds (litellm/ollama/vllm) require a ``base_url`` and are
    provider-override / proxy eligible.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    kind: Literal["litellm", "ollama", "vllm", "openai", "anthropic"]
    base_url: str | None = None
    auth_env: str | None = None  # NAME of the env var holding the token — never the secret

    @model_validator(mode="after")
    def _validate_kind_rules(self) -> Endpoint:
        if self.kind in _SELF_HOSTED_ENDPOINT_KINDS and not self.base_url:
            raise ValueError(
                f"endpoint '{self.name}': kind '{self.kind}' is self-hosted and requires base_url"
            )
        if self.kind in _NATIVE_ENDPOINT_KINDS and self.base_url:
            raise ValueError(
                f"endpoint '{self.name}': kind '{self.kind}' is native and must not set base_url"
            )
        return self

    @property
    def is_native(self) -> bool:
        return self.kind in _NATIVE_ENDPOINT_KINDS


class ModelEndpoint(BaseModel):
    """A named (model @ endpoint) pair — the unit of model selection (080)."""

    model_config = ConfigDict(extra="forbid")

    name: str
    endpoint: str  # references Endpoint.name
    model: str


class Mode(BaseModel):
    """A named orchestration behavior (080).

    ``strategy: single`` reads only ``tool`` and reproduces single-model
    behavior (no proxy). The multi-model strategies (always/conditional/
    think_once) drive the in-container planner/executor proxy.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    strategy: Literal["single", "always", "conditional", "think_once"]
    tool: str  # ModelEndpoint.name — executor / sole model (required for all strategies)
    thinking: str | None = None  # ModelEndpoint.name — planner (multi-model strategies)
    classifier: str | None = None  # ModelEndpoint.name (conditional only)
    threshold: float | None = None  # conditional only
    invalidate_after_turns: int | None = Field(default=None, ge=1)  # think_once only
    invalidate_on_error: bool | None = None  # think_once only
    error_pattern: str | None = None  # think_once only; default applied at use-site
    expose_plan_as: Literal["thinking", "prepend_content", "drop"] = "thinking"
    on_think_error: Literal["fall_back_to_act", "fail"] = "fall_back_to_act"

    @model_validator(mode="after")
    def _validate_strategy_fields(self) -> Mode:
        think_once_params = {
            "invalidate_after_turns": self.invalidate_after_turns,
            "invalidate_on_error": self.invalidate_on_error,
            "error_pattern": self.error_pattern,
        }
        conditional_params = {"classifier": self.classifier, "threshold": self.threshold}

        if self.strategy == "single":
            forbidden = [
                k
                for k, v in ({"thinking": self.thinking} | conditional_params | think_once_params).items()
                if v is not None
            ]
            if forbidden:
                raise ValueError(
                    f"mode '{self.name}': strategy 'single' must not set {', '.join(sorted(forbidden))} "
                    f"(single is one model via 'tool')"
                )
        else:
            if self.thinking is None:
                raise ValueError(
                    f"mode '{self.name}': strategy '{self.strategy}' requires 'thinking'"
                )
            if self.strategy == "conditional":
                missing = [k for k, v in conditional_params.items() if v is None]
                if missing:
                    raise ValueError(
                        f"mode '{self.name}': strategy 'conditional' requires {', '.join(sorted(missing))}"
                    )
            else:
                set_conditional = [k for k, v in conditional_params.items() if v is not None]
                if set_conditional:
                    raise ValueError(
                        f"mode '{self.name}': strategy '{self.strategy}' must not set "
                        f"{', '.join(sorted(set_conditional))} (conditional-only)"
                    )
            if self.strategy != "think_once":
                set_think_once = [k for k, v in think_once_params.items() if v is not None]
                if set_think_once:
                    raise ValueError(
                        f"mode '{self.name}': strategy '{self.strategy}' must not set "
                        f"{', '.join(sorted(set_think_once))} (think_once-only)"
                    )

        if self.threshold is not None and not (0.0 <= self.threshold <= 1.0):
            raise ValueError(f"mode '{self.name}': threshold must be between 0.0 and 1.0")
        return self


# ---------------------------------------------------------------------------
# 019 — Performer Lifecycle config models
# ---------------------------------------------------------------------------


class PerformerRoleConfig(BaseModel):
    """Configuration for a single performer role's backend.

    Fields that are None fall back to the global ProjectConfiguration
    defaults (e.g. agent_transport, agent_executable, transport_timeout_seconds).
    """

    backend: str = "opencode"
    # 080: model selection moved to the root-level catalogs. A role references a
    # `mode` (modes → model_endpoints → endpoints); inline `model`/`base_url`/
    # `api_key_env`/`auth_token_env` are removed (hard cut, see validator below).
    mode: str | None = None  # references modes[].name
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

    @model_validator(mode="before")
    @classmethod
    def _reject_removed_inline_model_fields(cls, data: Any) -> Any:
        """080 hard cut (FR-006): inline model/endpoint fields are removed.

        Their presence is a loud error directing the operator to the catalogs,
        rather than being silently ignored.
        """
        if isinstance(data, dict):
            present = [
                k for k in ("model", "base_url", "api_key_env", "auth_token_env") if k in data
            ]
            if present:
                raise ValueError(
                    f"performer role sets removed inline field(s) {', '.join(present)} — "
                    "080 moved all model selection to the root-level "
                    "endpoints/model_endpoints/modes catalogs; reference a mode with "
                    "`mode: <mode-name>` instead (see specs/080-dual-model-orchestration)."
                )
        return data

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
    env_bootstrap: PerformerRoleConfig | None = None

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

    # 116: master switch for the coordinare-managed stateful-service subsystem
    # (091→115: deb-fetch persona, rendered services-{start,health,stop}.sh, the
    # 101 service-readiness gate). Default OFF restores the pre-091 behavior where
    # the env-bootstrap PERFORMER owns environment setup end-to-end (it runs
    # service inference, writes its own scripts, and is verified by the toolchain
    # verify.sh). Flip to True to re-enable coordinare-managed services.
    coordinare_manages_services: bool = False


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
    # 080 — dual-model orchestration catalogs (reference-by-name; see Endpoint/ModelEndpoint/Mode)
    endpoints: list[Endpoint] = Field(default_factory=list)
    model_endpoints: list[ModelEndpoint] = Field(default_factory=list)
    modes: list[Mode] = Field(default_factory=list)
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

    # 073 (drive-by) — Hold all regular card dispatch while env_bootstrap is
    # in-flight.  When ``False`` (default), only the on-disk ``activate.sh``
    # gate applies; consumers can dispatch as soon as bootstrap writes the
    # file, which may overlap with bootstrap container teardown.  When
    # ``True``, consumers hold until ``EnvCacheState.bootstrap_in_flight``
    # clears, guaranteeing zero overlap with the bootstrap container --
    # required when bootstrap and consumer performers share a single LLM
    # backend (e.g. one ollama server) that can't tolerate concurrent calls.
    serialize_env_bootstrap: bool = False

    # 076 (live QA #150) — hard wall-clock ceiling on a single env_bootstrap
    # container.  The bootstrap poll loop declares failure AND reaps the
    # container once this elapses, so a slow or hung bootstrap can't gate the
    # whole symphony (the legacy ceiling was a hardcoded ~2 h).  A failed
    # bootstrap leaves the cache un-ready, so the next dispatch cycle simply
    # re-dispatches it (automatic retry).  Set to 0 to restore the legacy
    # ~2 h behaviour (no early reap).  Tune up for slow single-tenant
    # backends whose first-run install legitimately exceeds the default.
    bootstrap_max_seconds: int = Field(default=3600, ge=0, le=21600)

    # 076 (live QA #150) — idle reap for env_bootstrap.  The wall-clock budget
    # above can't tell "slow but progressing" from "hung", so a stuck bootstrap
    # still burns the whole budget.  When the bootstrap container produces no
    # NEW meaningful log output (LLM calls / install commands — coordinare's own
    # status-poll lines are filtered out) for this many seconds, reap it early.
    # Must be comfortably larger than the slowest legitimate gap (a long LLM
    # completion or install step); 600 s is safe for slow single-tenant
    # backends.  Set to 0 to disable idle reaping (wall-clock budget only).
    bootstrap_idle_timeout_seconds: int = Field(default=600, ge=0, le=7200)

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

    # 068 — Optional host directory for per-container + per-job logs.
    # When set, every ephemeral performer container is started with this
    # path bind-mounted at ``/var/log/performer`` and ``PERFORMER_LOG_DIR``
    # exported into its env. The coordinare also dumps ``docker logs <id>``
    # into this directory before stopping each container. Backends that
    # honour ``PERFORMER_LOG_DIR`` (e.g. Hermes) write per-job prompt /
    # stdout / stderr artifacts beside the container log.
    performer_log_dir: Path | None = Field(default=None)

    # 052 — Stale Branch Cleanup
    stale_branch_cleanup: bool = True
    branch_collision_strategy: BranchCollisionStrategy = BranchCollisionStrategy.delete

    # 053 — PR churn guard
    # Maximum number of CLOSED (unmerged) PRs linked to a card's issue before
    # coordinare blocks fresh implementer dispatches. 0 disables this guard.
    max_closed_pr_attempts_per_issue: int = Field(default=0, ge=0, le=100)

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

    # 088 (FR-009): bootstrap circuit breaker — consecutive failed bootstrap
    # attempts allowed per spec SHA before the breaker trips
    # (bootstrap_exhausted) and dispatch stops until the spec changes.
    env_bootstrap_max_attempts: int = Field(default=3, ge=1, le=20)

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

    @model_validator(mode="after")
    def _validate_orchestration_catalogs(self) -> ProjectConfiguration:
        """080: validate endpoints/model_endpoints/modes references + unique names.

        No-op when all three catalogs are empty (backwards compatible). Enforces
        unique names within each catalog and that every reference resolves
        (model_endpoint.endpoint -> endpoints; mode.{tool,thinking,classifier}
        -> model_endpoints). The performer.mode -> modes check is added when the
        performer config grows a `mode` field.
        """
        def _dups(names: list[str]) -> list[str]:
            return sorted({n for n, c in Counter(names).items() if c > 1})

        for label, items in (
            ("endpoints", self.endpoints),
            ("model_endpoints", self.model_endpoints),
            ("modes", self.modes),
        ):
            dups = _dups([i.name for i in items])
            if dups:
                raise ValueError(f"{label} contains duplicate name(s): {dups}")

        endpoint_names = {e.name for e in self.endpoints}
        model_endpoint_names = {m.name for m in self.model_endpoints}

        for me in self.model_endpoints:
            if me.endpoint not in endpoint_names:
                raise ValueError(
                    f"model_endpoint '{me.name}' references unknown endpoint '{me.endpoint}'"
                )

        for mode in self.modes:
            for field in ("tool", "thinking", "classifier"):
                ref = getattr(mode, field)
                if ref is not None and ref not in model_endpoint_names:
                    raise ValueError(
                        f"mode '{mode.name}' {field} references unknown model_endpoint '{ref}'"
                    )

        # performer.mode -> modes[] (FR-004/FR-005). Runs regardless of catalog
        # contents so a stray mode reference is caught even with empty catalogs.
        mode_names = {m.name for m in self.modes}
        for role_name in (
            "default", "advocate", "assessor", "architect", "implementer",
            "reviewer", "security", "qa", "tech_writer", "closer", "env_bootstrap",
        ):
            role = getattr(self.performers, role_name, None)
            if role is not None and role.mode is not None and role.mode not in mode_names:
                raise ValueError(
                    f"performers.{role_name} references unknown mode '{role.mode}'"
                )
        return self

    @model_validator(mode="after")
    def _validate_security_model_not_denylisted(self) -> ProjectConfiguration:
        """083 US3: reject a `security` role bound to a weak-judge model (fail-closed).

        Scope is the `security` role ONLY — it is the authoritative review lever,
        so its model must not come from the known weak-judge denylist. Resolves
        through the role's mode → tool leg → model_endpoint.model (reuse of the
        dispatch resolver), strips any provider prefix, and raises at load on a
        denylist hit. Logs nothing (no `auth_env` values, FR-011)."""
        dispatch = self.resolve_performer_dispatch_model("security")
        model = dispatch.get("model")
        if not model:
            return self
        bare = model.rsplit("/", 1)[-1]
        if bare in _SECURITY_MODEL_DENYLIST:
            raise ValueError(
                f"performers.security model '{model}' is on the weak-judge denylist "
                f"and may not gate security review (spec 083); choose a capable model"
            )
        return self

    def resolve_endpoint(self, name: str) -> Endpoint | None:
        """080: return the Endpoint by name, or None."""
        return next((e for e in self.endpoints if e.name == name), None)

    def resolve_model_endpoint(self, name: str) -> ModelEndpoint | None:
        """080: return the ModelEndpoint by name, or None."""
        return next((m for m in self.model_endpoints if m.name == name), None)

    def resolve_mode(self, name: str) -> Mode | None:
        """080: return the Mode by name, or None."""
        return next((m for m in self.modes if m.name == name), None)

    def resolve_performer_dispatch_model(self, role_name: str) -> dict[str, str]:
        """080: resolve a role's mode → model/endpoint into dispatch card_context.

        Returns the subset of {model, base_url, api_key_env, auth_token_env} that
        applies (empty when the role has no mode). Self-hosted endpoints map to
        bearer auth (auth_token_env) + a base_url override; native endpoints map
        to api_key_env with no override. Uses the mode's ``tool`` leg as the
        primary model — correct for ``single`` and the executor leg for the
        multi-model strategies (whose planner orchestration is layered on in 080
        US2 via the dispatch ``orchestration`` block).
        """
        role = self.performers.resolved_role(role_name)
        if role is None or role.mode is None:
            return {}
        mode = self.resolve_mode(role.mode)
        if mode is None:
            return {}
        me = self.resolve_model_endpoint(mode.tool)
        if me is None:
            return {}
        out: dict[str, str] = {"model": me.model}
        ep = self.resolve_endpoint(me.endpoint)
        if ep is None:
            return out
        if ep.is_native:
            if ep.auth_env:
                out["api_key_env"] = ep.auth_env
        else:
            if ep.base_url:
                out["base_url"] = ep.base_url
            if ep.auth_env:
                out["auth_token_env"] = ep.auth_env
        return out

    def _upstream_ref(self, model_endpoint_name: str | None) -> dict[str, Any] | None:
        """080: resolve a model_endpoint name into a proxy UpstreamRef dict.

        ``wire_format`` follows the endpoint kind (anthropic → anthropic; openai/
        litellm/ollama/vllm → openai-compatible); ``auth_style`` is x-api-key for
        native anthropic, bearer otherwise. ``auth_env`` is the env-var NAME (the
        token itself rides the existing secrets path), never the secret value.
        """
        if model_endpoint_name is None:
            return None
        me = self.resolve_model_endpoint(model_endpoint_name)
        if me is None:
            return None
        ep = self.resolve_endpoint(me.endpoint)
        kind = ep.kind if ep else "openai"
        return {
            "name": me.name,
            "model": me.model,
            "wire_format": "anthropic" if kind == "anthropic" else "openai",
            "base_url": ep.base_url if ep else None,
            "auth_env": ep.auth_env if ep else None,
            "auth_style": "x-api-key" if kind == "anthropic" else "bearer",
        }

    def resolve_performer_orchestration(self, role_name: str) -> dict[str, Any] | None:
        """080: resolve a role's mode into the dispatch ``orchestration`` block.

        Returns None for ``single`` (no proxy) or an unmoded role. Otherwise a
        dict the performer hands to ``DualModelProxy.build_strategy``: strategy +
        resolved tool/thinking(/classifier) UpstreamRefs + strategy params.
        """
        role = self.performers.resolved_role(role_name)
        if role is None or role.mode is None:
            return None
        mode = self.resolve_mode(role.mode)
        if mode is None or mode.strategy == "single":
            return None
        out: dict[str, Any] = {
            "strategy": mode.strategy,
            "tool": self._upstream_ref(mode.tool),
            "thinking": self._upstream_ref(mode.thinking),
            "expose_plan_as": mode.expose_plan_as,
            "on_think_error": mode.on_think_error,
        }
        if mode.strategy == "conditional":
            out["classifier"] = self._upstream_ref(mode.classifier)
            out["threshold"] = mode.threshold
        if mode.strategy == "think_once":
            out["invalidate_after_turns"] = mode.invalidate_after_turns
            out["invalidate_on_error"] = mode.invalidate_on_error
            out["error_pattern"] = mode.error_pattern
        return out

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
# 064 — Closer PR-Checks Gate
# ---------------------------------------------------------------------------


class CloserPrChecksConfig(BaseModel):
    """Per-symphony config for the closer PR-checks gate (spec 064)."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    pending_timeout_seconds: int = Field(default=900, ge=60)
    poll_interval_seconds: int = Field(default=30, ge=5)
    fail_open_on_error: bool = True
    treat_unknown_required_as: Literal["pass", "block"] = "pass"


# ---------------------------------------------------------------------------
# 075 — Implementer CI Gate (nested under PersonaScopeConfig)
# ---------------------------------------------------------------------------


class PersonaCheckMapPerDepth(BaseModel):
    """Per-depth glob patterns selecting required CI checks for a persona (spec 075).

    The ``skim``/``normal``/``full`` fields correspond to the ``Depth`` values
    from ``session.Depth`` and are used when 074 persona-scope tiering is active
    (the runtime scope supplies a depth). The ``any`` field is depth-agnostic:
    it lets the implementer CI gate be scoped **without** enabling 074 tiering
    (077 decoupling) and also serves as a fallback when the depth-specific list
    is empty. An unrecognised depth value falls through to ``any``, then to
    resolver layer 2.
    """

    model_config = ConfigDict(extra="forbid")

    any: list[str] = Field(default_factory=list)
    skim: list[str] = Field(default_factory=list)
    normal: list[str] = Field(default_factory=list)
    full: list[str] = Field(default_factory=list)


class PersonaCheckMapConfig(RootModel[dict[str, PersonaCheckMapPerDepth]]):
    """Mapping persona name -> per-depth check globs (spec 075)."""


class CIGateConfig(BaseModel):
    """Implementer CI gate config nested under PersonaScopeConfig (spec 075)."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    max_bounces_per_head: int = Field(default=3, ge=1, le=20)
    pending_timeout_seconds: int = Field(default=900, ge=60, le=7200)


class LocalTestGateConfig(BaseModel):
    """Implementer local test gate (spec 089), sibling of CIGateConfig.

    Opt-in pre-filter: when enabled, the implementer runs the detected
    test_command locally before push. Default-off → byte-identical to pre-089
    (SC-005). ``max_fix_attempts`` is coordinare-only (drives the bounded
    self-fix loop); it is never sent to the performer.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    timeout_seconds: int = Field(default=600, ge=60, le=7200)
    max_fix_attempts: int = Field(default=2, ge=0, le=20)


# ---------------------------------------------------------------------------
# 090 — Baseline Repair Autonomy (three guarded-autonomy layers, all default-off)
# ---------------------------------------------------------------------------


class BaselinePreventionGateConfig(BaseModel):
    """L1 merge-precondition gate (spec 090, US1).

    When enabled, the closer refuses to merge while the base branch's REQUIRED
    checks are red — re-read every cycle, fail-safe (an unreadable base never
    hard-blocks). Default-off → routing/verdicts/merge stay byte-identical to
    the pre-feature baseline (SC-006).
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False


class BaselineClassificationGateConfig(BaseModel):
    """L2 inherited/introduced/flake/unknown classification (spec 090, US2).

    Observe-only when enabled: compares head failures against a merge-base
    baseline using the reason-sensitive failure signature and emits the
    classification, taking no autonomous action. Default-off (SC-006).
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False


class InheritedRepairGateConfig(BaseModel):
    """L3 bounded autonomous repair of inherited stable failures (spec 090, US3).

    When enabled, the implementer may repair INHERITED stable failures on the
    card's existing branch into its open PR, gated by the dual test-integrity
    guard and a per-head attempt budget; never auto-merged. Default-off → the
    repair loop is dormant; ``max_repair_attempts_per_head`` defaults to a
    single attempt (0 = repair disabled even when ``enabled``). (SC-006)
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    max_repair_attempts_per_head: int = Field(default=1, ge=0, le=20)


class EnvSignaturePattern(BaseModel):
    """095: one infrastructure/environment failure signature (operator-defined).

    Matched (case-insensitive regex) against a failing check's normalized reason.
    A match labels the failure ENV_BLOCKED — surfaced with ``cause`` + ``action``,
    never autonomously repaired or bounced.
    """

    model_config = ConfigDict(extra="forbid")

    id: str
    regex: str
    cause: str
    action: str


class EnvBlockedGateConfig(BaseModel):
    """095: infrastructure/environment CI-failure classification (ENV_BLOCKED).

    When enabled, a failing required check whose normalized reason matches a
    built-in or operator-supplied infra pattern is held (not repaired, not
    re-dispatched) and surfaced to the operator. ``patterns`` are matched in
    addition to the shipped built-ins (artifact-storage quota / runner-offline /
    billing-limit). Default-off → behavior identical to the pre-feature baseline.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    patterns: list[EnvSignaturePattern] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# 074 — Persona Scope Tiering
# ---------------------------------------------------------------------------


class PersonaScopeConfig(BaseModel):
    """Symphony-level config for persona scope tiering (spec 074).

    Opt-in: feature is dormant unless `enabled=True` AND `path_classes` is non-empty.
    Coordinare ships no path-class defaults — projects own their taxonomy (FR-004).
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    path_classes: dict[str, list[str]] = Field(default_factory=dict)
    forced_full_on_path_classes: dict[str, list[str]] = Field(default_factory=dict)
    classifier_latency_budget_seconds: float = Field(default=30.0, ge=1.0, le=600.0)
    classifier_failure_warning_cooldown_seconds: float = Field(default=600.0, ge=0.0)

    # 075 — Implementer CI gate (opt-in; default-off, see contracts/config-schema.md).
    persona_check_map: PersonaCheckMapConfig | None = None
    ci_gate: CIGateConfig = Field(default_factory=CIGateConfig)

    # 089 — Implementer local test gate (opt-in; default-off, see
    # contracts/local_test_gate_config.md).
    local_test_gate: LocalTestGateConfig = Field(default_factory=LocalTestGateConfig)

    # 090 — Baseline repair autonomy (three layers, all opt-in / default-off; see
    # specs/090-baseline-repair-autonomy/data-model.md §10, SC-006).
    baseline_prevention_gate: BaselinePreventionGateConfig = Field(
        default_factory=BaselinePreventionGateConfig
    )
    baseline_classification_gate: BaselineClassificationGateConfig = Field(
        default_factory=BaselineClassificationGateConfig
    )
    inherited_repair_gate: InheritedRepairGateConfig = Field(
        default_factory=InheritedRepairGateConfig
    )
    # 095 — Infrastructure/environment CI-failure classification (ENV_BLOCKED;
    # opt-in / default-off). See specs/095-env-blocked-ci/.
    env_blocked_gate: EnvBlockedGateConfig = Field(
        default_factory=EnvBlockedGateConfig
    )

    @field_validator("path_classes")
    @classmethod
    def _validate_path_classes(cls, v: dict[str, list[str]]) -> dict[str, list[str]]:
        cleaned: dict[str, list[str]] = {}
        for name, globs in v.items():
            stripped_name = name.strip()
            if not stripped_name:
                msg = "path_classes contains an empty class name"
                raise ValueError(msg)
            if not globs:
                msg = f"path_classes[{stripped_name!r}] has an empty glob list"
                raise ValueError(msg)
            cleaned_globs: list[str] = []
            for g in globs:
                if not isinstance(g, str):
                    msg = f"path_classes[{stripped_name!r}] glob must be a string, got {type(g).__name__}"
                    raise ValueError(msg)
                stripped_glob = g.strip()
                if not stripped_glob:
                    msg = f"path_classes[{stripped_name!r}] contains an empty/whitespace glob"
                    raise ValueError(msg)
                cleaned_globs.append(stripped_glob)
            cleaned[stripped_name] = cleaned_globs
        return cleaned

    @model_validator(mode="after")
    def _classes_in_forced_full_must_exist(self) -> PersonaScopeConfig:
        defined = set(self.path_classes.keys())
        for persona, classes in self.forced_full_on_path_classes.items():
            unknown = set(classes) - defined
            if unknown:
                msg = (
                    f"forced_full_on_path_classes[{persona!r}] references undefined "
                    f"classes: {sorted(unknown)}"
                )
                raise ValueError(msg)
        return self


# ---------------------------------------------------------------------------
# 092 — Symphony test-environment injection
# ---------------------------------------------------------------------------


class TestEnvConfig(BaseModel):
    """Symphony test-environment file (spec 092).

    Names a dotenv-style file whose ``KEY=VALUE`` pairs coordinare injects into
    every container that runs project code (the start-phase validation dry-run,
    the env-cache QA runtime, and code-running performers). Exactly one source
    must be supplied:

    - ``repo_path``: resolved INSIDE the cloned symphony repo (containment-checked).
    - ``host_path``: an absolute host path, read directly.

    The file carries genuine test credentials; loaded values are treated as
    secret-like (routed through the redacted secrets channel — never logged or
    persisted as literals). See contracts/test_env_config.md.
    """

    model_config = ConfigDict(extra="forbid")

    # Not a pytest test class despite the ``Test`` prefix (silences collection warning).
    __test__ = False

    repo_path: str | None = None
    host_path: str | None = None

    @model_validator(mode="after")
    def _exactly_one_source(self) -> TestEnvConfig:
        if self.repo_path is not None and self.host_path is not None:
            msg = "test_env: set exactly one of repo_path / host_path, not both"
            raise ValueError(msg)
        if self.repo_path is None and self.host_path is None:
            msg = "empty test_env block; set repo_path or host_path, or omit it"
            raise ValueError(msg)
        if self.repo_path is not None:
            rp = self.repo_path
            if PurePosixPath(rp).is_absolute() or Path(rp).is_absolute():
                msg = f"test_env.repo_path must be repo-relative, got absolute: {rp!r}"
                raise ValueError(msg)
            if ".." in PurePosixPath(rp).parts:
                msg = f"test_env.repo_path escapes the clone (contains '..'): {rp!r}"
                raise ValueError(msg)
        return self


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

    # 064 — Closer PR-checks gate per symphony.
    closer_pr_checks: CloserPrChecksConfig = Field(default_factory=lambda: CloserPrChecksConfig())

    # 074 — Persona scope tiering per symphony (opt-in; FR-010 additive default).
    persona_scope: PersonaScopeConfig | None = None

    # 092 — Symphony test-environment injection (opt-in; None = agent-discovery fallback).
    test_env: TestEnvConfig | None = None

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


class DispatcherDedupConfig(BaseModel):
    """Dispatcher deduplication / reconciliation tunables (spec 076).

    Controls the in-flight guard, startup reconciliation pass, idle-timeout
    retry budget, wedge-invariant promotion, and multi-PR detection cadence
    introduced by spec 076.  All defaults reflect the clarification answers
    recorded in ``specs/076-qa-cycle/spec.md`` (Q1-Q5).
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    reconciliation_budget_seconds: float = Field(default=30.0, gt=0.0, le=600.0)
    drain_budget_seconds: float = Field(default=5.0, ge=0.0, le=60.0)
    reap_budget_seconds: float = Field(default=5.0, ge=0.0, le=60.0)
    idle_timeout_retries: int = Field(default=2, ge=0, le=20)
    idle_timeout_window_hours: int = Field(default=24, ge=1, le=168)
    # 077 stall watchdog: a performer turn that is still "working" but has made
    # NO forward progress (no new events, no token growth) for this many seconds
    # is treated as wedged (e.g. a hung upstream model read the backend reports as
    # "busy") and killed + retried via the idle-timeout budget. The backend's own
    # idle_timeout and the opt-in role_timeouts both miss this case. 0 disables it
    # (default — opt-in per deployment); set conservatively above the slowest
    # legitimate no-output gap so working turns are never falsely tripped.
    stall_timeout_seconds: int = Field(default=0, ge=0, le=7200)
    # T171: empty-output (e.g. empty architecture plan) is usually a
    # deterministic model-capability failure on a given card, so the
    # default budget is low (1 retry, then BLOCK for a human) — bounding
    # the previous infinite block→requeue churn loop.
    empty_output_retries: int = Field(default=1, ge=0, le=20)
    empty_output_window_hours: int = Field(default=24, ge=1, le=168)
    wedge_block_threshold: int = Field(default=3, ge=1, le=100)
    wedge_block_window_hours: int = Field(default=24, ge=1, le=168)
    multi_pr_check_triggers: list[Literal["dispatch", "restart", "webhook"]] = Field(
        default_factory=lambda: ["dispatch", "restart", "webhook"]
    )

    @field_validator("multi_pr_check_triggers")
    @classmethod
    def _validate_triggers_non_empty(
        cls, v: list[str]
    ) -> list[str]:
        if not v:
            msg = "multi_pr_check_triggers must contain at least one of: dispatch, restart, webhook"
            raise ValueError(msg)
        return v


class CoordinareConfiguration(BaseModel):
    """Root configuration combining global defaults + multiple symphonies + orchestra."""

    model_config = ConfigDict(extra="forbid")

    global_config: ProjectConfiguration
    symphonies: list[SymphonyConfig]
    orchestra: OrchestraConfig = Field(default_factory=OrchestraConfig)
    # 076 — Dispatcher dedup + reconciliation tunables (top-level so all
    # symphonies share the same dispatcher invariants).
    dispatcher_dedup: DispatcherDedupConfig = Field(default_factory=DispatcherDedupConfig)

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


# ---------------------------------------------------------------------------
# Persona-scope startup validation (spec 074)
# ---------------------------------------------------------------------------

_PERSONA_SCOPE_CLASSIFIER_NAMES: tuple[str, ...] = (
    "reviewer",
    "security",
    "qa",
    "tech_writer",
    "closer",
)


def validate_persona_scope_config(
    persona_scope: PersonaScopeConfig | None,
    personas: PersonasConfig | None,
) -> list[tuple[str, str]]:
    """Emit startup warnings for persona-scope config edge cases (FR-009, FR-010).

    Returns a list of (level, message) tuples. Caller is responsible for logging
    via the project's logger. Three scenarios per contracts/config-schema.md:

      - enabled=True with empty path_classes  → warning (classifier will run
        but no path memberships will resolve; effectively shadow-only).
      - any persona has scope_behavior but persona_scope is missing/disabled
        → info ("scope_behavior configured but feature is off").
      - closer.scope_behavior is set → warning (FR-009: closer is
        scope-invariant; the block is ignored).
    """
    findings: list[tuple[str, str]] = []
    scope_enabled = persona_scope is not None and persona_scope.enabled

    if scope_enabled and persona_scope is not None and not persona_scope.path_classes:
        findings.append((
            "warning",
            "persona_scope.enabled=true but path_classes is empty; classifier will "
            "run with no path-class taxonomy (no per-persona tier overrides will "
            "fire). Define path_classes or set enabled=false.",
        ))

    if personas is not None:
        configured: list[str] = []
        for name in _PERSONA_SCOPE_CLASSIFIER_NAMES:
            pc = getattr(personas, name, None)
            if pc is not None and getattr(pc, "scope_behavior", None) is not None:
                configured.append(name)
        if configured and not scope_enabled:
            findings.append((
                "info",
                "scope_behavior configured for personas "
                f"{configured} but persona_scope is disabled or missing; "
                "these blocks have no effect until persona_scope.enabled=true.",
            ))

        closer_pc = getattr(personas, "closer", None)
        if closer_pc is not None and getattr(closer_pc, "scope_behavior", None) is not None:
            findings.append((
                "warning",
                "personas.closer.scope_behavior is set but closer is "
                "scope-invariant (FR-009); the configured tier overrides will be "
                "ignored. Remove the block to silence this warning.",
            ))

    # 077 — persona_check_map depth lists need 074 tiering to supply a depth.
    # When tiering is off, only the depth-agnostic `any` list applies. Warn if an
    # operator scoped via depth keys alone without enabling tiering.
    pcm = getattr(persona_scope, "persona_check_map", None) if persona_scope else None
    if pcm is not None and not scope_enabled:
        pcm_dict = pcm.model_dump() if hasattr(pcm, "model_dump") else dict(pcm)
        depth_only = [
            persona
            for persona, slots in (pcm_dict or {}).items()
            if isinstance(slots, dict)
            and not (slots.get("any") or [])
            and any(slots.get(d) for d in ("skim", "normal", "full"))
        ]
        if depth_only:
            findings.append((
                "info",
                "persona_check_map depth lists "
                f"{depth_only} are set but persona_scope.enabled=false, so no "
                "depth is supplied to the resolver; these lists will not apply. "
                "Use the depth-agnostic `any` list to scope the CI gate without "
                "enabling 074 tiering.",
            ))

    return findings

