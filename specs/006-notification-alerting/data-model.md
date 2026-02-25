# Data Model: Notification & Alerting System (006)

**Feature**: 006-notification-alerting
**Module**: `src/coordinare/models/notification.py` (extensions) + `src/coordinare/services/notification.py` (NEW) + `src/coordinare/config.py` (extensions)
**Date**: 2026-02-24

---

## Enums

### EventType

Classification of a notification event's origin and purpose.

```python
class EventType(str, Enum):
    # Board orchestrator events (migrated from old Notification model)
    card_transition   = "card_transition"    # Card moved between columns
    card_dispatched   = "card_dispatched"    # Card dispatched to agent
    card_blocked      = "card_blocked"       # Card entered blocked state
    card_merged       = "card_merged"        # PR merged for a card

    # Advocate events (spec 007 escalation path)
    advocate_escalation = "advocate_escalation"   # Issue escalated to human

    # System alert events (daemon infrastructure)
    daemon_restart       = "daemon_restart"       # Daemon started or restarted
    circuit_breaker_trip = "circuit_breaker_trip" # Circuit breaker tripped (spec 005)
    prolonged_idle       = "prolonged_idle"       # No board activity for threshold period
```

### NotificationSeverity

Operator-visible importance level of the event.

```python
class NotificationSeverity(str, Enum):
    info     = "info"
    warning  = "warning"
    critical = "critical"
```

### NotificationStatus

Outcome of a single delivery attempt.

```python
class NotificationStatus(str, Enum):
    delivered   = "delivered"    # Sent successfully to channel
    failed      = "failed"       # All retries exhausted; channel unreachable
    rate_limited = "rate_limited" # Dropped: channel rate limit exceeded
    deduplicated = "deduplicated" # Dropped: same dedup_key within window
    unrouted    = "unrouted"     # No routing entry matched this event type
```

### ChannelType

Supported channel transport types.

```python
class ChannelType(str, Enum):
    slack = "slack"
    email = "email"
```

---

## Entities

### NotificationEvent

A structured event submitted to `NotificationService.dispatch()` by any coordinare component.

```python
@dataclass
class NotificationEvent:
    event_type: EventType
    severity: NotificationSeverity
    payload: dict[str, str]          # Key-value pairs used for template rendering
    source: str                      # Subsystem name: "board", "advocate", "daemon"
    dedup_key: str | None = None     # If set, deduplication is applied per channel
```

**Validation rules**:
- `event_type` must be a valid `EventType` member
- `source` must be non-empty
- `payload` values must be strings (safe for `str.format_map`)

**Example payload for `card_transition`**:
```python
{
    "card_title": "Add login page",
    "previous_status": "in_progress",
    "card_status": "in_review",
    "pr_url": "https://github.com/org/repo/pull/42",
}
```

---

### NotificationAttempt

A single delivery record in `NotificationHistory`.

```python
@dataclass
class NotificationAttempt:
    attempt_id: str                   # uuid4 string
    event_type: EventType
    channel_name: str                 # Name of the channel config entry
    status: NotificationStatus
    timestamp: datetime               # UTC timestamp of attempt
    elapsed_ms: float                 # Wall-clock ms for the attempt (incl. retries)
    dedup_key: str | None             # Dedup key from the event, if any
    retries_attempted: int = 0        # 0 if delivered on first try
    error_message: str | None = None  # Final error message after retries exhausted
```

---

### NotificationHistory

In-memory log of all `NotificationAttempt` records for the current daemon session.

```python
class NotificationHistory:
    def __init__(self, max_age_hours: int = 24) -> None:
        self._records: list[NotificationAttempt] = []
        self._max_age_seconds: float = max_age_hours * 3600

    def append(self, attempt: NotificationAttempt) -> None:
        """Add record and lazily evict expired entries from the front."""
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
        """Return matching records, newest-first."""
        self._evict()
        result = self._records[:]
        if event_type:
            result = [r for r in result if r.event_type == event_type]
        if channel_name:
            result = [r for r in result if r.channel_name == channel_name]
        if status:
            result = [r for r in result if r.status == status]
        if since:
            result = [r for r in result if r.timestamp >= since]
        return list(reversed(result))

    def _evict(self) -> None:
        """Remove records older than max_age_seconds from the front."""
        cutoff = datetime.now(UTC) - timedelta(seconds=self._max_age_seconds)
        while self._records and self._records[0].timestamp < cutoff:
            self._records.pop(0)
```

