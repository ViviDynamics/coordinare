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
