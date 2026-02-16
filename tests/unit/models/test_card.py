from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from coordinare.models.card import Card, CardStatus


def _base_card_data() -> dict[str, object]:
    now = datetime.now(UTC)
    return {
        "id": "PVI_1",
        "issue_id": "I_1",
        "issue_number": 42,
        "title": "Implement endpoint",
        "description": "Build the new endpoint.",
        "status": CardStatus.TODO,
        "created_at": now,
        "updated_at": now,
    }


def test_card_creation() -> None:
    card = Card(**_base_card_data())
    assert card.issue_number == 42
    assert card.status is CardStatus.TODO


def test_card_status_enum_values() -> None:
    assert CardStatus.TODO.value == "TODO"
    assert CardStatus.BLOCKED.value == "BLOCKED"
    assert CardStatus.IN_PROGRESS.value == "IN_PROGRESS"
    assert CardStatus.IN_REVIEW.value == "IN_REVIEW"
    assert CardStatus.DONE.value == "DONE"


def test_title_must_be_non_empty() -> None:
    data = _base_card_data()
    data["title"] = "   "
    with pytest.raises(ValidationError):
        Card(**data)


def test_in_review_requires_pr_url() -> None:
    data = _base_card_data()
    data["status"] = CardStatus.IN_REVIEW
    with pytest.raises(ValidationError):
        Card(**data)


def test_blocked_requires_open_questions() -> None:
    data = _base_card_data()
    data["status"] = CardStatus.BLOCKED
    with pytest.raises(ValidationError):
        Card(**data)


def test_card_transition_recording() -> None:
    card = Card(**_base_card_data())
    card.record_transition(CardStatus.IN_PROGRESS, reason="Dispatching to agent")

    assert card.previous_status is CardStatus.TODO
    assert card.status is CardStatus.IN_PROGRESS
    assert len(card.transition_history) == 1
    assert card.transition_history[0].reason == "Dispatching to agent"
