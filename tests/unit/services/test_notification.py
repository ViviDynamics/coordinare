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


class YieldingSender:
    def __init__(self, name: str) -> None:
        self._name = name
        self.sent: list[str] = []

    @property
    def channel_name(self) -> str:
        return self._name

    async def send(self, message: str, *, subject: str | None = None) -> None:
        import asyncio

        await asyncio.sleep(0)
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


# ---------------------------------------------------------------------------
# 492: the unrouted path bypassed deduplication entirely
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unrouted_recurring_event_is_deduplicated_within_window() -> None:
    """Issue 492: a recurring event with no routed channel must not fire per cycle.

    The unrouted attempt was recorded before deduplication could run, so
    prolonged_idle (dedup_key="prolonged_idle") re-emitted on every poll cycle
    — 1913 unrouted lines in 24h in the deployed daemon.
    """
    import structlog

    config = NotificationsConfig(channels=[], routing=[])
    svc = _build_service(config, [])

    event = _make_event(event_type=EventType.prolonged_idle, dedup_key="prolonged_idle")
    with structlog.testing.capture_logs() as logs:
        await svc.dispatch(event)
        await svc.dispatch(event)

    # The duplicate poll must not add per-cycle INFO lines — that is the
    # reported log-volume symptom. Under the default INFO config the
    # deduplicated branch logs at DEBUG, so it never reaches the capture.
    dedup_logs = [e for e in logs if e.get("event") == "notification_unrouted_deduplicated"]
    assert not dedup_logs
    unrouted_logs = [e for e in logs if e.get("event") == "notification_unrouted"]
    assert len(unrouted_logs) == 1
    assert unrouted_logs[0]["log_level"] == "info"

    unrouted = svc.history.query(status=NotificationStatus.unrouted)
    assert len(unrouted) == 1
    deduplicated = svc.history.query(status=NotificationStatus.deduplicated)
    assert len(deduplicated) == 1
    assert deduplicated[0].dedup_key == "prolonged_idle"


@pytest.mark.asyncio
async def test_unrouted_dedup_re_emits_after_window_expires() -> None:
    """The suppression is a window, not a latch: once it expires the event emits again.

    Uses a non-default window wired through NotificationsConfig so the test
    fails if NotificationService ignores unrouted_dedup_window_seconds.
    """
    from datetime import UTC, datetime, timedelta

    config = NotificationsConfig(channels=[], routing=[], unrouted_dedup_window_seconds=30)
    svc = _build_service(config, [])

    event = _make_event(event_type=EventType.prolonged_idle, dedup_key="prolonged_idle")
    await svc.dispatch(event)
    # Backdate the sighting just past the configured (non-default, 30 s) window.
    svc._unrouted_dedup_window._seen["prolonged_idle"] = datetime.now(UTC) - timedelta(seconds=31)

    await svc.dispatch(event)

    unrouted = svc.history.query(status=NotificationStatus.unrouted)
    assert len(unrouted) == 2


# ---------------------------------------------------------------------------
# 530: prolonged_idle notifies once per idle episode, not once per window
# ---------------------------------------------------------------------------


def _make_episode_event(dedup_key: str) -> NotificationEvent:
    event = _make_event(event_type=EventType.prolonged_idle, dedup_key=dedup_key)
    event.episode_scoped = True
    return event


@pytest.mark.asyncio
async def test_episode_scoped_repeat_suppressed_entirely() -> None:
    """Issue 530: an episode-scoped key is latched, not windowed.

    Same-episode repeats stay suppressed no matter how many dedup windows
    elapse: backdating the key's window sighting must not resurrect it.
    """
    from datetime import UTC, datetime, timedelta

    config = NotificationsConfig(channels=[], routing=[], unrouted_dedup_window_seconds=30)
    svc = _build_service(config, [])

    event = _make_episode_event("prolonged_idle@1000.0")
    await svc.dispatch(event)
    await svc.dispatch(event)
    # Simulate dedup windows elapsing for the key.
    svc._unrouted_dedup_window._seen["prolonged_idle@1000.0"] = (
        datetime.now(UTC) - timedelta(seconds=31)
    )
    await svc.dispatch(event)

    unrouted = svc.history.query(status=NotificationStatus.unrouted)
    assert len(unrouted) == 1
    deduplicated = svc.history.query(status=NotificationStatus.deduplicated)
    assert len(deduplicated) == 2
    assert deduplicated[0].dedup_key == "prolonged_idle@1000.0"


