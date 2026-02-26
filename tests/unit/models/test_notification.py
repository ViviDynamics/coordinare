from __future__ import annotations

from coordinare.models.card import CardStatus
from coordinare.models.notification import Notification


def test_notification_as_text_contains_transition() -> None:
    notification = Notification(
        card_title="Implement feature",
        previous_status=CardStatus.TODO,
        card_status=CardStatus.IN_PROGRESS,
        task_description="Build orchestration",
    )

    text = notification.as_text()

    assert "Transition:" in text
    assert "Implement feature" in text


def test_notification_as_text_includes_optional_fields() -> None:
    notification = Notification(
        card_title="Card",
        previous_status=CardStatus.TODO,
        card_status=CardStatus.BLOCKED,
        task_description="Task",
        open_questions=["What API?", "Which provider?"],
        commit_summary="abc1234 Fix bug",
        pr_url="https://github.com/org/repo/pull/42",
    )

    text = notification.as_text()

    assert "Open Questions:" in text
    assert "- What API?" in text
    assert "- Which provider?" in text
    assert "Commit Summary: abc1234 Fix bug" in text
    assert "PR: https://github.com/org/repo/pull/42" in text
