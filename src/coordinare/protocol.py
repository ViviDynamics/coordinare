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
    "docs_committed",
    "assessment_complete",
    "blocked",
    "error",
    "unknown",
    "busy",
    "acknowledged",
    "session_expired",
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
