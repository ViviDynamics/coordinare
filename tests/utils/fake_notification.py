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

    def __init__(self) -> None:
        self.dispatched: list[NotificationEvent] = []
        self._history = NotificationHistory(max_age_hours=24)

    async def dispatch(self, event: NotificationEvent) -> None:
        self.dispatched.append(event)

    @property
    def history(self) -> NotificationHistory:
        return self._history
