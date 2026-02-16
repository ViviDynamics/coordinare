from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field, model_validator


class CardStatus(StrEnum):
    TODO = "TODO"
    BLOCKED = "BLOCKED"
    IN_PROGRESS = "IN_PROGRESS"
    IN_REVIEW = "IN_REVIEW"
    DONE = "DONE"


class CardTransition(BaseModel):
    from_status: CardStatus
    to_status: CardStatus
    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    reason: str | None = None


class Card(BaseModel):
    id: str
    issue_id: str
    issue_number: int
    title: str
    description: str

    acceptance_criteria: list[str] = Field(default_factory=list)
    status: CardStatus
    previous_status: CardStatus | None = None

    assigned_agent: str | None = None
    pr_url: str | None = None
    pr_node_id: str | None = None

    open_questions: list[str] = Field(default_factory=list)
    transition_history: list[CardTransition] = Field(default_factory=list)

    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def _validate_fields(self) -> Card:
        if not self.title.strip():
            msg = "title must be non-empty"
            raise ValueError(msg)
        if self.status == CardStatus.IN_REVIEW and not self.pr_url:
            msg = "pr_url is required when status is IN_REVIEW"
            raise ValueError(msg)
        if self.status == CardStatus.BLOCKED and not self.open_questions:
            msg = "open_questions are required when status is BLOCKED"
            raise ValueError(msg)
        return self

    def record_transition(self, to_status: CardStatus, reason: str | None = None) -> None:
        transition = CardTransition(from_status=self.status, to_status=to_status, reason=reason)
        self.transition_history.append(transition)
        self.previous_status = self.status
        self.status = to_status
        self.updated_at = transition.timestamp
