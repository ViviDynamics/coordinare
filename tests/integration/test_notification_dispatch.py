"""Integration test for full notification dispatch pipeline (T030).

Wires a complete NotificationService with real routing, rate limiter, dedup,
history, and metrics using a FakeChannelSender. Validates the entire pipeline
end-to-end in dispatch calls.
"""
from __future__ import annotations

import pytest

from coordinare.config import ChannelConfig, NotificationsConfig, RoutingEntry
from coordinare.metrics import CoordinareMetrics
from coordinare.models.notification import (
    EventType,
    NotificationEvent,
    NotificationSeverity,
    NotificationStatus,
)
from coordinare.services.notification import NotificationService


class FakeChannelSender:
    def __init__(self, name: str, *, fail_first_n: int = 0) -> None:
        self._name = name
        self._fail_count = 0
        self._fail_first_n = fail_first_n
        self.sent: list[str] = []

    @property
    def channel_name(self) -> str:
        return self._name

    async def send(self, message: str, *, subject: str | None = None) -> None:
        if self._fail_count < self._fail_first_n:
            self._fail_count += 1
            raise ConnectionError(f"Fake failure {self._fail_count}")
        self.sent.append(message)


def _make_event(
    event_type: EventType = EventType.card_transition,
    dedup_key: str | None = None,
) -> NotificationEvent:
    return NotificationEvent(
        event_type=event_type,
        severity=NotificationSeverity.info,
        payload={
            "event_type": event_type.value,
            "severity": "info",
            "source": "test",
            "summary": "integration test event",
        },
        source="test",
        dedup_key=dedup_key,
    )


@pytest.mark.asyncio
async def test_retry_and_history() -> None:
    """Retry produces correct retries_attempted count in history."""
    config = NotificationsConfig(
        channels=[
            ChannelConfig(
                name="slack-ops",
                type="slack",
                webhook_url="https://hooks.slack.com/services/T/B/C",
                retry_count=3,
                retry_delay_seconds=0.0,
                message_template="{event_type}: {summary}",
            ),
        ],
        routing=[
            RoutingEntry(event_type=EventType.card_transition, channels=["slack-ops"]),
        ],
    )
    sender = FakeChannelSender("slack-ops", fail_first_n=2)
    svc = NotificationService(config=config, senders=[sender], metrics=CoordinareMetrics())

    await svc.dispatch(_make_event())

    delivered = svc.history.query(status=NotificationStatus.delivered)
    assert len(delivered) == 1
    assert delivered[0].retries_attempted == 2


@pytest.mark.asyncio
async def test_rate_limiting() -> None:
    """Rate limiter drops excess events."""
    config = NotificationsConfig(
        channels=[
            ChannelConfig(
                name="slack-ops",
                type="slack",
                webhook_url="https://hooks.slack.com/services/T/B/C",
                rate_limit=2,
                rate_window_seconds=60,
                message_template="{event_type}: {summary}",
            ),
        ],
        routing=[
            RoutingEntry(event_type=EventType.card_transition, channels=["slack-ops"]),
        ],
    )
    sender = FakeChannelSender("slack-ops")
    svc = NotificationService(config=config, senders=[sender], metrics=CoordinareMetrics())

    for _ in range(5):
        await svc.dispatch(_make_event())

    assert len(sender.sent) == 2
    rate_limited = svc.history.query(status=NotificationStatus.rate_limited)
    assert len(rate_limited) == 3


