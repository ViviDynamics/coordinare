# Feature Specification: Notification & Alerting System

**Feature Branch**: `006-notification-alerting`
**Created**: 2026-02-24
**Status**: Draft
**Input**: User description: "A unified notification and alerting system for the coordinare daemon. Extends and consolidates the ad-hoc Slack and email notifications already present in the board orchestrator (spec 001) and customer advocate (spec 007) into a structured, configurable notification layer. Adds operator-facing system alerts for health events (daemon crash, restart, circuit breaker trips from spec 005, prolonged idle), a notification routing layer that maps event types to one or more configured channels (Slack, email), per-channel rate limiting and deduplication to prevent notification storms, and a notification history log. The system should require no changes to existing node code when new alert types are added."

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Configurable Notification Routing (Priority: P1)

As an operator, I want all notifications from coordinare (card transitions, escalations, system alerts) to be routed through a single configurable routing layer, so that I can control which events reach which channels without modifying any coordinare node code.

**Why this priority**: The existing system has hardcoded Slack+email calls scattered across node code. Without a unified routing layer, every new event type requires code changes and there is no way to silence or redirect notifications without a deployment. This is the foundational capability everything else depends on.

**Independent Test**: Configure a routing table with two rules — one sending `card_transition` events to Slack only and one sending `advocate_escalation` events to email only. Trigger both events. Verify Slack receives only card_transition and email receives only advocate_escalation. No node code changes required.

**Acceptance Scenarios**:

1. **Given** the routing table has an entry mapping `card_transition` to `slack`, **When** a card transition occurs, **Then** a Slack message is sent and no email is sent.
2. **Given** the routing table has an entry mapping `advocate_escalation` to both `slack` and `email`, **When** an escalation fires, **Then** both channels receive the notification.
3. **Given** an event type with no routing entry, **When** that event fires, **Then** no notification is sent and a structured log entry records the unrouted event.
4. **Given** a new event type is added to coordinare, **When** a routing entry for it is added to `config.yaml`, **Then** it is delivered without any changes to node code.

---

### User Story 2 - Operator System Alerts (Priority: P2)

As an operator, I want to receive alerts when coordinare encounters critical health events (daemon crash or restart, circuit breaker trips, prolonged idle state), so that I am aware of operational problems before they affect end users.

**Why this priority**: Card-level notifications are business events; system health events are operational. Operators currently have no way to learn that coordinare has stopped working except by noticing downstream effects. This closes that gap.

**Independent Test**: Trigger a simulated circuit breaker trip (without sending real traffic). Verify that a system alert notification is dispatched through the configured channel with the correct severity and event type. Confirm no card-level notification is sent.

**Acceptance Scenarios**:

1. **Given** the daemon restarts, **When** startup completes, **Then** a `daemon_restart` alert is dispatched to the configured system-alert channel.
2. **Given** a circuit breaker trips (spec 005), **When** the trip event fires, **Then** a `circuit_breaker_trip` alert includes the affected service name and trip count.
3. **Given** no board activity occurs for longer than a configured idle threshold, **When** the threshold is crossed, **Then** a `prolonged_idle` alert is sent.
4. **Given** system alerts are disabled in config, **When** any health event fires, **Then** no system alert is dispatched and no error is logged.

---

### User Story 3 - Rate Limiting and Deduplication (Priority: P3)

As an operator, I want per-channel rate limiting and deduplication so that a burst of related events does not flood a Slack channel or inbox with hundreds of identical messages.

**Why this priority**: Without rate limiting, a loop or cascade failure could generate unlimited notifications. This would render Slack channels unusable and is a P3 quality-of-life requirement rather than a blocking capability.

**Independent Test**: Fire the same `circuit_breaker_trip` event 50 times within 60 seconds for Slack. Verify that Slack receives at most the configured burst limit (e.g., 5 messages per minute) and the remainder are silently dropped with structured log entries. Confirm deduplication: the same issue ID within a dedup window generates exactly one notification.

**Acceptance Scenarios**:

1. **Given** a Slack channel is configured with a rate limit of 5 messages per minute, **When** 20 events fire within 30 seconds, **Then** exactly 5 messages are sent and 15 are dropped (logged as `rate_limited`).
2. **Given** an escalation fires for issue #42 and the dedup window is 10 minutes, **When** the same escalation fires again for issue #42 within 10 minutes, **Then** the second notification is suppressed and logged as `deduplicated`.
3. **Given** the dedup window has expired, **When** the same event fires again, **Then** the notification is delivered normally.

---

### User Story 4 - Notification History (Priority: P4)

As an operator, I want a queryable notification history log so that I can audit what was sent, when, to which channel, and whether it was delivered or suppressed.

**Why this priority**: Auditing and debugging notification behaviour is a secondary operational concern. Useful for diagnosing missed alerts but not required for core functionality.

**Independent Test**: Send three notifications (one delivered, one rate-limited, one deduplicated). Query the notification history. Verify all three appear with correct status, timestamp, event type, and channel.

**Acceptance Scenarios**:

