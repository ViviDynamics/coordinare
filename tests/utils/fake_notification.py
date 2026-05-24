"""Fake notification service for testing."""
from __future__ import annotations

from coordinare.models.notification import (
    NotificationEvent,
    NotificationHistory,
)


class FakeNotificationService:
    """Records dispatched events for assertion in tests.

    Implements NotificationServiceProtocol.
    """

    def __init__(self, card_blocked_reminder_cooldown_seconds: int = 3600) -> None:
        self.dispatched: list[NotificationEvent] = []
        self._history = NotificationHistory(max_age_hours=24)
        self._card_blocked_reminder_cooldown_seconds = card_blocked_reminder_cooldown_seconds

    async def dispatch(self, event: NotificationEvent) -> None:
        self.dispatched.append(event)

    @property
    def history(self) -> NotificationHistory:
        return self._history

    @property
    def card_blocked_reminder_cooldown_seconds(self) -> int:
        return self._card_blocked_reminder_cooldown_seconds
