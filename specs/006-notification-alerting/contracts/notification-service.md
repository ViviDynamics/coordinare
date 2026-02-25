# Contract: NotificationService

**Feature**: 006-notification-alerting
**Module**: `src/coordinare/services/notification.py` (NEW)
**Date**: 2026-02-24

---

## NotificationServiceProtocol

The protocol used in `CoordinareState` and injected at startup. Replaces the old `NotificationServiceProtocol` (`send_notification(*args, **kwargs)`).

```python
from __future__ import annotations
from typing import Protocol
from coordinare.models.notification import NotificationEvent


class NotificationServiceProtocol(Protocol):
    """Protocol for the unified notification dispatch service.

    V1 implementation: NotificationService (services/notification.py)
    Test implementation: FakeNotificationService (for unit/integration tests)
    """

    async def dispatch(self, event: NotificationEvent) -> None:
        """Route, rate-limit, dedup, retry, and record a notification event.

        Args:
            event: The structured event to dispatch.

        Guarantees:
            - MUST NOT raise. All delivery failures are swallowed after retries.
            - Always records a NotificationAttempt regardless of outcome.
            - Concurrent delivery to all matched channels via asyncio.gather.
        """
        ...

    @property
    def history(self) -> "NotificationHistory":
        """Access to the in-memory notification attempt log."""
        ...
```

---

## ChannelSenderProtocol

Internal protocol for low-level channel delivery. Not exposed on `CoordinareState` — used only within `NotificationService`.

```python
from typing import Protocol


class ChannelSenderProtocol(Protocol):
    """Low-level channel transport. Raises on delivery failure (caught by retry logic)."""

    @property
    def channel_name(self) -> str:
        """Unique name matching the ChannelConfig name field."""
        ...

    async def send(self, message: str) -> None:
        """Send a pre-rendered message string to the channel.

        Raises:
            Exception: Any transport error (httpx, aiosmtplib, etc.).
                       The caller (NotificationService retry loop) handles these.
        """
        ...
```

---

## V1: SlackChannelSender

```python
class SlackChannelSender:
    """Delivers notifications to a Slack channel via webhook URL (httpx)."""

    def __init__(self, name: str, webhook_url: str) -> None:
        self._name = name
        self._webhook_url = webhook_url

    @property
    def channel_name(self) -> str:
        return self._name

    async def send(self, message: str) -> None:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                self._webhook_url,
                json={"text": message},
            )
            response.raise_for_status()
```

**Error Contract**:
- `httpx.HTTPStatusError`, `httpx.TransportError`, `httpx.TimeoutException` — all propagate to retry logic.
- `response.raise_for_status()` raises on 4xx/5xx — treated as delivery failure.

---

## V1: EmailChannelSender

```python
class EmailChannelSender:
    """Delivers notifications via SMTP (aiosmtplib)."""

    def __init__(
        self,
        name: str,
        *,
        smtp_host: str,
        smtp_port: int,
        smtp_username: str | None,
        smtp_password: str | None,
        sender: str,
        recipient: str,
        subject_template: str = "[Coordinare] {event_type}",
    ) -> None:
        self._name = name
        self._smtp_host = smtp_host
        self._smtp_port = smtp_port
        self._smtp_username = smtp_username
        self._smtp_password = smtp_password
        self._sender = sender
        self._recipient = recipient
        self._subject_template = subject_template

    @property
    def channel_name(self) -> str:
        return self._name

    async def send(self, message: str) -> None:
        # subject is rendered separately from message body
        subject = self._subject_template  # caller may extend to pass event context
        msg = EmailMessage()
        msg["From"] = self._sender
        msg["To"] = self._recipient
        msg["Subject"] = subject
        msg.set_content(message)
        await aiosmtplib.send(
            msg,
            hostname=self._smtp_host,
            port=self._smtp_port,
            username=self._smtp_username,
            password=self._smtp_password,
        )
```

**Error Contract**:
- `aiosmtplib.SMTPException` and subclasses — propagate to retry logic.
- Network errors — propagate to retry logic.

---

## NotificationService (full implementation contract)

