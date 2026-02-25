# Quickstart: Notification & Alerting System (006)

**Feature**: 006-notification-alerting
**Date**: 2026-02-24

---

## Prerequisites

- Coordinare already running with spec 001 configuration
- Existing `config.yaml` uses the old flat `smtp_*` and `slack_*` fields — **these must be migrated** to the new `notifications:` block (see below)
- No new dependencies required — all libraries already present

---

## Config Migration

The old flat fields are replaced by a structured `notifications:` block. Copy `config.example.yaml` and fill in real values.

**Old format (spec 001, now removed)**:
```yaml
smtp_host: "smtp.example.com"
smtp_port: 587
smtp_username: "coordinare@example.com"
smtp_password: "secret"
slack_webhook_url: "https://hooks.slack.com/..."
slack_channel: "#coordinare"
notification_email: "team@example.com"
```

**New format (spec 006)**:
```yaml
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
      message_template: "[{severity}] {event_type} | {source} | {summary}"

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
      message_template: "Coordinare [{severity}] {event_type}\n\nSource: {source}\n{summary}"

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

## Running

No startup changes — the coordinare daemon picks up the new config automatically:

```bash
cd src
python -m coordinare --config ../config.yaml
```

Look for structured log entries with `event: notification_attempt` and `event: notification_unrouted`.

To run a single cycle for testing:

```bash
cd src
COORDINARE_OUTPUT_MODE=structured python -m coordinare --max-cycles 1
```

---

## Testing

Run the full notification test suite:

```bash
cd src
pytest tests/unit/services/test_notification.py -v
pytest tests/unit/models/test_notification_models.py -v
pytest tests/integration/test_notification_dispatch.py -v
pytest tests/contract/test_notification_service.py -v
```

---

## Manual End-to-End Tests

### Test 1: Card transition routed to Slack only (US1)

1. Configure routing with `card_transition → [slack-ops]` (no email entry).
2. Run one coordinare poll cycle with a card that transitions columns.
3. Verify:
   - A message appears in the configured Slack channel.
   - No email is sent.
   - A structured log entry `event: notification_attempt, status: delivered, channel: slack-ops` is present.

### Test 2: Daemon restart alert (US2)

1. Start coordinare with `daemon_restart → [slack-ops]` in routing.
2. Verify on startup:
   - A Slack message is delivered within 5 seconds of startup complete.
   - The message payload contains `event_type: daemon_restart` and `source: daemon`.

### Test 3: Rate limiting (US3)

1. Configure `slack-ops` with `rate_limit: 2, rate_window_seconds: 60`.
2. Trigger 5 card transition events within 30 seconds.
3. Verify:
   - Exactly 2 Slack messages are sent.
   - 3 entries in `NotificationHistory` have `status: rate_limited`.
   - Structured log contains `event: notification_attempt, status: rate_limited` × 3.

### Test 4: Deduplication (US3)

1. Configure `advocate-escalation → [email-team]` with `dedup_window_seconds: 600`.
2. Dispatch advocate escalation for issue #99 twice within 5 minutes using the same dedup key.
3. Verify:
   - Exactly 1 email is sent.
   - `NotificationHistory` shows 1 `delivered` and 1 `deduplicated` for issue #99.

### Test 5: Retry on failure (US1)

1. Point `webhook_url` to a URL that returns HTTP 500.
2. Configure `retry_count: 3, retry_delay_seconds: 1`.
3. Trigger a card transition.
4. Verify:
   - `NotificationHistory` shows `status: failed, retries_attempted: 3`.
   - The poll cycle completes without raising an exception.
   - Log entry `event: notification_delivery_failed, retries: 3` is present.

### Test 6: Prolonged idle alert (US2)

1. Configure `prolonged_idle → [slack-ops]` with `prolonged_idle_threshold_seconds: 60`.
2. Run coordinare with no actionable board cards for 70 seconds.
3. Verify:
   - A single Slack message is delivered (not repeated due to dedup).
   - `NotificationHistory` shows `event_type: prolonged_idle, status: delivered`.

### Test 7: Notification history query (US4)

1. Run coordinare for one cycle, generating at least one delivered, one rate-limited, and one deduplicated attempt.
2. Query `notification_service.history.query(status=NotificationStatus.delivered)` in a test.
3. Verify:
   - Only `delivered` records are returned.
   - Each record has `timestamp`, `channel_name`, `event_type`, and `elapsed_ms` populated.

---

## Disabling Notifications

To disable all notifications without config removal:

```yaml
notifications:
  channels: []
  routing: []
```

All events are logged as `unrouted` with zero latency overhead.

---

## Adding a New Event Type

No code changes required. Example — adding a `pr_review_requested` event:

1. Add `pr_review_requested` to `EventType` enum in `models/notification.py`.
2. Add a routing entry to `config.yaml`:
   ```yaml
   - event_type: pr_review_requested
     channels: [slack-ops]
   ```
3. In the node that detects this condition, call:
   ```python
   await notification_service.dispatch(NotificationEvent(
       event_type=EventType.pr_review_requested,
       severity=NotificationSeverity.info,
       source="board",
       payload={"summary": f"PR #{pr_number} is ready for review", ...},
   ))
   ```

---

## Configuration Reference

| Key | Default | Description |
|-----|---------|-------------|
| `notifications.history_max_age_hours` | `24` | In-memory history retention window |
| `notifications.prolonged_idle_threshold_seconds` | `1800` | Seconds of idle before `prolonged_idle` alert fires |
| `notifications.channels[].name` | — | Unique channel identifier |
| `notifications.channels[].type` | — | `slack` or `email` |
| `notifications.channels[].rate_limit` | `0` | Max messages per window (0 = unlimited) |
| `notifications.channels[].rate_window_seconds` | `60` | Rate limit time window |
| `notifications.channels[].dedup_window_seconds` | `600` | Dedup suppression window (10 min) |
| `notifications.channels[].retry_count` | `5` | Max delivery attempts |
| `notifications.channels[].retry_delay_seconds` | `2.0` | Fixed wait between retries |
| `notifications.channels[].message_template` | `"{event_type}: {source}"` | Python `str.format_map` template |
| `notifications.routing[].event_type` | — | Event type to match |
| `notifications.routing[].channels` | — | List of channel names to deliver to |
| `notifications.routing[].enabled` | `true` | Set `false` to disable without removing |

---

## Observability

All notification events emit structured log entries (via `structlog`):

| Event | Fields |
|-------|--------|
| `notification_attempt` | `event_type`, `channel`, `status`, `elapsed_ms` |
| `notification_unrouted` | `event_type`, `source` |
| `notification_delivery_failed` | `channel`, `event_type`, `retries`, `error` |
| `notification_channel_missing` | `channel` |

Prometheus metrics (appended to existing `metrics.py`):

| Metric | Type | Labels |
|--------|------|--------|
| `coordinare_notifications_dispatched_total` | Counter | `event_type`, `channel` |
| `coordinare_notifications_failed_total` | Counter | `channel` |
| `coordinare_notifications_rate_limited_total` | Counter | `channel` |
| `coordinare_notifications_deduplicated_total` | Counter | `channel` |
