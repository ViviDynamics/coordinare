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
    "blocked",
    "error",
    "unknown",
    "busy",
    "acknowledged",
    "session_expired",
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
