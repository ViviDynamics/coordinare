# Research: Notification & Alerting System (006)

**Feature**: 006-notification-alerting
**Date**: 2026-02-24

---

## R1: Spec Assumption Correction — Slack Library

**Decision**: `SlackService` uses `httpx` for Slack webhook delivery, **not** `slack-sdk` as stated in spec assumption A-001.

**Finding**: Reading `src/coordinare/services/slack.py` confirms that `SlackService.send_notification()` posts directly to the webhook URL via `httpx.AsyncClient`. There is no `slack-sdk` import in the project.

**Impact on spec**: Assumption A-001 ("uses the existing `slack-sdk` integration") is factually incorrect. The correct statement is "uses the existing `httpx`-based `SlackService`." This has no impact on implementation approach — `httpx` is already present and the channel adapter will continue to use it.

**Correction applied**: Research resolves this. No spec re-edit required for planning; the data-model and contracts will use the correct library.

**Alternatives considered**: Migrating to `slack-sdk`'s async client was evaluated and rejected — no functional benefit, adds a dependency, and breaks the no-new-dependencies constraint.

---

## R2: Channel Adapter Pattern

**Decision**: `NotificationService` owns a registry of `ChannelSender` adapters, one per configured channel instance. Each adapter wraps the low-level delivery (httpx for Slack, aiosmtplib for email) and exposes a single `async def send(text: str) -> None` method.

**Rationale**: This separates message routing/retry/rate-limit logic (in `NotificationService`) from raw transport (in `ChannelSender` subclasses). The existing `SlackService` and `EmailService` are modified to expose a simpler low-level `send()` method that takes pre-formatted text. The `NotificationService` handles template rendering before calling send.

**Message rendering**: `NotificationEvent.payload` is a `dict[str, str]`. The template string (from `config.yaml`) is rendered via `template.format_map(event.payload)` — standard library, no Jinja2 dependency.

**Alternatives considered**:
- Keep `SlackService`/`EmailService` as-is and have `NotificationService` convert `NotificationEvent → Notification` before calling their existing methods. Rejected: `Notification` is card-specific and would need to become generic — awkward design.
- Have `NotificationService` directly own all `httpx`/`aiosmtplib` calls, eliminating `SlackService`/`EmailService`. Rejected: removes existing tested code unnecessarily.

---

## R3: Rate Limiting Implementation

**Decision**: Per-channel sliding window using a `collections.deque` of `datetime` timestamps. On each send attempt, evict timestamps older than `rate_window_seconds`, then check if `len(deque) >= rate_limit`. If so, drop and log. Otherwise append timestamp and proceed.

**Rationale**: `deque` is O(1) for append and popleft. No external state, no background timer. Accurate sliding window (not a leaky bucket). Already handles the zero/negative rate_limit edge case: treat as unlimited (no deque check).

**Alternatives considered**:
- Token bucket: more complex, same asymptotic behaviour for low-volume daemon. Rejected.
- Simple counter + window-start timestamp: inaccurate at window boundaries (bursty). Rejected.
- Redis-backed counter: violates no-external-deps constraint. Rejected.

---

## R4: Deduplication Implementation

**Decision**: Per-channel `dict[str, datetime]` mapping dedup key → last dispatch timestamp. On each send attempt, check if key exists and `now - last_dispatch < dedup_window_seconds`. If so, suppress and log. Otherwise, record timestamp and proceed. Lazy cleanup: entries older than 2× the dedup window are evicted on each insert to prevent unbounded dict growth.

**Rationale**: Simple, correct, no external deps. Lazy eviction bounds memory without a background task (aligns with assumption A-010 from spec).

**Alternatives considered**: LRU cache with TTL: adds complexity. Rejected.

---

## R5: Retry Implementation

**Decision**: Synchronous retry loop (within the async context) with a fixed `asyncio.sleep(retry_delay_seconds)` between attempts. Caller awaits the full retry window. After `retry_count` failures, record the final error in `NotificationHistory` and return without raising.

**Rationale**: The clarified dispatch model is "awaited with bounded retry" (spec clarification Q1). `asyncio.sleep` yields control to the event loop between retries, so the daemon is not hard-blocked. Max overhead = `retry_count × retry_delay_seconds` per channel, dispatched concurrently across channels via `asyncio.gather`.

**Retry scope**: One retry sequence per channel per dispatch call. If channel A and channel B both fail, each retries independently in parallel.

**Alternatives considered**:
- Exponential backoff: noted in spec assumption A-009 as deferred to a later spec. Not implemented here.
- Gather with `return_exceptions=True` and no retry: the existing `notify.py` pattern. Rejected — the clarification explicitly adds retry.