@pytest.mark.asyncio
async def test_dedup_suppresses_duplicate() -> None:
    """Dedup suppresses second event within window."""
    config = NotificationsConfig(
        channels=[
            ChannelConfig(
                name="slack-ops",
                type="slack",
                webhook_url="https://hooks.slack.com/services/T/B/C",
                dedup_window_seconds=600,
                message_template="{event_type}: {summary}",
            ),
        ],
        routing=[
            RoutingEntry(event_type=EventType.card_blocked, channels=["slack-ops"]),
        ],
    )
    sender = FakeChannelSender("slack-ops")
    svc = NotificationService(config=config, senders=[sender], metrics=CoordinareMetrics())

    await svc.dispatch(_make_event(event_type=EventType.card_blocked, dedup_key="issue-42"))
    await svc.dispatch(_make_event(event_type=EventType.card_blocked, dedup_key="issue-42"))

    assert len(sender.sent) == 1
    deduplicated = svc.history.query(status=NotificationStatus.deduplicated)
    assert len(deduplicated) == 1


@pytest.mark.asyncio
async def test_unrouted_event() -> None:
    """Unrouted events are recorded in history."""
    config = NotificationsConfig(
        channels=[
            ChannelConfig(
                name="slack-ops",
                type="slack",
                webhook_url="https://hooks.slack.com/services/T/B/C",
                message_template="{event_type}: {summary}",
            ),
        ],
        routing=[
            RoutingEntry(event_type=EventType.card_transition, channels=["slack-ops"]),
        ],
    )
    sender = FakeChannelSender("slack-ops")
    svc = NotificationService(config=config, senders=[sender], metrics=CoordinareMetrics())

    await svc.dispatch(_make_event(event_type=EventType.daemon_restart))

    unrouted = svc.history.query(status=NotificationStatus.unrouted)
    assert len(unrouted) == 1
    assert len(sender.sent) == 0


@pytest.mark.asyncio
async def test_metrics_incremented_on_dispatch() -> None:
    """dispatched_total counter increments on successful delivery."""
    config = NotificationsConfig(
        channels=[
            ChannelConfig(
                name="slack-ops",
                type="slack",
                webhook_url="https://hooks.slack.com/services/T/B/C",
                message_template="{event_type}: {summary}",
            ),
        ],
        routing=[
            RoutingEntry(event_type=EventType.card_transition, channels=["slack-ops"]),
        ],
    )
    sender = FakeChannelSender("slack-ops")
    metrics = CoordinareMetrics()
    svc = NotificationService(config=config, senders=[sender], metrics=metrics)

    await svc.dispatch(_make_event())

    dispatched_value = metrics.notifications_dispatched_total.labels(
        event_type="card_transition", channel_name="slack-ops",
    )._value.get()
    assert dispatched_value == 1.0


@pytest.mark.asyncio
async def test_all_outcome_statuses_in_history() -> None:
    """Verify all outcome statuses can be recorded in history."""
    config = NotificationsConfig(
        channels=[
            ChannelConfig(
                name="slack-ops",
                type="slack",
                webhook_url="https://hooks.slack.com/services/T/B/C",
                rate_limit=1,
                rate_window_seconds=60,
                dedup_window_seconds=600,
                retry_count=1,
                retry_delay_seconds=0.0,
                message_template="{event_type}: {summary}",
            ),
        ],
        routing=[
            RoutingEntry(event_type=EventType.card_transition, channels=["slack-ops"]),
            RoutingEntry(event_type=EventType.card_blocked, channels=["slack-ops"]),
        ],
    )
    # Sender always fails → triggers failed status
    always_fail = FakeChannelSender("slack-ops", fail_first_n=999)
    svc = NotificationService(config=config, senders=[always_fail], metrics=CoordinareMetrics())

    # 1. Failed (sender raises, retry exhausted)
    await svc.dispatch(_make_event())
    # 2. Rate limited (limit is 1, already used)
    await svc.dispatch(_make_event())
    # 3. Unrouted (daemon_restart not in routing)
    await svc.dispatch(_make_event(event_type=EventType.daemon_restart))

    all_records = svc.history.query()
    statuses = {r.status for r in all_records}
    assert NotificationStatus.failed in statuses
    assert NotificationStatus.rate_limited in statuses
    assert NotificationStatus.unrouted in statuses
