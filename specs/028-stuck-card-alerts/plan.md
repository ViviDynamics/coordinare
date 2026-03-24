# Implementation Plan: Stuck Card Alerts

**Branch**: `028-stuck-card-alerts` | **Date**: 2026-03-24 | **Spec**: [spec.md](./spec.md)

## Summary

Add stuck-card detection to the daemon loop. A `phase_entered_at` timestamp on `CoordinareState` is set on every phase transition. A `check_stuck_card` helper runs each cycle: if elapsed time in a non-idle phase exceeds a configurable threshold, a `card_stuck` notification is emitted. Per-phase threshold overrides supported via config. No new dependencies required.

## Technical Context

**Language/Version**: Python 3.12+
**Primary Dependencies**: structlog (existing), pydantic-settings (existing) -- no new dependencies
**Storage**: `phase_entered_at` persisted via existing `StateStore`
**Testing**: pytest + pytest-asyncio (existing)
**Scale/Scope**: 3 modified files, ~10 new unit tests; no new service dependencies

## Constitution Check

| Principle | Status | Notes |
|-----------|--------|-------|
| I. Code Quality First | PASS | Stuck detection is a single pure helper |
| II. Testing Discipline | PASS | Threshold logic, per-phase overrides, and dedup independently testable |
| III. User Experience | PASS | Operator receives actionable notification |
| IV. Performance by Design | PASS | Single timestamp comparison per cycle |
| V. Clarity Before Action | PASS | No open clarifications |

## Source Code (files changed)

```text
src/coordinare/config.py                          # MODIFIED -- StuckThresholdsConfig, stuck_threshold_seconds
src/coordinare/graph/state.py                     # MODIFIED -- phase_entered_at, last_stuck_notified_at
src/coordinare/daemon.py                          # MODIFIED -- phase tracking + check_stuck_card call
src/coordinare/models/notification.py             # MODIFIED -- card_stuck EventType
tests/unit/test_daemon.py                        # MODIFIED -- phase_entered_at tracking tests
tests/unit/test_stuck_card.py                    # NEW -- stuck detection logic tests (~10 tests)
tests/unit/test_config.py                        # MODIFIED -- StuckThresholdsConfig parsing tests
```

## Detailed Implementation Plan

### Step 1 -- Config Model (`src/coordinare/config.py`)

Add `StuckThresholdsConfig(BaseModel)` with one `int = 0` field per monitored phase (`dispatching`, `monitoring_agent`, `monitoring_performer`, `monitoring_pr`, `blocked`, `relay_feedback`, `merging`). A value of 0 means "use global default". Add `stuck_threshold_seconds: int = 1800` and `stuck_thresholds: StuckThresholdsConfig` to `ProjectConfiguration`.

### Step 2 -- Extend CoordinareState (`src/coordinare/graph/state.py`)

Add `phase_entered_at: datetime | None` and `last_stuck_notified_at: datetime | None`. Initialize both in `initial_state()` (`datetime.now(UTC)` and `None` respectively).

### Step 3 -- Add `card_stuck` Event Type (`src/coordinare/models/notification.py`)

Add `card_stuck = "card_stuck"` to the `EventType` enum.

### Step 4 -- Phase Tracking in Daemon (`src/coordinare/daemon.py`)

After each graph step, compare new phase to previous phase. On change, set `phase_entered_at = now` and clear `last_stuck_notified_at`.

### Step 5 -- Stuck Detection (`src/coordinare/daemon.py`)

Add `check_stuck_card(state, config)` async helper. Skip `idle` and `system_error` phases. Resolve effective threshold: use per-phase value if > 0, else global default. If `now - phase_entered_at > threshold` and dedup window has elapsed, emit a `card_stuck` notification with `dedup_key=f"stuck:{card_id}:{phase}"` and update `last_stuck_notified_at`. Call at end of each daemon cycle.

## Complexity Tracking

| Change | Scope | Justification |
|--------|-------|---------------|
| `StuckThresholdsConfig` | ~10 LOC | One int field per phase |
| Phase tracking | ~4 LOC | Single timestamp update in daemon loop |
| `check_stuck_card` helper | ~30 LOC | Threshold comparison + notification dispatch |
| `card_stuck` event type | 1 LOC | New enum member |
