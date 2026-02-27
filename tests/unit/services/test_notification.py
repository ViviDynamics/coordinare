"""Unit tests for NotificationService (006-notification-alerting).

T015: Routing, retry, concurrent dispatch.
T025: SlidingWindowRateLimiter, DeduplicationWindow, rate_limited/deduplicated status.
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
from coordinare.services.notification import (
    DeduplicationWindow,
    NotificationService,
    SlidingWindowRateLimiter,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class FakeChannelSender:
    def __init__(self, name: str, *, fail_first_n: int = 0) -> None:
        self._name = name
        self._fail_count = 0
        self._fail_first_n = fail_first_n
        self.sent: list[str] = []
        self.subjects: list[str | None] = []

    @property
    def channel_name(self) -> str:
        return self._name

    async def send(self, message: str, *, subject: str | None = None) -> None:
        if self._fail_count < self._fail_first_n:
            self._fail_count += 1
            raise ConnectionError(f"Fake failure {self._fail_count}")
        self.sent.append(message)
        self.subjects.append(subject)


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
            "summary": "test event",
        },
        source="test",
        dedup_key=dedup_key,
    )


def _make_config(
    *,
    channel_name: str = "slack-ops",
    rate_limit: int = 0,
    rate_window_seconds: int = 60,
    dedup_window_seconds: int = 600,
    retry_count: int = 3,
    retry_delay_seconds: float = 0.0,
    routing: list[tuple[EventType, list[str]]] | None = None,
) -> NotificationsConfig:
    channels = [
        ChannelConfig(
            name=channel_name,
            type="slack",
            webhook_url="https://hooks.slack.com/services/T/B/C",
            rate_limit=rate_limit,
            rate_window_seconds=rate_window_seconds,
            dedup_window_seconds=dedup_window_seconds,
            retry_count=retry_count,
            retry_delay_seconds=retry_delay_seconds,
            message_template="{event_type}: {summary}",
        ),
    ]
    if routing is None:
        routing_entries = [
            RoutingEntry(event_type=EventType.card_transition, channels=[channel_name]),
        ]
    else:
        routing_entries = [
            RoutingEntry(event_type=et, channels=chs) for et, chs in routing
        ]
    return NotificationsConfig(channels=channels, routing=routing_entries)


def _build_service(
    config: NotificationsConfig,
    senders: list[FakeChannelSender],
    metrics: CoordinareMetrics | None = None,
) -> NotificationService:
    return NotificationService(
        config=config,
        senders=senders,
        metrics=metrics or CoordinareMetrics(),
    )


# ---------------------------------------------------------------------------
# T015: Routing tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_routing_to_matched_channel_calls_sender() -> None:
    config = _make_config()
    sender = FakeChannelSender("slack-ops")
    svc = _build_service(config, [sender])

    await svc.dispatch(_make_event())

    assert len(sender.sent) == 1


@pytest.mark.asyncio
async def test_unmatched_event_type_records_unrouted() -> None:
    config = _make_config()
    sender = FakeChannelSender("slack-ops")
    svc = _build_service(config, [sender])

    # card_blocked is NOT in the routing table
    await svc.dispatch(_make_event(event_type=EventType.card_blocked))

    assert len(sender.sent) == 0
    records = svc.history.query(status=NotificationStatus.unrouted)
    assert len(records) == 1


@pytest.mark.asyncio
async def test_retry_loop_calls_sender_up_to_retry_count() -> None:
    config = _make_config(retry_count=3, retry_delay_seconds=0.0)
    sender = FakeChannelSender("slack-ops", fail_first_n=2)
    svc = _build_service(config, [sender])

    await svc.dispatch(_make_event())

    # First 2 fail, third succeeds
    assert len(sender.sent) == 1
    records = svc.history.query(status=NotificationStatus.delivered)
    assert len(records) == 1
    assert records[0].retries_attempted == 2


@pytest.mark.asyncio
async def test_exception_swallowed_after_all_retries() -> None:
    config = _make_config(retry_count=2, retry_delay_seconds=0.0)
    sender = FakeChannelSender("slack-ops", fail_first_n=999)
    svc = _build_service(config, [sender])

    # Should not raise
    await svc.dispatch(_make_event())

    records = svc.history.query(status=NotificationStatus.failed)
    assert len(records) == 1
    assert records[0].retries_attempted == 2


@pytest.mark.asyncio
async def test_concurrent_dispatch_calls_all_matched_channels() -> None:
    channels = [
        ChannelConfig(
            name="slack-ops",
            type="slack",
            webhook_url="https://hooks.slack.com/services/T/B/C",
            message_template="{event_type}: {summary}",
        ),
        ChannelConfig(
            name="slack-alerts",
            type="slack",
            webhook_url="https://hooks.slack.com/services/T/B/D",
            message_template="{event_type}: {summary}",
        ),
    ]
    config = NotificationsConfig(
        channels=channels,
        routing=[
            RoutingEntry(
                event_type=EventType.card_transition,
                channels=["slack-ops", "slack-alerts"],
            ),
        ],
    )
    sender1 = FakeChannelSender("slack-ops")
    sender2 = FakeChannelSender("slack-alerts")
    svc = _build_service(config, [sender1, sender2])

    await svc.dispatch(_make_event())

    assert len(sender1.sent) == 1
    assert len(sender2.sent) == 1


@pytest.mark.asyncio
async def test_advocate_escalation_dispatches_without_enablement_key() -> None:
    """FR-014: advocate_escalation routes through routing table stateless."""
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
            RoutingEntry(
                event_type=EventType.advocate_escalation,
                channels=["slack-ops"],
            ),
        ],
    )
    sender = FakeChannelSender("slack-ops")
    svc = _build_service(config, [sender])

    await svc.dispatch(_make_event(event_type=EventType.advocate_escalation))

    assert len(sender.sent) == 1


# ---------------------------------------------------------------------------
# T025: SlidingWindowRateLimiter tests
# ---------------------------------------------------------------------------


def test_rate_limiter_allows_below_limit() -> None:
    rl = SlidingWindowRateLimiter(max_messages=3, window_seconds=60.0)
    assert rl.is_allowed() is True
    assert rl.is_allowed() is True
    assert rl.is_allowed() is True


def test_rate_limiter_drops_at_limit() -> None:
    rl = SlidingWindowRateLimiter(max_messages=2, window_seconds=60.0)
    assert rl.is_allowed() is True
    assert rl.is_allowed() is True
    assert rl.is_allowed() is False


def test_rate_limiter_unlimited_when_max_zero() -> None:
    rl = SlidingWindowRateLimiter(max_messages=0, window_seconds=60.0)
    for _ in range(100):
        assert rl.is_allowed() is True


# ---------------------------------------------------------------------------
# T025: DeduplicationWindow tests
# ---------------------------------------------------------------------------


def test_dedup_window_returns_false_first_call() -> None:
    dw = DeduplicationWindow(window_seconds=600.0)
    assert dw.is_duplicate("key1") is False


def test_dedup_window_returns_true_within_window() -> None:
    dw = DeduplicationWindow(window_seconds=600.0)
    dw.record("key1")
    assert dw.is_duplicate("key1") is True


def test_dedup_window_different_keys_are_independent() -> None:
    dw = DeduplicationWindow(window_seconds=600.0)
    dw.record("key1")
    assert dw.is_duplicate("key2") is False


# ---------------------------------------------------------------------------
# T025: Rate limiting and dedup integration in NotificationService
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rate_limited_events_recorded_in_history() -> None:
    config = _make_config(rate_limit=1, rate_window_seconds=60)
    sender = FakeChannelSender("slack-ops")
    svc = _build_service(config, [sender])

    await svc.dispatch(_make_event())
    await svc.dispatch(_make_event())

    assert len(sender.sent) == 1
    records = svc.history.query(status=NotificationStatus.rate_limited)
    assert len(records) == 1


@pytest.mark.asyncio
async def test_deduplicated_events_recorded_in_history() -> None:
    config = _make_config(dedup_window_seconds=600)
    sender = FakeChannelSender("slack-ops")
    svc = _build_service(config, [sender])

    await svc.dispatch(_make_event(dedup_key="dup-key"))
    await svc.dispatch(_make_event(dedup_key="dup-key"))

    assert len(sender.sent) == 1
    records = svc.history.query(status=NotificationStatus.deduplicated)
    assert len(records) == 1


# ---------------------------------------------------------------------------
# Subject template tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subject_template_rendered_and_passed_to_sender() -> None:
    channels = [
        ChannelConfig(
            name="slack-ops",
            type="slack",
            webhook_url="https://hooks.slack.com/services/T/B/C",
            message_template="{event_type}: {summary}",
            subject_template="[Coordinare] {event_type}",
        ),
    ]
    config = NotificationsConfig(
        channels=channels,
        routing=[
            RoutingEntry(event_type=EventType.card_transition, channels=["slack-ops"]),
        ],
    )
    sender = FakeChannelSender("slack-ops")
    svc = _build_service(config, [sender])

    await svc.dispatch(_make_event())

    assert len(sender.sent) == 1
    assert sender.subjects[0] == "[Coordinare] card_transition"


@pytest.mark.asyncio
async def test_no_subject_template_passes_none() -> None:
    config = _make_config()  # no subject_template
    sender = FakeChannelSender("slack-ops")
    svc = _build_service(config, [sender])

    await svc.dispatch(_make_event())

    assert sender.subjects[0] is None
