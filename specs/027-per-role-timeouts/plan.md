# Implementation Plan: Per-Role Timeouts

**Branch**: `027-per-role-timeouts` | **Date**: 2026-03-24 | **Spec**: [spec.md](./spec.md)

## Summary

Wire the existing `PerformerRoleConfig.timeout_seconds` field through to the `monitor_performer` node so each performer role enforces its own timeout. Build a `role_timeouts` mapping at startup for fast runtime lookup. Update the dashboard SSE payload to include the active role's timeout alongside elapsed time. No new dependencies required.

## Technical Context

**Language/Version**: Python 3.12+
**Primary Dependencies**: pydantic-settings (existing), structlog (existing), FastAPI (existing) -- no new dependencies
**Storage**: N/A -- timeouts derived from config at startup
**Testing**: pytest + pytest-asyncio (existing)
**Scale/Scope**: 2 modified files, ~8 new unit tests; no new service dependencies

## Constitution Check

| Principle | Status | Notes |
|-----------|--------|-------|
| I. Code Quality First | PASS | Timeout resolution is a single dict lookup |
| II. Testing Discipline | PASS | Per-role timeout, fallback, and enforcement independently testable |
| III. User Experience | PASS | Dashboard shows elapsed time and timeout per role |
| IV. Performance by Design | PASS | Dict lookup + timestamp comparison; negligible cost |
| V. Clarity Before Action | PASS | No open clarifications |

## Source Code (files changed)

```text
src/coordinare/__main__.py                        # MODIFIED -- build role_timeouts mapping at startup
src/coordinare/graph/state.py                     # MODIFIED -- add role_timeouts field
src/coordinare/graph/nodes/monitor_performer.py   # MODIFIED -- enforce per-role timeout
src/coordinare/dashboard.py                       # MODIFIED -- include timeout in SSE payload
tests/unit/test_main.py                          # MODIFIED -- role_timeouts construction tests
tests/unit/graph/nodes/test_monitor_performer.py # MODIFIED -- per-role timeout enforcement tests
```

## Detailed Implementation Plan

### Step 1 -- Build `role_timeouts` at Startup (`src/coordinare/__main__.py`)

Add `_build_role_timeouts(config) -> dict[str, int]` that iterates `_CANONICAL_ORDER`, reads `role_config.timeout_seconds` for each configured role, falls back to `config.transport_timeout_seconds` when None, and returns a stage-name-to-seconds mapping. Set `state["role_timeouts"]` in `_bootstrap_services`.

### Step 2 -- Extend CoordinareState (`src/coordinare/graph/state.py`)

Add `role_timeouts: dict[str, int]` to `CoordinareState` and `"role_timeouts": {}` to `initial_state()`.

### Step 3 -- Enforce in `monitor_performer` (`src/coordinare/graph/nodes/monitor_performer.py`)

Before the status poll, compare `(now - agent_dispatch_at)` against `state["role_timeouts"].get(stage, 1800)`. If exceeded, return `{"phase": "blocked", "system_error_reason": f"...timed out..."}` and log a warning.

### Step 4 -- Dashboard SSE Payload (`src/coordinare/dashboard.py`)

When phase is `monitoring_agent` or `monitoring_performer`, include `performer_timeout: {stage, elapsed_seconds, timeout_seconds}` in the SSE status payload.

## Complexity Tracking

| Change | Scope | Justification |
|--------|-------|---------------|
| `_build_role_timeouts` | ~12 LOC | Pure startup function |
| `monitor_performer` check | ~10 LOC | Single timestamp comparison |
| Dashboard SSE extension | ~6 LOC | Three extra fields in existing payload |
