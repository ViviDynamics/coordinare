# Implementation Plan: Card Cancellation

**Branch**: `026-card-cancellation` | **Date**: 2026-03-24 | **Spec**: [spec.md](./spec.md)

## Summary

Add two cancellation paths: a `POST /api/cancel` dashboard endpoint and board-column-based detection in `check_board`. Both paths invoke a shared `cancel_active_card()` helper that stops the performer session, cleans up the workspace, resets state, and emits a notification. No new dependencies required.

## Technical Context

**Language/Version**: Python 3.12+
**Primary Dependencies**: FastAPI (existing), structlog (existing), asyncio (stdlib) -- no new dependencies
**Storage**: N/A -- cancellation is an in-memory state transition
**Testing**: pytest + pytest-asyncio (existing)
**Scale/Scope**: 3 modified files (+ 1 new), ~10 new unit tests; no new service dependencies

## Constitution Check

| Principle | Status | Notes |
|-----------|--------|-------|
| I. Code Quality First | PASS | Single async helper reused by both cancel paths |
| II. Testing Discipline | PASS | Both paths and edge cases independently testable |
| III. User Experience | PASS | Dashboard cancel button + board column detection |
| IV. Performance by Design | PASS | No polling overhead; triggered on-demand or during existing cycle |
| V. Clarity Before Action | PASS | No open clarifications |

## Source Code (files changed)

```text
src/coordinare/cancel.py                          # NEW -- shared cancel_active_card() helper
src/coordinare/dashboard.py                       # MODIFIED -- POST /api/cancel endpoint + cancel button
src/coordinare/graph/nodes/check_board.py         # MODIFIED -- detect active card missing from known columns
tests/unit/test_cancel.py                        # NEW -- cancel helper tests
tests/unit/graph/nodes/test_check_board.py       # MODIFIED -- board-based cancellation tests
tests/unit/test_dashboard.py                     # MODIFIED -- cancel endpoint tests
```

## Detailed Implementation Plan

### Step 1 -- Shared Cancel Helper (`src/coordinare/cancel.py`)

`cancel_active_card(state, *, move_to_todo=True, cancel_timeout_seconds=10) -> dict` that: (1) returns `{"status": "no_active_card"}` if idle; (2) stops the performer session via `service.cancel(session_id)` with `asyncio.wait_for` timeout; (3) calls `workspace_manager.teardown`; (4) optionally moves the card to TODO via `github.move_card`; (5) resets `phase`, `current_card`, `agent_dispatch`, `workspace_path`; (6) emits a `card_cancelled` notification.

### Step 2 -- Dashboard Endpoint (`src/coordinare/dashboard.py`)

Add `@app.post("/api/cancel")` inside `create_dashboard_app` that calls `cancel_active_card(daemon.state)` and returns the result as JSON. Add a cancel button in the dashboard HTML alongside the force-poll button.

### Step 3 -- Board Detection in `check_board` (`src/coordinare/graph/nodes/check_board.py`)

After reading the board snapshot, if an active card's ID does not appear in any of the six known columns (BACKLOG, TODO, IN_PROGRESS, BLOCKED, IN_REVIEW, DONE) and the phase is `monitoring_agent` or `monitoring_performer`, call `cancel_active_card(state, move_to_todo=False)` and return early.

## Complexity Tracking

| Change | Scope | Justification |
|--------|-------|---------------|
| `cancel.py` helper | ~40 LOC | Shared logic avoids duplication between API and board paths |
| Dashboard endpoint | ~10 LOC | Thin wrapper around the shared helper |
| `check_board` detection | ~10 LOC | Early-return guard at top of function |
