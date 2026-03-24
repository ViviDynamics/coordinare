# Feature Specification: Smart Health-Check Retry

**Feature Branch**: `033-smart-health-retry`
**Created**: 2026-03-24
**Status**: Draft

## Overview

When `dispatch_performer` performs a health check and receives "unreachable" or "unknown", it immediately sets `phase = "idle"`, causing the coordinare to abandon the dispatch attempt until the next poll cycle. This is wasteful when the performer is transiently unavailable (e.g. container starting up, network blip). This feature adds configurable retry-with-exponential-backoff to the health check in `dispatch_performer`, giving transient failures time to resolve before the coordinare gives up. Permanent failures (e.g. container crashed, image missing) are still surfaced promptly.

## Clarifications

### Session 2026-03-24

- Q: Should retries block the coordinare event loop? -> A: No. Use `asyncio.sleep` between retries so the loop remains cooperative, but the dispatch node itself awaits the full retry sequence before returning.
- Q: Should the retry loop apply to all health statuses? -> A: Only to "unreachable" and "unknown". An "error" status is treated as a permanent failure and is not retried (it already routes to "blocked").
- Q: Where do the config values live? -> A: In `ProjectConfiguration` under a new `health_check` section, not per-role. All roles share the same retry policy.

## User Scenarios & Testing *(mandatory)*

### User Story 1 -- Configurable Retry for Transient Health Failures (Priority: P1)

When `dispatch_performer` gets an "unreachable" or "unknown" health status, it retries the health check up to `health_check_retries` times (default 3) with exponential backoff (`health_check_backoff_seconds`, default 1.0 -- delays of 1s, 2s, 4s). If any retry succeeds, dispatch proceeds normally. If all retries are exhausted, the phase is set to "idle" as before.

**Why this priority**: Without retry, every transient health blip wastes an entire poll cycle. This is the minimum viable improvement.

**Independent Test**: Mock `service.check_health()` to return "unreachable" twice then "healthy" on the third call. Verify dispatch proceeds and that `asyncio.sleep` was called with the expected backoff delays (1.0, 2.0).

**Acceptance Scenarios**:

1. **Given** a performer health check returns "unreachable" twice then "healthy", **When** `dispatch_performer` runs with `health_check_retries=3`, **Then** the dispatch proceeds and the card moves to "monitoring_performer".
2. **Given** a performer health check returns "unknown" on all 3 attempts, **When** `dispatch_performer` runs with `health_check_retries=3`, **Then** `phase` is set to "idle" after the final attempt.
3. **Given** `health_check_retries=1` (retries disabled), **When** a health check returns "unreachable", **Then** `phase` is set to "idle" immediately with no retry.
4. **Given** a health check returns "error" on the first attempt, **When** `dispatch_performer` runs, **Then** the card is blocked immediately with no retry.

---

### User Story 2 -- Distinguish Transient from Permanent Failures (Priority: P2)

When all retries are exhausted, the coordinare logs a structured warning that includes the number of attempts, total elapsed time, and the final health status. This gives operators visibility into whether failures are transient (resolved on retry) or persistent (exhausted all retries).

**Why this priority**: Observability is important but not blocking -- operators can still see failures in existing logs without this enhancement.

**Independent Test**: Mock `service.check_health()` to always return "unreachable". Verify the final log entry includes `attempts`, `elapsed_seconds`, and `final_status` fields.

**Acceptance Scenarios**:

1. **Given** all retry attempts are exhausted, **When** the coordinare logs the failure, **Then** the log includes `health_check_retries_exhausted` event with `attempts`, `elapsed_seconds`, and `final_status`.
2. **Given** a retry succeeds on attempt 2, **When** the coordinare logs the success, **Then** the log includes `health_check_retry_succeeded` event with `attempt` and `elapsed_seconds`.

---

### Edge Cases

- What if `health_check_backoff_seconds` is set to 0? (Allow it; all retries fire immediately.)
- What if the health check raises an exception on some attempts and returns a dict on others? (Treat exceptions as "unreachable".)
- What if the coordinare is shut down (CancelledError) during a retry sleep? (Propagate CancelledError immediately, do not suppress.)

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: `dispatch_performer` MUST retry health checks that return "unreachable" or "unknown" up to `health_check_retries` times (configurable, default 3).
- **FR-002**: The backoff between retries MUST be exponential: `health_check_backoff_seconds * 2^(attempt - 1)`.
- **FR-003**: Health checks that return "error" MUST NOT be retried -- the existing "blocked" handling MUST apply immediately.
- **FR-004**: `asyncio.CancelledError` during a retry sleep MUST propagate without being caught.
- **FR-005**: A structured log entry MUST be emitted when retries are exhausted, including attempt count, elapsed time, and final status.
- **FR-006**: A structured log entry MUST be emitted when a retry succeeds, including the successful attempt number and elapsed time.
- **FR-007**: Configuration MUST be added to `ProjectConfiguration` under a `health_check` section with fields `retries` (int, default 3) and `backoff_seconds` (float, default 1.0).

### Key Entities

- **HealthCheckConfig**: New Pydantic model holding `retries` and `backoff_seconds`.
- **dispatch_performer**: Existing node modified to include the retry loop.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A performer that takes 3 seconds to start up is successfully dispatched without waiting for the next poll cycle (assuming default 3 retries with 1s base backoff).
- **SC-002**: A permanently unreachable performer still transitions to "idle" within `retries * max_backoff` seconds, not blocking the coordinare indefinitely.
- **SC-003**: Zero behavioral change when `health_check_retries=1` (backward compatible with no retry).

## Assumptions

- The health check is a lightweight operation (sub-second) -- retry delays dominate the wall-clock cost.
- All performer roles share the same health-check retry policy. Per-role retry configuration is out of scope.
- The retry loop runs within a single `dispatch_performer` invocation -- it does not span multiple graph cycles.