@pytest.mark.asyncio
async def test_episode_boundary_resets_suppression() -> None:
    """Issue 530: a new idle episode is a new key, so it may notify again."""
    config = NotificationsConfig(channels=[], routing=[], unrouted_dedup_window_seconds=600)
    svc = _build_service(config, [])

    await svc.dispatch(_make_episode_event("prolonged_idle@1000.0"))
    await svc.dispatch(_make_episode_event("prolonged_idle@2000.0"))

    unrouted = svc.history.query(status=NotificationStatus.unrouted)
    assert {r.dedup_key for r in unrouted} == {
        "prolonged_idle@1000.0",
        "prolonged_idle@2000.0",
    }
    deduplicated = svc.history.query(status=NotificationStatus.deduplicated)
    assert len(deduplicated) == 0


@pytest.mark.asyncio
async def test_window_key_re_emits_while_episode_key_stays_latched() -> None:
    """Issue 530: window and episode suppression stay coherent.

    A plain key keeps the #492 window semantics (re-emit after expiry) while
    an episode key is latched for its whole episode, and the episode key
    never enters the window at all.
    """
    from datetime import UTC, datetime, timedelta

    config = NotificationsConfig(channels=[], routing=[], unrouted_dedup_window_seconds=30)
    svc = _build_service(config, [])

    plain = _make_event(event_type=EventType.prolonged_idle, dedup_key="prolonged_idle")
    episode = _make_episode_event("prolonged_idle@1000.0")
    await svc.dispatch(plain)
    await svc.dispatch(episode)
    await svc.dispatch(plain)
    await svc.dispatch(episode)
    # Expire the plain key's window sighting only.
    svc._unrouted_dedup_window._seen["prolonged_idle"] = datetime.now(UTC) - timedelta(seconds=31)
    await svc.dispatch(plain)
    await svc.dispatch(episode)

    unrouted = svc.history.query(status=NotificationStatus.unrouted)
    plain_keys = [r.dedup_key for r in unrouted if r.dedup_key == "prolonged_idle"]
    episode_keys = [r.dedup_key for r in unrouted if r.dedup_key == "prolonged_idle@1000.0"]
    assert len(plain_keys) == 2
    assert len(episode_keys) == 1
    assert "prolonged_idle@1000.0" not in svc._unrouted_dedup_window._seen


# ---------------------------------------------------------------------------
# 532: routed channels honor episode-scoped keys, latched per channel
# ---------------------------------------------------------------------------


def _routed_config(
    *,
    channel_name: str = "slack-ops",
    dedup_window_seconds: int = 600,
    retry_count: int = 3,
) -> NotificationsConfig:
    return _make_config(
        channel_name=channel_name,
        dedup_window_seconds=dedup_window_seconds,
        retry_count=retry_count,
        routing=[(EventType.prolonged_idle, [channel_name])],
    )


@pytest.mark.asyncio
async def test_routed_episode_first_emit_delivered_repeat_suppressed() -> None:
    """Issue 532: first prolonged_idle per episode per channel delivers.

    Same-episode repeats stay suppressed even after the channel's dedup
    window elapses: the episode latch is not a window.
    """
    from datetime import UTC, datetime, timedelta

    config = _routed_config()
    sender = FakeChannelSender("slack-ops")
    svc = _build_service(config, [sender])

    event = _make_episode_event("prolonged_idle@1000.0")
    await svc.dispatch(event)
    await svc.dispatch(event)
    # Simulate the channel's dedup windows elapsing for the key.
    svc._dedup_windows["slack-ops"]._seen["prolonged_idle@1000.0"] = (
        datetime.now(UTC) - timedelta(seconds=601)
    )
    await svc.dispatch(event)

    assert len(sender.sent) == 1
    deduplicated = svc.history.query(status=NotificationStatus.deduplicated)
    assert len(deduplicated) == 2
    assert all(r.channel_name == "slack-ops" for r in deduplicated)


@pytest.mark.asyncio
async def test_routed_episode_suppression_is_per_channel() -> None:
    """Issue 532: suppression is keyed per channel, one delivery each."""
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
                event_type=EventType.prolonged_idle,
                channels=["slack-ops", "slack-alerts"],
            ),
        ],
    )
    sender1 = FakeChannelSender("slack-ops")
    sender2 = FakeChannelSender("slack-alerts")
    svc = _build_service(config, [sender1, sender2])

    event = _make_episode_event("prolonged_idle@1000.0")
    for _ in range(3):
        await svc.dispatch(event)

    assert len(sender1.sent) == 1
    assert len(sender2.sent) == 1
    deduplicated = svc.history.query(status=NotificationStatus.deduplicated)
    assert len(deduplicated) == 4
    assert {r.channel_name for r in deduplicated} == {"slack-ops", "slack-alerts"}