---

### SlidingWindowRateLimiter

Per-channel rate limiter using a sliding timestamp window.

```python
class SlidingWindowRateLimiter:
    """True sliding-window rate limiter using a deque of send timestamps."""

    def __init__(self, max_messages: int, window_seconds: float) -> None:
        self._max_messages = max_messages        # 0 = unlimited
        self._window_seconds = window_seconds
        self._timestamps: deque[datetime] = deque()

    def is_allowed(self) -> bool:
        """Check whether a send is permitted; record timestamp if so."""
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
```

---

### DeduplicationWindow

Per-channel deduplication state.

```python
class DeduplicationWindow:
    """Suppresses repeated events with the same dedup_key within a time window."""

    def __init__(self, window_seconds: float) -> None:
        self._window_seconds = window_seconds
        self._seen: dict[str, datetime] = {}     # dedup_key → last_sent_at

    def is_duplicate(self, dedup_key: str) -> bool:
        """Return True if the key was seen within the window (do not record)."""
        if dedup_key not in self._seen:
            return False
        return (datetime.now(UTC) - self._seen[dedup_key]).total_seconds() < self._window_seconds

    def record(self, dedup_key: str) -> None:
        """Record a key after a successful dispatch."""
        self._seen[dedup_key] = datetime.now(UTC)
        self._evict()

    def _evict(self) -> None:
        """Lazily remove entries older than 2× the window."""
        cutoff = datetime.now(UTC) - timedelta(seconds=self._window_seconds * 2)
        self._seen = {k: v for k, v in self._seen.items() if v >= cutoff}
```

---

## Configuration Model

### ChannelConfig

Pydantic model for a single channel instance in `config.yaml`.

```python
class ChannelConfig(BaseModel):
    name: str                              # Unique identifier (e.g., "slack-ops", "email-team")
    type: ChannelType                      # "slack" or "email"

    # Slack-specific (required when type=slack)
    webhook_url: SecretStr | None = None

    # Email-specific (required when type=email)
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
    smtp_sender: str = "coordinare@vividynamics.com"
    smtp_recipient: str | None = None      # Required when type=email

    # Rate limiting
    rate_limit: int = 0                    # 0 = unlimited; messages per rate_window_seconds
    rate_window_seconds: int = 60

    # Deduplication
    dedup_window_seconds: int = 600        # 10 minutes default

    # Retry
    retry_count: int = 5                   # Max delivery attempts
    retry_delay_seconds: float = 2.0       # Fixed wait between retries

    # Message template (Python str.format_map syntax)
    message_template: str = "{event_type}: {source}"

    @model_validator(mode="after")
    def _validate_type_fields(self) -> "ChannelConfig":
        if self.type == ChannelType.slack and not self.webhook_url:
            raise ValueError("webhook_url required for slack channels")
        if self.type == ChannelType.email and (not self.smtp_host or not self.smtp_recipient):
            raise ValueError("smtp_host and smtp_recipient required for email channels")
        return self
```

**Validation rules**:
- `name` must be unique across all channels (validated at `NotificationsConfig` level)
- `rate_limit >= 0` (0 = unlimited)
- `retry_count >= 1`
- `retry_delay_seconds >= 0`

---

### RoutingEntry

Maps one event type to one or more channel names.

```python
class RoutingEntry(BaseModel):
    event_type: EventType                  # The event this rule matches
    channels: list[str]                    # Channel names (must exist in ChannelConfig list)
    enabled: bool = True                   # False = rule is disabled without deletion
```

---

### NotificationsConfig

Top-level notifications block nested inside `ProjectConfiguration`.

```python
class NotificationsConfig(BaseModel):
    channels: list[ChannelConfig] = Field(default_factory=list)
    routing: list[RoutingEntry] = Field(default_factory=list)
    history_max_age_hours: int = Field(default=24, ge=1)
    prolonged_idle_threshold_seconds: int = Field(default=1800, ge=60)

    @model_validator(mode="after")
    def _validate_unique_channel_names(self) -> "NotificationsConfig":
        names = [c.name for c in self.channels]
        if len(names) != len(set(names)):
            raise ValueError("Channel names must be unique")
        return self

    @model_validator(mode="after")
    def _validate_routing_references(self) -> "NotificationsConfig":
        channel_names = {c.name for c in self.channels}
        for entry in self.routing:
            for ch in entry.channels:
                if ch not in channel_names:
                    raise ValueError(f"Routing entry references unknown channel '{ch}'")
        return self
```

