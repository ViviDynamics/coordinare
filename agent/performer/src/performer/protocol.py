"""Wire protocol models for the performer.

Mirrors coordinare.protocol.ProtocolMessage / ProtocolResponse exactly,
with an additional optional ``metrics`` field on PerformerResponse.
The coordinare ignores the extra field (pydantic drops unknown fields on
deserialization) until it explicitly adopts it.
"""
from __future__ import annotations

from typing import Any, Literal

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
    "docs_committed",
    "assessment_complete",
    "healthy",
    "unhealthy",
]


class PerformerMetrics(BaseModel):
    """Best-effort runtime telemetry attached to ``working`` status responses."""

    pid: int | None = None
    child_pids: list[int] = Field(default_factory=list)
    memory_bytes: int | None = None
    cpu_percent: float | None = None
    tokens_processed: int | None = None


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
    suggestions: list[str] = Field(default_factory=list)  # 021: non-blocking suggestions
    findings: list[dict] = Field(default_factory=list)  # 022: security findings [{severity, category, ...}]
    failures: list[dict] = Field(default_factory=list)  # 023: QA failures [{criterion, expected, actual, test}]
    report: dict | None = None  # 023: QA pass report {criteria_checked, criteria_passed, new_tests_added}
    files_modified: list[str] = Field(default_factory=list)  # 024: doc files committed by tech writer
    metrics: PerformerMetrics | None = None
    events: list[dict] = Field(default_factory=list)  # serialised BackendEvent list