1. **Given** a notification is delivered to Slack, **When** the history is queried, **Then** an entry exists with `status=delivered`, the channel, event type, and timestamp.
2. **Given** a notification is rate-limited, **When** the history is queried, **Then** an entry exists with `status=rate_limited` and the suppression reason.
3. **Given** a notification is deduplicated, **When** the history is queried, **Then** an entry exists with `status=deduplicated` and the dedup key.

---

### Edge Cases

- What happens when the Slack API is unavailable? The system must not raise an exception into the calling node; the failure must be logged and the attempt recorded as `failed` in notification history.
- What happens when both Slack and email are unconfigured but a notification fires? The event is logged as `unrouted` with no error.
- What happens when a routing entry references a channel name not present in the channel config? The routing entry is skipped with a structured log warning; other entries for the same event are still processed.
- What happens when the rate limit config is zero or negative? The system treats it as unlimited (no rate limiting).
- What happens when a `prolonged_idle` alert itself triggers a prolonged_idle check on the next cycle? Deduplication window prevents a storm of repeated idle alerts.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The system MUST provide a `NotificationService` that all coordinare nodes use to dispatch notifications; as part of this feature's implementation, the existing direct Slack/email calls in the board orchestrator (spec 001) `notify` node and the customer advocate (spec 007) escalation path MUST be replaced with calls to `NotificationService`. This migration is in scope for spec 006 — the old direct-call code is removed, not kept in parallel.
- **FR-002**: `NotificationService` MUST accept a structured `NotificationEvent` containing: event type, severity, payload (key-value pairs), and an optional dedup key.
- **FR-003**: The system MUST maintain a routing table (`NotificationRoutingTable`) that maps event types to one or more configured channel names; the routing table MUST be defined entirely in `config.yaml` with no code changes required to add new event types.
- **FR-004**: The system MUST support at least two channel types: `slack` and `email`; each channel instance has a unique name and its own configuration (webhook URL / SMTP settings). Sensitive credential fields (webhook URLs, SMTP passwords) are stored directly in the operator-supplied `config.yaml`, which is excluded from version control. A committed `config.example.yaml` documents all required fields with placeholder values.
- **FR-005**: The system MUST dispatch notifications concurrently to all channels matched by the routing table for a given event.
- **FR-006**: The system MUST register the following operator-facing system alert event types in `EventType`: `daemon_restart`, `circuit_breaker_trip`, `prolonged_idle`. The `daemon_restart` and `prolonged_idle` dispatch calls MUST be implemented as infrastructure hooks in `daemon.py` without modifying any LangGraph node files. The `circuit_breaker_trip` dispatch call is the responsibility of spec 005, which will call `NotificationService.dispatch()` directly; this spec provides the `EventType` entry and routing table support only.
- **FR-007**: Each channel MUST support per-channel rate limiting: a configurable maximum number of notifications per configurable time window; excess notifications MUST be dropped silently (logged, not raised as errors).
- **FR-008**: Each channel MUST support deduplication: a configurable window duration; if an event with the same dedup key arrives within the window, the second notification is suppressed.
- **FR-009**: The system MUST record every notification attempt (delivered, failed, rate_limited, deduplicated, unrouted) in a `NotificationHistory` log accessible within the daemon's current session. Records older than a configurable retention window (`history_max_age_hours`, default: 24 hours) MUST be evicted automatically; Slack and email channels serve as the durable audit trail beyond that window.
- **FR-010**: When a channel delivery fails (network error, API error), the system MUST retry delivery up to a configurable maximum number of attempts (`retry_count`, default: 5) with a configurable delay between each attempt (`retry_delay_seconds`, default: 2s). After all retries are exhausted the failure MUST be recorded in `NotificationHistory` and MUST NOT propagate as an exception to the calling node. The caller is blocked for the duration of the retry window.
- **FR-011**: The `NotificationService` MUST be injectable into `CoordinareState` as a protocol-typed field so that nodes can call it without coupling to a concrete implementation.
- **FR-012**: All notification events MUST emit a structured log entry (`structlog`) with event type, channel, status, and elapsed time.
- **FR-013**: The system MUST expose Prometheus counters for: `notifications_dispatched_total` (by event type and channel), `notifications_failed_total` (by channel), `notifications_rate_limited_total` (by channel), `notifications_deduplicated_total` (by channel).
- **FR-014**: When `advocate.enabled = false`, advocate escalation event types MUST still be routable (the routing table is evaluated regardless of whether the originating subsystem is active); unmatched events are silently dropped.

### Key Entities

