from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field, model_validator


class ReviewerType(StrEnum):
    HUMAN = "HUMAN"
    BOT = "BOT"


class ReviewState(StrEnum):
    APPROVED = "APPROVED"
    CHANGES_REQUESTED = "CHANGES_REQUESTED"
    COMMENTED = "COMMENTED"
    DISMISSED = "DISMISSED"


class Review(BaseModel):
    id: str
    author_login: str
    author_type: ReviewerType
    state: ReviewState
    body: str = ""
    submitted_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    is_actionable: bool = False

    @model_validator(mode="after")
    def _set_actionable(self) -> Review:
        self.is_actionable = self.author_type == ReviewerType.HUMAN
        return self


def classify_reviewer(author_login: str, human_reviewers: list[str]) -> ReviewerType:
    normalized = {login.strip().lower() for login in human_reviewers}
    if author_login.strip().lower() in normalized:
        return ReviewerType.HUMAN
    return ReviewerType.BOT
