from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel, Field, model_validator


class ReviewerType(StrEnum):
    HUMAN = "HUMAN"
    TRUSTED_BOT = "TRUSTED_BOT"
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
        self.is_actionable = self.author_type in (ReviewerType.HUMAN, ReviewerType.TRUSTED_BOT)
        return self


def classify_reviewer(
    author_login: str,
    human_reviewers: list[str],
    trusted_bot_reviewers: list[str] | None = None,
) -> ReviewerType:
    normalized_humans = {login.strip().lower() for login in human_reviewers}
    if author_login.strip().lower() in normalized_humans:
        return ReviewerType.HUMAN
    if trusted_bot_reviewers:
        normalized_bots = {login.strip().lower() for login in trusted_bot_reviewers}
        if author_login.strip().lower() in normalized_bots:
            return ReviewerType.TRUSTED_BOT
    return ReviewerType.BOT