@pytest.mark.asyncio
async def test_routed_episode_failed_delivery_retried_on_next_emit() -> None:
    """Issue 532: record-on-success, a failed delivery is not lost for the episode."""
    config = _routed_config(retry_count=1)
    sender = FakeChannelSender("slack-ops", fail_first_n=1)
    svc = _build_service(config, [sender])

    event = _make_episode_event("prolonged_idle@1000.0")
    await svc.dispatch(event)
    failed = svc.history.query(status=NotificationStatus.failed)
    assert len(failed) == 1

    await svc.dispatch(event)
    assert len(sender.sent) == 1

    await svc.dispatch(event)
    assert len(sender.sent) == 1
    deduplicated = svc.history.query(status=NotificationStatus.deduplicated)
    assert len(deduplicated) == 1


@pytest.mark.asyncio
async def test_routed_episode_boundary_resets_suppression() -> None:
    """Issue 532: a new idle episode is a new key, so each channel may notify again."""
    config = _routed_config()
    sender = FakeChannelSender("slack-ops")
    svc = _build_service(config, [sender])

    await svc.dispatch(_make_episode_event("prolonged_idle@1000.0"))
    await svc.dispatch(_make_episode_event("prolonged_idle@2000.0"))

    assert len(sender.sent) == 2
    assert svc.history.query(status=NotificationStatus.deduplicated) == []


@pytest.mark.asyncio
async def test_routed_plain_key_keeps_window_semantics() -> None:
    """Issue 532: plain keys on routed channels keep the per-channel window."""
    from datetime import UTC, datetime, timedelta

    config = _routed_config()
    sender = FakeChannelSender("slack-ops")
    svc = _build_service(config, [sender])

    plain = _make_event(event_type=EventType.prolonged_idle, dedup_key="prolonged_idle")
    await svc.dispatch(plain)
    await svc.dispatch(plain)
    assert len(sender.sent) == 1
    # Expire the plain key's channel-window sighting: it re-delivers.
    svc._dedup_windows["slack-ops"]._seen["prolonged_idle"] = (
        datetime.now(UTC) - timedelta(seconds=601)
    )
    await svc.dispatch(plain)

    assert len(sender.sent) == 2
    assert ("slack-ops", "prolonged_idle") not in svc._routed_episode_latch


@pytest.mark.asyncio
async def test_routed_episode_concurrent_duplicate_dispatch_sends_once() -> None:
    """Issue 532: an in-flight episode key suppresses a concurrent duplicate.

    The latch is reserved before the first await, so a second dispatch of the
    same key while the first send is in flight is deduplicated, not delivered.
    """
    import asyncio

    config = _routed_config()
    sender = YieldingSender("slack-ops")
    svc = _build_service(config, [sender])

    event = _make_episode_event("prolonged_idle@1000.0")
    await asyncio.gather(svc.dispatch(event), svc.dispatch(event))

    assert len(sender.sent) == 1
    deduplicated = svc.history.query(status=NotificationStatus.deduplicated)
    assert len(deduplicated) == 1


@pytest.mark.asyncio
async def test_routed_episode_template_failure_does_not_latch() -> None:
    """Issue 532: a template-render failure must not latch the episode key."""
    config = NotificationsConfig(
        channels=[
            ChannelConfig(
                name="slack-ops",
                type="slack",
                webhook_url="https://hooks.slack.com/services/T/B/C",
                message_template="{event_type}: {detail}",
            ),
        ],
        routing=[RoutingEntry(event_type=EventType.prolonged_idle, channels=["slack-ops"])],
    )
    sender = FakeChannelSender("slack-ops")
    svc = _build_service(config, [sender])

    broken = _make_event(event_type=EventType.prolonged_idle, dedup_key="prolonged_idle@1000.0")
    broken.episode_scoped = True
    await svc.dispatch(broken)

    good = _make_episode_event("prolonged_idle@1000.0")
    good.payload["detail"] = "recovered"
    await svc.dispatch(good)

    assert len(sender.sent) == 1
    assert svc.history.query(status=NotificationStatus.deduplicated) == []