```python
class NotificationService:
    """Routes NotificationEvents to configured channels with rate limiting,
    deduplication, bounded retry, and history recording."""

    def __init__(
        self,
        config: NotificationsConfig,
        senders: list[ChannelSenderProtocol],
        metrics: CoordinareMetrics,
    ) -> None:
        self._routing: dict[EventType, list[str]] = self._build_routing(config)
        self._senders: dict[str, ChannelSenderProtocol] = {s.channel_name: s for s in senders}
        self._channel_configs: dict[str, ChannelConfig] = {c.name: c for c in config.channels}
        self._rate_limiters: dict[str, SlidingWindowRateLimiter] = {
            c.name: SlidingWindowRateLimiter(c.rate_limit, c.rate_window_seconds)
            for c in config.channels
        }
        self._dedup_windows: dict[str, DeduplicationWindow] = {
            c.name: DeduplicationWindow(c.dedup_window_seconds)
            for c in config.channels
        }
        self._history = NotificationHistory(config.history_max_age_hours)
        self._metrics = metrics

    @property
    def history(self) -> NotificationHistory:
        return self._history

    async def dispatch(self, event: NotificationEvent) -> None:
        channel_names = self._routing.get(event.event_type, [])
        if not channel_names:
            attempt = NotificationAttempt(
                attempt_id=str(uuid.uuid4()),
                event_type=event.event_type,
                channel_name="(none)",
                status=NotificationStatus.unrouted,
                timestamp=datetime.now(UTC),
                elapsed_ms=0.0,
                dedup_key=event.dedup_key,
            )
            self._history.append(attempt)
            logger.info("notification_unrouted", event_type=event.event_type, source=event.source)
            return

        await asyncio.gather(
            *[self._dispatch_to_channel(event, name) for name in channel_names],
            return_exceptions=True,  # should never raise; belt-and-suspenders
        )

    async def _dispatch_to_channel(self, event: NotificationEvent, channel_name: str) -> None:
        cfg = self._channel_configs.get(channel_name)
        sender = self._senders.get(channel_name)
        if cfg is None or sender is None:
            logger.warning("notification_channel_missing", channel=channel_name)
            return

        start = monotonic()

        # Deduplication check
        if event.dedup_key and self._dedup_windows[channel_name].is_duplicate(event.dedup_key):
            self._record(event, channel_name, NotificationStatus.deduplicated, start)
            self._metrics.notifications_deduplicated_total.labels(channel=channel_name).inc()
            return

        # Rate limit check
        if not self._rate_limiters[channel_name].is_allowed():
            self._record(event, channel_name, NotificationStatus.rate_limited, start)
            self._metrics.notifications_rate_limited_total.labels(channel=channel_name).inc()
            return

        # Retry loop
        message = cfg.message_template.format_map(event.payload)
        last_error: str | None = None
        for attempt_num in range(cfg.retry_count):
            try:
                await sender.send(message)
                self._record(
                    event, channel_name, NotificationStatus.delivered, start,
                    retries_attempted=attempt_num,
                )
                if event.dedup_key:
                    self._dedup_windows[channel_name].record(event.dedup_key)
                self._metrics.notifications_dispatched_total.labels(
                    event_type=event.event_type, channel=channel_name
                ).inc()
                return
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
                if attempt_num < cfg.retry_count - 1:
                    await asyncio.sleep(cfg.retry_delay_seconds)

        self._record(
            event, channel_name, NotificationStatus.failed, start,
            retries_attempted=cfg.retry_count,
            error_message=last_error,
        )
        self._metrics.notifications_failed_total.labels(channel=channel_name).inc()
        logger.error(
            "notification_delivery_failed",
            channel=channel_name,
            event_type=event.event_type,
            retries=cfg.retry_count,
            error=last_error,
        )

    def _record(
        self,
        event: NotificationEvent,
        channel_name: str,
        status: NotificationStatus,
        start: float,
        *,
        retries_attempted: int = 0,
        error_message: str | None = None,
    ) -> None:
        elapsed_ms = (monotonic() - start) * 1000
        attempt = NotificationAttempt(
            attempt_id=str(uuid.uuid4()),
            event_type=event.event_type,
            channel_name=channel_name,
            status=status,
            timestamp=datetime.now(UTC),
            elapsed_ms=elapsed_ms,
            dedup_key=event.dedup_key,
            retries_attempted=retries_attempted,
            error_message=error_message,
        )
        self._history.append(attempt)
        logger.info(
            "notification_attempt",
            event_type=event.event_type,
            channel=channel_name,
            status=status,
            elapsed_ms=round(elapsed_ms, 2),
        )

    @staticmethod
    def _build_routing(config: NotificationsConfig) -> dict[EventType, list[str]]:
        return {
            entry.event_type: entry.channels
            for entry in config.routing
            if entry.enabled
        }
```

---

## Factory: `build_notification_service`

Called by `__main__._bootstrap_services()` to wire `NotificationService` from `ProjectConfiguration`.

```python
def build_notification_service(
    config: NotificationsConfig,
    metrics: CoordinareMetrics,
) -> NotificationService:
    """Instantiate NotificationService from config, building channel senders."""
    senders: list[ChannelSenderProtocol] = []
    for ch in config.channels:
        if ch.type == ChannelType.slack:
            senders.append(
                SlackChannelSender(
                    name=ch.name,
                    webhook_url=ch.webhook_url.get_secret_value(),
                )
            )
        elif ch.type == ChannelType.email:
            senders.append(
                EmailChannelSender(
                    name=ch.name,
                    smtp_host=ch.smtp_host,
                    smtp_port=ch.smtp_port,
                    smtp_username=ch.smtp_username,
                    smtp_password=ch.smtp_password.get_secret_value() if ch.smtp_password else None,
                    sender=ch.smtp_sender,
                    recipient=ch.smtp_recipient,
                )
            )
    return NotificationService(config, senders, metrics)
```