**Added to `ProjectConfiguration`**:
```python
notifications: NotificationsConfig = Field(default_factory=NotificationsConfig)
```

**Removed from `ProjectConfiguration`** (migration in scope per FR-001):
- `notification_email`
- `smtp_host`, `smtp_port`, `smtp_username`, `smtp_password`
- `slack_webhook_url`, `slack_channel`

---

## State Extensions

### `CoordinareState` changes (`src/coordinare/graph/state.py`)

**Removed fields**:
- `email_service: NotificationServiceProtocol`
- `slack_service: NotificationServiceProtocol`

**Added field**:
- `notification_service: NotificationServiceProtocol`

**Updated protocol**:

```python
class NotificationServiceProtocol(Protocol):
    async def dispatch(self, event: NotificationEvent) -> None:
        """Route, rate-limit, dedup, retry and record a notification event."""
        ...
```

**Removed from `initial_state()`**: `email_service` and `slack_service` keys.
**Not added to `initial_state()`**: `notification_service` is injected by `_bootstrap_services()` in `__main__.py` at startup.

---

## Config YAML Example

```yaml
# config.yaml (gitignored — operator fills in real values)
notifications:
  history_max_age_hours: 24
  prolonged_idle_threshold_seconds: 1800

  channels:
    - name: slack-ops
      type: slack
      webhook_url: "https://hooks.slack.com/services/T0ABC/B1DEF/your-secret"
      rate_limit: 10
      rate_window_seconds: 60
      dedup_window_seconds: 600
      retry_count: 5
      retry_delay_seconds: 2
      message_template: "[{severity}] {event_type} from {source}: {summary}"

    - name: email-team
      type: email
      smtp_host: "smtp.example.com"
      smtp_port: 587
      smtp_username: "coordinare@example.com"
      smtp_password: "your-smtp-password"
      smtp_recipient: "team@example.com"
      rate_limit: 5
      rate_window_seconds: 300
      dedup_window_seconds: 3600
      retry_count: 3
      retry_delay_seconds: 5
      message_template: "Coordinare alert [{severity}]: {event_type}\n\nSource: {source}\n{summary}"

  routing:
    - event_type: card_transition
      channels: [slack-ops]
    - event_type: card_dispatched
      channels: [slack-ops]
    - event_type: card_blocked
      channels: [slack-ops, email-team]
    - event_type: card_merged
      channels: [slack-ops]
    - event_type: advocate_escalation
      channels: [slack-ops, email-team]
    - event_type: daemon_restart
      channels: [slack-ops]
    - event_type: circuit_breaker_trip
      channels: [slack-ops, email-team]
    - event_type: prolonged_idle
      channels: [email-team]
```

---

## Decision Flow

```
NotificationService.dispatch(event: NotificationEvent)
    │
    ├─ Lookup routing table for event.event_type
    │      └─ No match → record NotificationAttempt(status=unrouted); return
    │
    └─ For each matched channel (concurrently via asyncio.gather):
           │
           ├─ entry.enabled = False → skip (no history record)
           │
           ├─ dedup_key present AND DeduplicationWindow.is_duplicate(key)?
           │      └─ YES → record NotificationAttempt(status=deduplicated); continue
           │
           ├─ SlidingWindowRateLimiter.is_allowed()?
           │      └─ NO → record NotificationAttempt(status=rate_limited); continue
           │
           └─ Retry loop (up to retry_count attempts):
                  ├─ Render message: channel.message_template.format_map(event.payload)
                  ├─ Call ChannelSender.send(rendered_message)
                  ├─ Success → record NotificationAttempt(status=delivered)
                  │            DeduplicationWindow.record(dedup_key) if present
                  │            return
                  └─ Failure → asyncio.sleep(retry_delay_seconds); try again
                               After all retries: record NotificationAttempt(status=failed, error=...)
```
