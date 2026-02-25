# Implementation Plan: Notification & Alerting System

**Branch**: `006-notification-alerting` | **Date**: 2026-02-24 | **Spec**: [spec.md](./spec.md)
**Input**: Feature specification from `/specs/006-notification-alerting/spec.md`

## Summary

Replace the hardcoded dual Slack+email dispatch in the `notify` node and the ad-hoc notification calls in spec 007 (customer advocate) with a unified `NotificationService` that routes `NotificationEvent` objects through a configurable routing table, applies per-channel rate limiting and deduplication, retries failed deliveries up to a configurable bound, records all attempts in a time-bounded in-memory `NotificationHistory`, and exposes three operator-facing system alert hooks (`daemon_restart`, `circuit_breaker_trip`, `prolonged_idle`). No LangGraph node code changes are required to add new event types after this feature is delivered.

## Technical Context

**Language/Version**: Python 3.12+
**Primary Dependencies**: `httpx` (Slack webhook delivery — already present, not `slack-sdk`), `aiosmtplib` (email delivery — already present), `pydantic` + `pydantic-settings` (config model), `structlog` (structured logging), `prometheus-client` (metrics) — all existing; **no new dependencies required**
**Storage**: In-memory only (`NotificationHistory` as a plain list on `NotificationService`; lazy time-based eviction). No DB.
**Testing**: `pytest` + `pytest-asyncio`; mocks via `unittest.mock.AsyncMock` (standard library)
**Target Platform**: Python async daemon (Linux server); same as spec 001
**Project Type**: Single project (`src/`, `tests/`)
**Performance Goals**: Maximum dispatch latency per event = `retry_count × retry_delay_seconds` per channel (default: 5 × 2s = 10s). With concurrent channel dispatch via `asyncio.gather`, worst-case poll cycle overhead = 10s (single slowest channel). Operators must configure `retry_count × retry_delay_seconds < poll_interval_seconds` for latency-sensitive deployments.
**Constraints**: No new external dependencies; in-memory state only; no persistent history; all delivery failures silenced from calling nodes

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

| Principle | Status | Notes |
|-----------|--------|-------|
| I. Code Quality | ✅ PASS | `NotificationService` has single clear responsibility: route, rate-limit, retry, record. Channel senders are thin adapters. No dead code — old `email_service`/`slack_service` state fields removed in migration. |
| II. Testing | ✅ PASS | Unit tests for `NotificationService`, rate limiter, dedup, retry logic; integration test for full dispatch cycle; contract tests for `NotificationServiceProtocol` and `ChannelSenderProtocol`. |
| III. UX Consistency | ✅ N/A | Daemon, no user-facing UI. Structured log patterns remain consistent with existing `runtime_event` emit style. |
| IV. Performance | ⚠️ NOTE | No explicit ms-level latency budget in spec (SC-005 is functional, not quantitative). Acceptable: retry window is bounded by config; documented in Technical Context above. Operators configure to fit within `poll_interval_seconds`. |
| V. Clarity Before Action | ✅ PASS | All four clarifications resolved before planning. No `NEEDS CLARIFICATION` markers remain. |

**Post-Design Re-check**: See research decisions R1–R9. No new violations introduced.

## Project Structure

### Documentation (this feature)

```text
specs/006-notification-alerting/
├── plan.md              # This file
├── research.md          # Phase 0 output
├── data-model.md        # Phase 1 output
├── quickstart.md        # Phase 1 output
├── contracts/           # Phase 1 output
│   └── notification-service.md
└── tasks.md             # Phase 2 output (/speckit.tasks — NOT created by /speckit.plan)
```

### Source Code (repository root)

```text
src/coordinare/
├── config.py                          # MODIFIED: add NotificationsConfig nested model;
│                                      #   remove flat smtp_* and slack_* fields
├── daemon.py                          # MODIFIED: daemon_restart hook on startup;
│                                      #   prolonged_idle tracking per poll cycle
├── metrics.py                         # MODIFIED: add 4 new notification counters
│                                      #   (dispatched, failed, rate_limited, deduplicated)
├── models/
│   └── notification.py                # MODIFIED: add NotificationEvent, NotificationAttempt,
│                                      #   NotificationHistory, EventType, NotificationStatus enums;
│                                      #   retire old Notification model (replaced by NotificationEvent)
├── graph/
│   ├── state.py                       # MODIFIED: replace email_service + slack_service with
│                                      #   notification_service: NotificationServiceProtocol;
│                                      #   update protocol signature to dispatch(event)
│   └── nodes/
│       └── notify.py                  # MODIFIED: call notification_service.dispatch(NotificationEvent)
│                                      #   instead of direct email/slack calls
├── services/
│   ├── notification.py                # NEW: NotificationService (routing, retry, rate limiting,
│                                      #   dedup, history); SlackChannelSender; EmailChannelSender;
│                                      #   ChannelSenderProtocol
│   ├── slack.py                       # MODIFIED: add low-level send(text: str) method;
│                                      #   retire send_notification(Notification)
│   └── email.py                       # MODIFIED: add low-level send(recipient, subject, body) method;
│                                      #   retire send_notification(Notification)
└── __main__.py                        # MODIFIED: bootstrap NotificationService from NotificationsConfig;
                                       #   remove old EmailService/SlackService direct wiring to state

tests/
├── unit/
│   ├── services/
│   │   └── test_notification.py       # NEW: NotificationService, rate limiter, dedup, retry, history
│   └── models/
│       └── test_notification_models.py # NEW: NotificationEvent, NotificationAttempt, enums validation
├── integration/
│   └── test_notification_dispatch.py  # NEW: full routing + delivery cycle end-to-end with mocked channels
└── contract/
    └── test_notification_service.py   # NEW: NotificationServiceProtocol compliance; ChannelSenderProtocol
```

**Structure Decision**: Single project layout (`src/`, `tests/`). All new code in `src/coordinare/services/notification.py` (new file) and modifications to existing files listed above.

## Complexity Tracking

No constitution violations requiring justification.