- **NotificationEvent**: A structured event to be dispatched. Contains: `event_type` (string enum), `severity` (info/warning/critical), `payload` (dict of string key-value pairs for message templating), `dedup_key` (optional string for deduplication), `source` (subsystem name, e.g., `"advocate"`, `"board"`, `"daemon"`).
- **NotificationRoutingTable**: The complete routing configuration. A list of `RoutingEntry` objects loaded from `config.yaml`. Immutable after startup.
- **RoutingEntry**: Maps a single event type to one or more channel names. Contains: `event_type`, `channels` (list of channel names), optional `enabled` flag to suppress without deletion.
- **ChannelConfig**: Configuration for a single notification channel instance. Contains: `name` (unique identifier), `type` (`slack` or `email`), `rate_limit` (max messages per window), `rate_window_seconds`, `dedup_window_seconds`, `retry_count` (max delivery attempts, default: 5), `retry_delay_seconds` (wait between retry attempts, default: 2), and channel-specific connection settings.
- **NotificationAttempt**: A single delivery attempt record. Contains: `attempt_id`, `event_type`, `channel_name`, `status` (delivered/failed/rate_limited/deduplicated/unrouted), `dedup_key`, `timestamp` (UTC), `elapsed_ms`, `retries_attempted` (0 if delivered on first try), `error_message` (if failed after all retries).
- **NotificationHistory**: In-memory store of `NotificationAttempt` records for the current daemon session. Queryable by event type, channel, status, and time range. Records older than `history_max_age_hours` (default: 24h) are evicted; Slack/email serve as the durable audit trail.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: All existing card-transition and advocate-escalation notifications continue to be delivered after migration to the unified routing layer, with no regressions in delivery behaviour.
- **SC-002**: Adding a new notification event type requires only a `config.yaml` change; zero node code files are modified.
- **SC-003**: A burst of 50 identical events within 60 seconds to a rate-limited channel results in at most the configured limit being delivered and the remainder recorded as `rate_limited` in notification history.
- **SC-004**: Two notifications with the same dedup key within the configured dedup window result in exactly one delivery; the second is recorded as `deduplicated`.
- **SC-005**: When a channel delivery fails, the system retries up to the configured `retry_count` with `retry_delay_seconds` between each attempt; after all retries are exhausted the failure is recorded in notification history and the poll cycle continues without raising an exception.
- **SC-006**: `daemon_restart` and `prolonged_idle` alerts are dispatched by infrastructure hooks in `daemon.py` without any modification to existing LangGraph node files; `circuit_breaker_trip` is registered in `EventType` and routable via `config.yaml`, with its dispatch call implemented in spec 005 (out of scope for this spec's acceptance tests).
- **SC-007**: Notification history for the current session is accessible and contains accurate status, channel, event type, and timestamp for every attempt.
- **SC-008**: All four Prometheus metrics are present and incrementing correctly under normal operation.
- **SC-009**: Per-channel dispatch latency MUST NOT exceed `retry_count × retry_delay_seconds` seconds (default ceiling: 5 × 2s = 10s); with concurrent multi-channel dispatch via `asyncio.gather`, worst-case poll cycle overhead equals the slowest channel's retry window; operators MUST configure `retry_count × retry_delay_seconds < poll_interval_seconds` for latency-sensitive deployments.

## Assumptions

- **A-001**: Slack connectivity uses the existing `httpx` integration already present in spec 001 — `SlackService` delivers webhook payloads via `httpx.AsyncClient.post()` directly; there is no `slack-sdk` dependency (see research.md R1 for the correction of this assumption from the original description). No new Slack library is introduced.
- **A-002**: Email delivery uses the existing `aiosmtplib` integration already present in spec 001; no new email library is introduced.
- **A-003**: Notification history is in-memory only for this spec; cross-session persistence is deferred to spec 003 (persistent storage layer).
- **A-004**: Rate limiting uses a simple sliding window counter held in memory; Redis or external state is not required.
- **A-005**: The `prolonged_idle` threshold is configurable and defaults to 30 minutes of no board activity.
- **A-006**: Circuit breaker trip events are emitted by the circuit breaker infrastructure (spec 005); this spec consumes those events via the `NotificationService` dispatch call, which spec 005 will call directly — no new inter-spec coupling mechanism is introduced.
- **A-007**: Message templates for each event type are defined in `config.yaml` as format strings; payload key-value pairs are substituted at dispatch time.
- **A-008**: The `NotificationService` is instantiated once at daemon startup and injected into `CoordinareState`; it is not re-created per poll cycle.
- **A-009**: Retry delay is a fixed wait between attempts (not exponential back-off) for V1 simplicity; exponential back-off may be introduced in a later spec.
- **A-010**: `NotificationHistory` eviction runs lazily (e.g., on each new record insertion or query), not on a background timer, to avoid introducing a separate background task.

## Clarifications

### Session 2026-02-24

- Q: Should `NotificationService.dispatch()` block the caller or fire-and-forget? → A: Awaited with bounded retry — caller blocks while the system retries up to `retry_count` times with `retry_delay_seconds` between each attempt; after retries exhausted, error is logged and execution continues without raising.
- Q: Is the migration of existing direct Slack/email calls (spec 001 + spec 007) in scope for spec 006? → A: Yes — migration is in scope; old direct-call code in the spec 001 `notify` node and spec 007 escalation path is removed and replaced as part of this implementation.
- Q: How are channel credentials (Slack webhook URLs, SMTP passwords) stored? → A: Directly in the gitignored `config.yaml`; a committed `config.example.yaml` documents required fields with placeholder values. No env-var interpolation or separate secrets file required.
- Q: Should `NotificationHistory` have a capacity cap? → A: Time-based retention — records older than `history_max_age_hours` (default: 24h) are evicted lazily; Slack/email are the durable audit trail so in-memory history is purely for same-session debugging.
