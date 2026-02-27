from __future__ import annotations

from coordinare.models.notification import (
    EventType,
    NotificationEvent,
    NotificationSeverity,
)


def test_notification_event_fields() -> None:
    event = NotificationEvent(
        event_type=EventType.card_transition,
        severity=NotificationSeverity.info,
        payload={"summary": "test"},
        source="board",
    )
    assert event.event_type == EventType.card_transition
    assert event.severity == NotificationSeverity.info
    assert event.dedup_key is None


def test_notification_event_with_dedup_key() -> None:
    event = NotificationEvent(
        event_type=EventType.card_blocked,
        severity=NotificationSeverity.warning,
        payload={"summary": "blocked"},
        source="board",
        dedup_key="issue-42",
    )
    assert event.dedup_key == "issue-42"
