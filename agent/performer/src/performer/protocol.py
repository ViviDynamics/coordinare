"""Wire protocol models for the performer.

Mirrors coordinare.protocol.ProtocolMessage / ProtocolResponse exactly,
with an additional optional ``metrics`` field on PerformerResponse.
The coordinare ignores the extra field (pydantic drops unknown fields on
deserialization) until it explicitly adopts it.
"""
from __future__ import annotations

from typing import Any, Literal, get_args

from pydantic import BaseModel, Field

ActionType = Literal["dispatch", "status", "relay_feedback", "health"]

# Must stay in sync with coordinare.protocol.StatusType.
# "healthy"/"unhealthy" are included in both coordinare and performer for health-check responses.
PerformerStatusType = Literal[
    "accepted",
    "working",
    "pr_opened",
    "blocked",
    "error",
    "unknown",
    "busy",
    "acknowledged",
    "session_expired",
    "token_limit",
    "plan_committed",
    "approved",
    "changes_requested",
    "security_passed",
    "security_failed",
    "qa_passed",
    "qa_failed",
    "qa_env_blocked",
    # 089: implementer local-test gate hit an environment failure (services
    # failed to start / env-cache unhealthy / timeout w/ env signal). Terminal
    # non-success — coordinare holds the card and repairs the environment.
    "env_blocked",
    "docs_committed",
    "env_bootstrap_complete",
    "assessment_complete",
    "diagnostic_complete",
    "partial_progress",
    "healthy",
    "unhealthy",
]

# Non-terminal statuses — session is still in progress.
_NON_TERMINAL_STATUSES: frozenset[str] = frozenset({
    "accepted", "working", "busy", "acknowledged", "unknown", "healthy", "unhealthy",
})

# Derived from the Literal above so new terminal statuses are picked up automatically.
TERMINAL_STATUSES: frozenset[str] = frozenset(get_args(PerformerStatusType)) - _NON_TERMINAL_STATUSES

# Failure states within the terminal set — used to determine job success.
FAILURE_STATUSES: frozenset[str] = frozenset({
    "error",
    "session_expired",
    "token_limit",
    "blocked",
    "changes_requested",
    "security_failed",
    "qa_failed",
    # 088: env-blocked QA is terminal non-success — the job did not verify
    # anything; coordinare holds the card and repairs the environment.
    "qa_env_blocked",
    # 089: env-blocked implementer local-test gate — same handling as
    # qa_env_blocked (hold + repair), not a code defect.
    "env_blocked",
})


class PerformerMetrics(BaseModel):
    """Best-effort runtime telemetry attached to ``working`` status responses."""

    model_config = {"extra": "ignore"}

    pid: int | None = None
    child_pids: list[int] = Field(default_factory=list)
    memory_bytes: int | None = None
    cpu_percent: float | None = None
    tokens_processed: int | None = None
    # 067: non-2xx upstream response envelope. Serialized as a dict on the wire
    # so older coordinares (pydantic extra="ignore") drop it cleanly. The
    # authoritative model lives in src/coordinare/upstream_errors.py and the
    # contract at specs/067-compatibility-first-backend/contracts/upstream_http_error.md.
    upstream_http_error: dict | None = None


class PerformerMessage(BaseModel):
    """Inbound wire protocol message from the coordinare."""

    action: ActionType
    session_id: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)


class PerformerResponse(BaseModel):
    """Outbound wire protocol response from the performer."""

    status: PerformerStatusType
    session_id: str = ""
    reason: str | None = None
    questions: list[str] = Field(default_factory=list)
    pr_url: str | None = None
    pr_node_id: str | None = None
    progress: str | None = None
    backend: str | None = None  # AGENT_BACKEND name returned on dispatch
    model: str | None = None  # model override returned on dispatch (if configured)
    plan_path: str | None = None  # 020: path to committed architecture plan
    comments: list[dict] = Field(default_factory=list)  # 021: review comments [{file, line, body}]
    body: str | None = None  # 065 Fix 4b: reviewer/closer prose body forwarded on changes_requested
    suggestions: list[str] = Field(default_factory=list)  # 021: non-blocking suggestions
    findings: list[dict] = Field(default_factory=list)  # 022: security findings [{severity, category, ...}]
    failures: list[dict] = Field(default_factory=list)  # 023: QA failures [{criterion, expected, actual, test}]
    report: dict | None = None  # 023: QA pass report {criteria_checked, criteria_passed, new_tests_added}
    files_modified: list[str] = Field(default_factory=list)  # 024: doc files committed by tech writer
    metrics: PerformerMetrics | None = None
    events: list[dict] = Field(default_factory=list)  # serialised BackendEvent list
    # 063 Phase 4 (T023): True when services-health.sh exited non-zero during
    # workspace setup. Coordinare uses this to force env-cache regeneration.
    env_cache_health_failed: bool = False
    # 089: True when the implementer local-test gate ran the detected
    # test_command and it failed for a code reason (not env). Coordinare uses
    # this on changes_requested to drive the bounded self-fix loop (T023).
    local_test_failed: bool = False
    # 063 Cross-cutting (T026c/T026d): service-inference outcome from the
    # env_bootstrap performer. ``inference_skipped_reason`` is set when the
    # agent did not run (no env_cache_path, coordinare package missing,
    # manual-override applied, no API key, etc.); fields are otherwise the
    # summary of the most recent infer_services call.
    inference_skipped_reason: str | None = None
    inference_agent_version: str | None = None
    inference_attempts: int | None = None
    inference_succeeded: bool | None = None
    inference_services: list[str] = Field(default_factory=list)
    # 092 US2: agent-discovered test-env source PATH (never literal values).
    # Coordinare persists this alongside the env-cache and reloads the same file
    # for later QA-runtime and performer contexts when no test_env is configured.
    inference_test_env_source: str | None = None
    # 070: branch HEAD before/after the performer session. Set on terminal
    # statuses for implementer-style stages so the coordinare can detect
    # zero-commit turns (head_before == head_after) and route them to retry
    # rather than honoring a no-progress "blocked" verdict.
    head_before: str | None = None
    head_after: str | None = None
    # 070: continuation hint emitted alongside status="partial_progress" so
    # the coordinare relays a focused next-chunk prompt to the next turn.
    next_focus: str | None = None
    # 072: count of new PR comments authored by the bot user during this
    # turn. Used by the coordinare per-role zero-progress guardrail to
    # distinguish reviewer/qa turns that surfaced something real from
    # silent no-op turns.
    bot_pr_comment_delta: int = 0