---

## Metrics Extensions (`src/coordinare/metrics.py`)

New counters added to `CoordinareMetrics.__init__()`:

```python
self.notifications_dispatched_total = Counter(
    "coordinare_notifications_dispatched_total",
    "Notifications successfully delivered",
    labelnames=("event_type", "channel"),
    registry=self.registry,
)
self.notifications_failed_total = Counter(
    "coordinare_notifications_failed_total",
    "Notifications that failed after all retries",
    labelnames=("channel",),
    registry=self.registry,
)
self.notifications_rate_limited_total = Counter(
    "coordinare_notifications_rate_limited_total",
    "Notifications dropped due to rate limiting",
    labelnames=("channel",),
    registry=self.registry,
)
self.notifications_deduplicated_total = Counter(
    "coordinare_notifications_deduplicated_total",
    "Notifications suppressed by deduplication",
    labelnames=("channel",),
    registry=self.registry,
)
```

**Existing metric kept**: `notifications_total(channel, status)` from spec 001 is **removed** during migration (it tracked the old Notification model). The four new counters above replace and extend its coverage.

---

## `notify.py` Node Migration Contract

After migration (FR-001), `notify.py` dispatches a `NotificationEvent` instead of building a `Notification`:

```python
async def notify(state: CoordinareState) -> CoordinareState:
    notification_service = state.get("notification_service")
    card = state.get("current_card")
    if not isinstance(card, dict) or notification_service is None:
        return state

    event = NotificationEvent(
        event_type=EventType.card_transition,  # or card_dispatched / card_merged / card_blocked
        severity=NotificationSeverity.info,
        source="board",
        payload={
            "card_title": str(card.get("title", "")),
            "previous_status": str(card.get("previous_status", "")),
            "card_status": str(card.get("status", "")),
            "pr_url": str(card.get("pr_url", "")),
            "summary": _build_summary(card, state),
        },
        dedup_key=f"card_transition:{card.get('id', '')}:{card.get('status', '')}",
    )
    await notification_service.dispatch(event)
    return state
```

**Event type selection**: The `notify` node should use the most specific `EventType` for the current state transition (e.g., `card_dispatched` when `phase=="dispatching"`, `card_merged` after merge, `card_blocked` after block). A helper `_event_type_for_phase(phase: str) -> EventType` maps phase to event type.

---

## System Alert Dispatch (`daemon.py` hooks)

### daemon_restart

In `CoordinareDaemon.start()`, immediately after the startup emit and before the first poll cycle:

```python
notification_service = self._state.get("notification_service")
if notification_service is not None:
    await notification_service.dispatch(
        NotificationEvent(
            event_type=EventType.daemon_restart,
            severity=NotificationSeverity.info,
            source="daemon",
            payload={
                "run_mode": self._run_mode,
                "summary": "Coordinare daemon started",
            },
        )
    )
```

### prolonged_idle

In the `start()` loop, after `initial_state()` and `last_heartbeat` setup, add:

```python
last_activity_at = monotonic()

# Inside the loop, after phase check:
current_phase = self._state.get("phase")
if current_phase != "idle":
    last_activity_at = monotonic()  # reset on any non-idle activity

idle_seconds = monotonic() - last_activity_at
idle_threshold = config.notifications.prolonged_idle_threshold_seconds
if idle_seconds >= idle_threshold and notification_service is not None:
    await notification_service.dispatch(
        NotificationEvent(
            event_type=EventType.prolonged_idle,
            severity=NotificationSeverity.warning,
            source="daemon",
            payload={
                "idle_seconds": str(int(idle_seconds)),
                "threshold_seconds": str(idle_threshold),
                "summary": f"No board activity for {int(idle_seconds)}s",
            },
            dedup_key="prolonged_idle",  # dedup prevents repeated alerts within window
        )
    )
```

**Note**: `CoordinareDaemon` does not currently accept `config` as a parameter — it receives scalar values from `__main__.py`. The `idle_threshold_seconds` must be passed as a constructor parameter: `idle_threshold_seconds: int = 1800`.

---

## Rate Limit Estimates

| Operation | Worst-case per cycle | Notes |
|-----------|---------------------|-------|
| `dispatch(card_transition)` to 1 Slack channel | 1 httpx call | ~100ms typical |
| `dispatch(advocate_escalation)` to Slack + email | 2 parallel calls | ~200ms typical |
| Retry (5×, 2s delay each) | 10s per channel | Only on delivery failure |
| `prolonged_idle` dispatch | 1 call, deduped | Once per dedup_window |
