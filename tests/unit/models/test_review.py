from __future__ import annotations

from coordinare.models.review import Review, ReviewerType, ReviewState, classify_reviewer


def test_classify_reviewer_human_and_bot() -> None:
    assert classify_reviewer("alice", ["alice", "bob"]) is ReviewerType.HUMAN
    assert classify_reviewer("copilot[bot]", ["alice", "bob"]) is ReviewerType.BOT


def test_classify_reviewer_trusted_bot() -> None:
    result = classify_reviewer(
        "copilot-pull-request-reviewer[bot]",
        ["alice"],
        trusted_bot_reviewers=["copilot-pull-request-reviewer[bot]"],
    )
    assert result is ReviewerType.TRUSTED_BOT


def test_classify_reviewer_trusted_bot_case_insensitive() -> None:
    result = classify_reviewer(
        "Copilot-Pull-Request-Reviewer[bot]",
        ["alice"],
        trusted_bot_reviewers=["copilot-pull-request-reviewer[bot]"],
    )
    assert result is ReviewerType.TRUSTED_BOT


def test_classify_reviewer_untrusted_bot() -> None:
    result = classify_reviewer(
        "some-other-bot[bot]",
        ["alice"],
        trusted_bot_reviewers=["copilot-pull-request-reviewer[bot]"],
    )
    assert result is ReviewerType.BOT


def test_review_sets_actionable_for_humans() -> None:
    review = Review(
        id="R1",
        author_login="alice",
        author_type=ReviewerType.HUMAN,
        state=ReviewState.COMMENTED,
    )
    assert review.is_actionable is True


def test_review_sets_actionable_for_trusted_bots() -> None:
    review = Review(
        id="R2",
        author_login="copilot[bot]",
        author_type=ReviewerType.TRUSTED_BOT,
        state=ReviewState.CHANGES_REQUESTED,
    )
    assert review.is_actionable is True


def test_review_not_actionable_for_untrusted_bots() -> None:
    review = Review(
        id="R3",
        author_login="random-bot",
        author_type=ReviewerType.BOT,
        state=ReviewState.COMMENTED,
    )
    assert review.is_actionable is False
