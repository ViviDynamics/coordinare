"""Unified notification dispatch service.

Routes NotificationEvent objects through a configurable routing table,
applies per-channel rate limiting and deduplication, retries failed deliveries
up to a configurable bound, and records all attempts in a time-bounded
in-memory NotificationHistory.
"""
from __future__ import annotations

import asyncio
import uuid
from collections import deque
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from time import monotonic
from typing import TYPE_CHECKING, Protocol

import aiosmtplib
import httpx
import structlog

if TYPE_CHECKING:
    from coordinare.config import ChannelConfig, NotificationsConfig
    from coordinare.metrics import CoordinareMetrics

from coordinare.models.notification import (
    EventType,
    NotificationAttempt,
    NotificationEvent,
    NotificationHistory,
    NotificationStatus,
)

logger = structlog.get_logger(__name__)


# ---------------------------------------------------------------------------
# Channel Sender Protocol + Implementations (T009)
# ---------------------------------------------------------------------------


class ChannelSenderProtocol(Protocol):
    @property
    def channel_name(self) -> str: ...
    async def send(self, message: str, *, subject: str | None = None) -> None: ...


class SlackChannelSender:
    def __init__(self, name: str, webhook_url: str) -> None:
        self._name = name
        self._webhook_url = webhook_url

    @property
    def channel_name(self) -> str:
        return self._name

    async def send(self, message: str, *, subject: str | None = None) -> None:
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(
                self._webhook_url,
                json={"text": message},
            )
            response.raise_for_status()


class EmailChannelSender:
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
    ) -> None:
        self._name = name
        self._smtp_host = smtp_host
        self._smtp_port = smtp_port
        self._smtp_username = smtp_username
        self._smtp_password = smtp_password
        self._sender = sender
        self._recipient = recipient

    @property
    def channel_name(self) -> str:
        return self._name

    async def send(self, message: str, *, subject: str | None = None) -> None:
        msg = EmailMessage()
        msg["From"] = self._sender
        msg["To"] = self._recipient
        msg["Subject"] = subject or "[Coordinare] Notification"
        msg.set_content(message)
        await aiosmtplib.send(
            msg,
            hostname=self._smtp_host,
            port=self._smtp_port,
            username=self._smtp_username,
            password=self._smtp_password,
            start_tls=True,
            timeout=10,
        )


# ---------------------------------------------------------------------------
# Rate Limiter + Deduplication (T022, T023)
# ---------------------------------------------------------------------------


class SlidingWindowRateLimiter:
    def __init__(self, max_messages: int, window_seconds: float) -> None:
        self._max_messages = max_messages
        self._window_seconds = window_seconds
        self._timestamps: deque[datetime] = deque()

    def is_allowed(self) -> bool:
        if self._max_messages == 0:
            return True
        now = datetime.now(UTC)
        cutoff = now - timedelta(seconds=self._window_seconds)
        while self._timestamps and self._timestamps[0] < cutoff:
            self._timestamps.popleft()
        if len(self._timestamps) >= self._max_messages:
            return False
        self._timestamps.append(now)
        return True


class DeduplicationWindow:
    def __init__(self, window_seconds: float) -> None:
        self._window_seconds = window_seconds
        self._seen: dict[str, datetime] = {}

    def is_duplicate(self, dedup_key: str) -> bool:
        if dedup_key not in self._seen:
            return False
        return (datetime.now(UTC) - self._seen[dedup_key]).total_seconds() < self._window_seconds

    def record(self, dedup_key: str) -> None:
        self._seen[dedup_key] = datetime.now(UTC)
        self._evict()

    def _evict(self) -> None:
        cutoff = datetime.now(UTC) - timedelta(seconds=self._window_seconds * 2)
        self._seen = {k: v for k, v in self._seen.items() if v >= cutoff}


# ---------------------------------------------------------------------------
# NotificationService (T011, T024, T028)
# ---------------------------------------------------------------------------


class NotificationService:
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
        self._card_blocked_reminder_cooldown_seconds = (
            config.card_blocked_reminder_cooldown_seconds
        )
        self._metrics = metrics

    @property
    def history(self) -> NotificationHistory:
        return self._history

    @property
    def card_blocked_reminder_cooldown_seconds(self) -> int:
        return self._card_blocked_reminder_cooldown_seconds

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
            logger.info("notification_unrouted", event_type=event.event_type.value, source=event.source)
            return

        await asyncio.gather(
            *[self._dispatch_to_channel(event, name) for name in channel_names],
            return_exceptions=True,
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
            self._metrics.notifications_deduplicated_total.labels(channel_name=channel_name).inc()
            return

        # Rate limit check
        if not self._rate_limiters[channel_name].is_allowed():
            self._record(event, channel_name, NotificationStatus.rate_limited, start)
            self._metrics.notifications_rate_limited_total.labels(channel_name=channel_name).inc()
            return

        # Retry loop
        message = cfg.message_template.format_map(event.payload)
        subject = cfg.subject_template.format_map(event.payload) if cfg.subject_template else None
        last_error: str | None = None
        for attempt_num in range(cfg.retry_count):
            try:
                await sender.send(message, subject=subject)
                self._record(
                    event, channel_name, NotificationStatus.delivered, start,
                    retries_attempted=attempt_num,
                )
                if event.dedup_key:
                    self._dedup_windows[channel_name].record(event.dedup_key)
                self._metrics.notifications_dispatched_total.labels(
                    event_type=event.event_type.value, channel_name=channel_name,
                ).inc()
                return
            except Exception as exc:
                last_error = str(exc)
                if attempt_num < cfg.retry_count - 1:
                    await asyncio.sleep(cfg.retry_delay_seconds)

        self._record(
            event, channel_name, NotificationStatus.failed, start,
            retries_attempted=cfg.retry_count,
            error_message=last_error,
        )
        self._metrics.notifications_failed_total.labels(channel_name=channel_name).inc()
        logger.error(
            "notification_delivery_failed",
            channel=channel_name,
            event_type=event.event_type.value,
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
            event_type=event.event_type.value,
            channel=channel_name,
            status=status.value,
            elapsed_ms=round(elapsed_ms, 2),
        )

    @staticmethod
    def _build_routing(config: NotificationsConfig) -> dict[EventType, list[str]]:
        return {
            entry.event_type: entry.channels
            for entry in config.routing
            if entry.enabled
        }


# ---------------------------------------------------------------------------
# Factory (T010)
# ---------------------------------------------------------------------------


def build_notification_service(
    config: NotificationsConfig,
    metrics: CoordinareMetrics,
) -> NotificationService:
    from coordinare.models.notification import ChannelType

    senders: list[ChannelSenderProtocol] = []
    for ch in config.channels:
        if ch.type == ChannelType.slack:
            senders.append(
                SlackChannelSender(
                    name=ch.name,
                    webhook_url=ch.webhook_url.get_secret_value() if ch.webhook_url else "",
                )
            )
        elif ch.type == ChannelType.email:
            senders.append(
                EmailChannelSender(
                    name=ch.name,
                    smtp_host=ch.smtp_host or "",
                    smtp_port=ch.smtp_port,
                    smtp_username=ch.smtp_username,
                    smtp_password=ch.smtp_password.get_secret_value() if ch.smtp_password else None,
                    sender=ch.smtp_sender,
                    recipient=ch.smtp_recipient or "",
                )
            )
    return NotificationService(config, senders, metrics)
