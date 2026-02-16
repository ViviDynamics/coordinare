from __future__ import annotations

from coordinare.models.review import Review, ReviewerType, ReviewState, classify_reviewer


def test_classify_reviewer_human_and_bot() -> None:
    assert classify_reviewer("alice", ["alice", "bob"]) is ReviewerType.HUMAN
    assert classify_reviewer("copilot[bot]", ["alice", "bob"]) is ReviewerType.BOT


def test_review_sets_actionable_for_humans() -> None:
    review = Review(
        id="R1",
        author_login="alice",
        author_type=ReviewerType.HUMAN,
        state=ReviewState.COMMENTED,
    )

    assert review.is_actionable is True
