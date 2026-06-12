from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from pathlib import Path

from pydantic import BaseModel, Field

ActionType = Literal["dispatch", "status", "relay_feedback", "health"]

StatusType = Literal[
    "accepted",
    "working",
    "pr_opened",
    "plan_committed",
    "approved",
    "changes_requested",
    "security_passed",
    "security_failed",
    "qa_passed",
    "qa_failed",
    "qa_env_blocked",
    "docs_committed",
    "env_bootstrap_complete",
    "assessment_complete",
    "partial_progress",
    "blocked",
    "error",
    "unknown",
    "busy",
    "acknowledged",
    "session_expired",
    "token_limit",
    "healthy",
    "unhealthy",
]


class ProtocolMessage(BaseModel):
    action: ActionType
    session_id: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)


class ProtocolResponse(BaseModel):
    status: StatusType
    session_id: str = ""
    reason: str | None = None
    questions: list[str] = Field(default_factory=list)
    pr_url: str | None = None
    pr_node_id: str | None = None
    progress: str | None = None
    # Telemetry fields — populated by performer on "working" status responses.
    # Must be kept in sync with performer.protocol.PerformerResponse.
    backend: str | None = None  # AGENT_BACKEND name, returned on dispatch
    model: str | None = None  # model override, returned on dispatch if set
    plan_path: str | None = None  # 020: path to committed architecture plan
    comments: list[dict] = Field(default_factory=list)  # 021: review comments [{file, line, body}]
    suggestions: list[str] = Field(default_factory=list)  # 021: non-blocking suggestions
    findings: list[dict] = Field(default_factory=list)  # 022: security findings
    failures: list[dict] = Field(default_factory=list)  # 023: QA failures
    report: dict | None = None  # 023: QA pass report
    files_modified: list[str] = Field(default_factory=list)  # 024: doc files committed
    events: list[dict] = Field(default_factory=list)  # serialised BackendEvent list
    metrics: dict | None = None  # PerformerMetrics (pid, memory_bytes, cpu_percent, …)
    # 063 Phase 4 (T023): performer sets True when services-health.sh exited
    # non-zero during workspace setup. Coordinare's daemon glue routes this
    # into EnvCacheService.mark_runtime_health_failed for forced regen.
    env_cache_health_failed: bool = False
    # 063 Cross-cutting (T026c/T026d): performer-reported service-inference
    # outcome for env_bootstrap jobs. EnvCacheService stamps these onto
    # EnvCacheState so the dashboard can surface what the agent produced.
    inference_skipped_reason: str | None = None
    inference_agent_version: str | None = None
    inference_attempts: int | None = None
    inference_succeeded: bool | None = None
    inference_services: list[str] = Field(default_factory=list)
    # 070: branch HEAD before/after a performer turn; used by the coordinare
    # router to detect implementer turns that produced zero new commits and
    # route them back to dispatching rather than honoring a "blocked" verdict.
    head_before: str | None = None
    head_after: str | None = None
    # 070: continuation hint emitted with status="partial_progress".
    next_focus: str | None = None
    # 072: number of new PR comments authored by the bot user during this
    # turn. Used by the per-role zero-progress guardrail to distinguish a
    # reviewer that surfaced something real (delta > 0) from one that
    # silently churned (delta == 0).
    bot_pr_comment_delta: int = 0


def generate_contracts(output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)

    message_schema = ProtocolMessage.model_json_schema()
    response_schema = ProtocolResponse.model_json_schema()

    (output_dir / "protocol-message.schema.json").write_text(
        json.dumps(message_schema, indent=2) + "\n"
    )
    (output_dir / "protocol-response.schema.json").write_text(
        json.dumps(response_schema, indent=2) + "\n"
    )
