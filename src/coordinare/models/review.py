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


class StalenessClass(StrEnum):
    """128: how an outstanding CHANGES_REQUESTED verdict relates to the PR head.

    FRESH             — review sits on/near the current head (or its threads are
                        still open on head): genuinely-outstanding feedback →
                        existing behavior applies.
    STALE_ADDRESSED   — review commit is behind head past the threshold AND its
                        feedback looks addressed (all its threads resolved, or a
                        body-only review with new commits since) → re-request +
                        route IN_REVIEW.
    STALE_UNADDRESSED — review commit is behind head but threads remain open →
                        park + surface once (do not auto-advance).
    """

    FRESH = "FRESH"
    STALE_ADDRESSED = "STALE_ADDRESSED"
    STALE_UNADDRESSED = "STALE_UNADDRESSED"


class ReviewThread(BaseModel):
    """128: an inline PR review conversation. ``is_resolved`` is the per-item
    "addressed" signal; ``review_id`` attributes the thread to its review when
    GitHub exposes it."""

    id: str
    is_resolved: bool = False
    review_id: str | None = None


class Review(BaseModel):
    id: str
    author_login: str
    author_type: ReviewerType
    state: ReviewState
    body: str = ""
    submitted_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    is_actionable: bool = False
    # 128: the commit SHA this review was submitted against (from
    # PullRequestReview.commit.oid). Empty when GitHub omits it — staleness then
    # falls back to FRESH (never auto-advance on unknown provenance).
    commit_oid: str = ""

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
