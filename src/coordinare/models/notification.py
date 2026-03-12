from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

# ---------------------------------------------------------------------------
# 006 — Enums
# ---------------------------------------------------------------------------


class EventType(StrEnum):
    card_transition = "card_transition"
    card_dispatched = "card_dispatched"
    card_blocked = "card_blocked"
    card_merged = "card_merged"
    advocate_escalation = "advocate_escalation"
    daemon_restart = "daemon_restart"
    circuit_breaker_trip = "circuit_breaker_trip"
    prolonged_idle = "prolonged_idle"
    performer_error = "performer_error"


class NotificationSeverity(StrEnum):
    info = "info"
    warning = "warning"
    critical = "critical"


class NotificationStatus(StrEnum):
    delivered = "delivered"
    failed = "failed"
    rate_limited = "rate_limited"
    deduplicated = "deduplicated"
    unrouted = "unrouted"


class ChannelType(StrEnum):
    slack = "slack"
    email = "email"


# ---------------------------------------------------------------------------
# 006 — Core entities
# ---------------------------------------------------------------------------


@dataclass
class NotificationEvent:
    event_type: EventType
    severity: NotificationSeverity
    payload: dict[str, str]
    source: str
    dedup_key: str | None = None


@dataclass
class NotificationAttempt:
    attempt_id: str
    event_type: EventType
    channel_name: str
    status: NotificationStatus
    timestamp: datetime
    elapsed_ms: float
    dedup_key: str | None = None
    retries_attempted: int = 0
    error_message: str | None = None


# ---------------------------------------------------------------------------
# 006 — NotificationHistory (in-memory, time-bounded)
# ---------------------------------------------------------------------------


class NotificationHistory:
    def __init__(self, max_age_hours: int = 24) -> None:
        self._records: list[NotificationAttempt] = []
        self._max_age_seconds: float = max_age_hours * 3600

    def append(self, attempt: NotificationAttempt) -> None:
        self._evict()
        self._records.append(attempt)

    def query(
        self,
        *,
        event_type: EventType | None = None,
        channel_name: str | None = None,
        status: NotificationStatus | None = None,
        since: datetime | None = None,
    ) -> list[NotificationAttempt]:
        self._evict()
        result = self._records[:]
        if event_type is not None:
            result = [r for r in result if r.event_type == event_type]
        if channel_name is not None:
            result = [r for r in result if r.channel_name == channel_name]
        if status is not None:
            result = [r for r in result if r.status == status]
        if since is not None:
            result = [r for r in result if r.timestamp >= since]
        return list(reversed(result))

    def _evict(self) -> None:
        cutoff = datetime.now(UTC) - timedelta(seconds=self._max_age_seconds)
        while self._records and self._records[0].timestamp < cutoff:
            self._records.pop(0)