---

## R6: `NotificationHistory` Storage

**Decision**: Plain `list[NotificationAttempt]` on `NotificationService` instance. Lazy eviction: on each append, scan from the front and remove records where `now - record.timestamp > history_max_age_hours * 3600`. Since records are appended in time order, `itertools.dropwhile` (or a while loop checking index 0) is O(evicted) which is typically O(0) in steady state.

**Rationale**: Simple, standard library, no background task (aligns with assumption A-010). Records are always appended in chronological order so eviction is a front-trimming operation.

**Alternatives considered**: `collections.deque(maxlen=N)` (count-based cap): rejected in favour of time-based (spec clarification Q4). heapq: overkill for append-only ordered data.

---

## R7: System Alert Hook Architecture

**Decision**: `daemon.py` directly dispatches `NotificationEvent` via `self._state.get("notification_service")` at three injection points:

1. **`daemon_restart`**: At the end of `start()` startup preamble, immediately after `METRICS.up.set(1)` call in `__main__.py`. Specifically, `_bootstrap_services()` in `__main__.py` wires `notification_service` into state before `daemon.start()` is called, so the service is available when `start()` runs. The dispatch happens at the top of the `start()` loop before the first cycle.

2. **`prolonged_idle`**: Inside the `start()` loop, track `last_activity_at = monotonic()`. Reset on any state change away from `"idle"`. If `monotonic() - last_activity_at > idle_threshold_seconds`, dispatch `prolonged_idle` alert. Use dedup key `"prolonged_idle"` with the configured dedup window to prevent repeated alerts.

3. **`circuit_breaker_trip`**: Dispatched by spec 005 infrastructure when it calls `notification_service.dispatch(NotificationEvent(event_type="circuit_breaker_trip", ...))` directly. No changes to `daemon.py` for this event type.

**Rationale**: Hooks live in `daemon.py` (not in node code), satisfying FR-006 ("existing LangGraph nodes MUST NOT require modification"). `notification_service` is already on state when these hooks fire. The `idle_threshold_seconds` comes from `config.prolonged_idle_threshold_seconds` (new field, default: 1800 = 30 min, per spec assumption A-005).

**Alternatives considered**: A separate `alert_hook` LangGraph node: rejected — would require graph modification and couples alert delivery to the graph lifecycle rather than the daemon lifecycle.

---

## R8: Config Model Migration

**Decision**: `ProjectConfiguration` gains a nested `NotificationsConfig` model. The old flat fields (`smtp_host`, `smtp_port`, `smtp_username`, `smtp_password`, `slack_webhook_url`, `slack_channel`, `notification_email`) are **removed** from `ProjectConfiguration` and replaced by `notifications.channels` list entries. This is a breaking config change — `config.example.yaml` is updated to reflect the new structure.

**New top-level config field**: `notifications: NotificationsConfig` (optional, defaults to empty/disabled).

**`NotificationsConfig`** contains:
- `channels`: `list[ChannelConfig]` — each with `name`, `type`, connection settings, rate limit params, retry params
- `routing`: `list[RoutingEntry]` — event_type → channel name(s)
- `history_max_age_hours`: `int` (default: 24)
- `prolonged_idle_threshold_seconds`: `int` (default: 1800)

**Validators**: When any channel of type `slack` is present, validate `webhook_url` is non-empty. When any channel of type `email`, validate `smtp_host` is non-empty.

**Alternatives considered**: Keep old flat fields as deprecated aliases and add new `notifications` block: rejected — adds complexity, contradicts FR-001 (migration is in scope, old code is removed).

---

## R9: `CoordinareState` Protocol Update

**Decision**: Replace `email_service: NotificationServiceProtocol` and `slack_service: NotificationServiceProtocol` with a single `notification_service: NotificationServiceProtocol`. Update the protocol:

```python
class NotificationServiceProtocol(Protocol):
    async def dispatch(self, event: NotificationEvent) -> None: ...
```

The old `send_notification(*args, **kwargs)` protocol is retired along with the old state fields.

**`notify.py` node migration**: The node builds a `NotificationEvent` from card state fields and calls `notification_service.dispatch(event)`. The `Notification` model (old, card-specific) is replaced by `NotificationEvent` in the models file, or kept temporarily and mapped — but since migration is in scope, `Notification` is retired and `NotificationEvent` takes its place.

**Alternatives considered**: Keep both `email_service`/`slack_service` and add `notification_service` in parallel: rejected — duplicates, contradicts FR-001 requirement to remove old code.
