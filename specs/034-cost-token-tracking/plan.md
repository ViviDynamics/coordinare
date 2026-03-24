# Implementation Plan: Cost & Token Tracking

**Branch**: `034-cost-token-tracking` | **Date**: 2026-03-24 | **Spec**: [spec.md](./spec.md)

## Summary

Track per-card AI token usage and estimated cost across the performer lifecycle. Accumulate `tokens_processed` from performer status responses in `monitor_performer`, expose via Prometheus metrics with per-role labels, display on the dashboard, and optionally alert when a per-card cost budget is exceeded. Four files modified; approximately 12 new unit tests. No new dependencies.

## Technical Context

**Language/Version**: Python 3.12+ | **Dependencies**: prometheus-client, structlog, pydantic-settings, FastAPI (all existing)
**Storage**: N/A -- in-memory on CoordinareState | **Testing**: pytest + pytest-asyncio | **Scale**: ~4 modified files, ~12 new tests

## Constitution Check

| Principle | Status | Notes |
|-----------|--------|-------|
| I. Code Quality First | PASS | Pure arithmetic addition to existing monitor flow |
| II. Testing Discipline | PASS | Accumulation, dashboard, and budget alert each independently testable |
| III. User Experience | PASS | Dashboard panel provides at-a-glance cost visibility |
| IV. Performance by Design | PASS | Zero additional I/O; O(1) arithmetic per poll |
| V. Clarity Before Action | PASS | No open clarifications |

## Source Code (files changed)

```text
src/coordinare/config.py                              # MODIFIED -- CostTrackingConfig
src/coordinare/graph/state.py                         # MODIFIED -- card_tokens_total, card_cost_estimate, card_budget_alert_sent
src/coordinare/graph/nodes/monitor_performer.py       # MODIFIED -- accumulate tokens, cost, budget alert
src/coordinare/graph/nodes/dispatch_performer.py      # MODIFIED -- reset counters on new card
src/coordinare/metrics.py                             # MODIFIED -- token counter + cost gauge
src/coordinare/dashboard.py                           # MODIFIED -- token usage panel
tests/unit/graph/nodes/test_monitor_performer.py     # MODIFIED -- ~6 new tests
tests/unit/graph/nodes/test_dispatch_performer.py    # MODIFIED -- ~2 new tests
tests/unit/test_dashboard.py                         # MODIFIED -- ~2 new tests
tests/unit/test_metrics.py                           # MODIFIED -- ~2 new tests
```

## Detailed Implementation Plan

### Step 1 -- CostTrackingConfig (`config.py`)

New model: `cost_per_million_tokens: float = 3.0` and `cost_budget_per_card: float | None = None`. Added to `ProjectConfiguration` as `cost_tracking: CostTrackingConfig`.

### Step 2 -- Extend CoordinareState (`state.py`)

Add `card_tokens_total: int`, `card_cost_estimate: float`, `card_budget_alert_sent: bool`. Update `initial_state()` with zero defaults.

### Step 3 -- Prometheus Metrics (`metrics.py`)

Add `coordinare_card_tokens_total` Counter with `role` label and `coordinare_card_cost_estimate_dollars` Gauge.

### Step 4 -- Token Accumulation (`monitor_performer.py`)

After the existing `performer_metrics` storage, extract `tokens_processed` (clamped to >= 0). If positive: accumulate into `card_tokens_total`, recalculate `card_cost_estimate`, increment Prometheus counter with role label, update gauge. If budget configured and exceeded and not yet alerted, dispatch a notification and set `card_budget_alert_sent = True`.

### Step 5 -- Counter Reset (`dispatch_performer.py`)

In the dispatch success block, reset `card_tokens_total = 0`, `card_cost_estimate = 0.0`, `card_budget_alert_sent = False`, and set Prometheus gauge to 0.

### Step 6 -- Dashboard Panel (`dashboard.py`)

Add "Token Usage" section showing total tokens (comma-formatted) and estimated cost (dollar-formatted) from DashboardStore state.

## Complexity Tracking

| Change | Scope | Justification |
|--------|-------|---------------|
| Token accumulation | ~15 LOC | Arithmetic; no role branching |
| Budget alert helper | ~15 LOC | Mirrors existing notification patterns |
| Dashboard panel | ~10 LOC | Reuses existing template |
| Config + state | ~10 LOC | Standard Pydantic pattern |
