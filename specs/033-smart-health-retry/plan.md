# Implementation Plan: Smart Health-Check Retry

**Branch**: `033-smart-health-retry` | **Date**: 2026-03-24 | **Spec**: [spec.md](./spec.md)

## Summary

Add exponential-backoff retry to the health check in `dispatch_performer`. When the performer returns "unreachable" or "unknown", the node retries up to a configurable number of times before falling back to idle. Two files modified; approximately 8 new unit tests. No new dependencies.

## Technical Context

**Language/Version**: Python 3.12+ | **Dependencies**: asyncio, structlog, pydantic-settings (all existing)
**Storage**: N/A | **Testing**: pytest + pytest-asyncio | **Scale**: ~2 modified files, ~8 new tests

## Constitution Check

| Principle | Status | Notes |
|-----------|--------|-------|
| I. Code Quality First | PASS | Single helper function; no role-specific branching |
| II. Testing Discipline | PASS | All retry scenarios independently testable |
| III. User Experience | N/A | No UI changes |
| IV. Performance by Design | PASS | Worst-case 7s latency vs. wasting a 30-60s poll cycle |
| V. Clarity Before Action | PASS | No open clarifications |

## Source Code (files changed)

```text
src/coordinare/config.py                          # MODIFIED -- add HealthCheckConfig
src/coordinare/graph/nodes/dispatch_performer.py  # MODIFIED -- add retry loop
tests/unit/graph/nodes/test_dispatch_performer.py # MODIFIED -- ~8 new tests
```

## Detailed Implementation Plan

### Step 1 -- Add HealthCheckConfig (`config.py`)

New model with `retries: int = 3` (ge=1) and `backoff_seconds: float = 1.0` (ge=0). Added to `ProjectConfiguration` as `health_check: HealthCheckConfig = HealthCheckConfig()`.

### Step 2 -- Retry helper in `dispatch_performer.py`

Extract the health-check block into `_health_check_with_retry(service, config)`. For each attempt (1..retries): call `check_health()`, treat exceptions as "unreachable". If status is not in ("unreachable", "unknown"), return it (log success if attempt > 1). If retryable and not last attempt, `await asyncio.sleep(backoff * 2^(attempt-1))`. After exhaustion, log `health_check_retries_exhausted` with attempts/elapsed/final_status and return the final status. Replace the inline health-check in `dispatch_performer()` with a call to this helper.

### Step 3 -- Tests

| # | Scenario | Expected |
|---|----------|----------|
| 1 | Healthy on first attempt | No retry, dispatch proceeds |
| 2 | Unreachable x2 then healthy | Dispatch proceeds after retries |
| 3 | Unknown on all attempts | Phase = idle after exhaustion |
| 4 | Error on first attempt | Phase = blocked, no retry |
| 5 | Exception then healthy | Dispatch proceeds |
| 6 | retries=1 with unreachable | Phase = idle, no retry |
| 7 | CancelledError during sleep | Propagated |
| 8 | backoff_seconds=0 | Zero delay between retries |

## Complexity Tracking

| Change | Scope | Justification |
|--------|-------|---------------|
| `_health_check_with_retry` | ~30 LOC | Single-purpose; testable in isolation |
| HealthCheckConfig | ~5 LOC | Standard Pydantic pattern |
