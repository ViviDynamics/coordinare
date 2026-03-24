# Implementation Plan: Active Board Reconciliation

**Branch**: `032-active-board-reconciliation` | **Date**: 2026-03-24 | **Spec**: [spec.md](./spec.md)

## Summary

Add board-column reconciliation to `monitor_performer` so the coordinare detects when a human manually moves a card on the GitHub board and corrects internal state immediately. A phase-to-column mapping drives the comparison; mismatches trigger reset, fast-forward, or block actions. Two files are modified; no new dependencies.

## Technical Context

**Language/Version**: Python 3.12+
**Primary Dependencies**: structlog (existing) -- **no new dependencies required**
**Storage**: N/A -- reconciliation is stateless; uses existing `board_snapshot` from `CoordinareState`
**Testing**: pytest + pytest-asyncio (existing)
**Target Platform**: Linux server (coordinare daemon)
**Project Type**: Single project -- existing `src/coordinare/` layout
**Performance Goals**: O(1) dict lookup per poll cycle; no additional API calls
**Constraints**: Must not interfere with normal lifecycle progression; false positives must be zero
**Scale/Scope**: ~2 modified files, ~8 new unit tests

## Constitution Check

| Principle | Status | Notes |
|-----------|--------|-------|
| I. Code Quality First | PASS | Reconciliation is a pure function extracted for testability |
| II. Testing Discipline | PASS | Each mismatch direction independently tested; no-op case tested |
| III. User Experience | N/A | No UI changes |
| IV. Performance by Design | PASS | Pure dict lookup; no network calls |
| V. Clarity Before Action | PASS | No unresolved clarifications |

## Project Structure

### Source Code (files changed)

```text
src/coordinare/graph/nodes/
└── monitor_performer.py                 # MODIFIED -- add reconciliation check before status poll

tests/unit/graph/nodes/
└── test_monitor_performer.py            # MODIFIED -- add ~8 reconciliation tests
```

## Detailed Implementation Plan

### Step 1 -- Add Phase-to-Column Mapping (`monitor_performer.py`)

```python
PHASE_TO_EXPECTED_COLUMN: dict[str, str] = {
    "monitoring_performer": "IN_PROGRESS",
    "monitoring_pr": "IN_REVIEW",
    "dispatching": "IN_PROGRESS",
    "merging": "IN_REVIEW",
    "blocked": "BLOCKED",
}
```

---

### Step 2 -- Add Reconciliation Helper (`monitor_performer.py`)

```python
def _find_card_column(card_id: str, board_snapshot: dict[str, list[str]]) -> str | None:
    """Find which column a card is in from the board snapshot."""
    for column, card_ids in board_snapshot.items():
        if card_id in card_ids:
            return column
    return None

def _reconcile_board_mismatch(
    state: CoordinareState,
    actual_column: str,
    expected_column: str,
) -> dict[str, Any] | None:
    """Compute state updates to reconcile a board column mismatch.

    Returns a dict of state updates, or None if no action is needed.
    """
    if actual_column == expected_column:
        return None

    logger.warning(
        "monitor_performer.board_mismatch",
        expected=expected_column,
        actual=actual_column,
        card_id=state.get("current_card", {}).get("id"),
    )

    # Card moved backward (e.g., IN_PROGRESS -> TODO or BACKLOG)
    if actual_column in ("TODO", "BACKLOG"):
        return {
            "phase": "idle",
            "agent_dispatch": {},
            "agent_dispatch_at": None,
            "current_card": None,
        }

    # Card moved to DONE (human resolved it)
    if actual_column == "DONE":
        card = dict(state.get("current_card") or {})
        card["status"] = "DONE"
        return {
            "phase": "idle",
            "current_card": card,
            "agent_dispatch": {},
            "agent_dispatch_at": None,
        }

    # Card moved to BLOCKED
    if actual_column == "BLOCKED":
        return {
            "phase": "blocked",
            "open_questions": ["Card was manually moved to BLOCKED on the board."],
        }

    return None
```

---

### Step 3 -- Wire Into `monitor_performer`

Add the check at the top of `monitor_performer`, after resolving the service but before calling `check_status`:

```python
# --- Board reconciliation (032) ---
card_id = str(card.get("id", ""))
board_snapshot = state.get("board_snapshot") or {}
expected_column = PHASE_TO_EXPECTED_COLUMN.get(state.get("phase", ""), "")

if expected_column and board_snapshot:
    actual_column = _find_card_column(card_id, board_snapshot)
    if actual_column is not None:
        reconcile_updates = _reconcile_board_mismatch(state, actual_column, expected_column)
        if reconcile_updates is not None:
            for k, v in reconcile_updates.items():
                state[k] = v
            await _teardown_workspace(state)
            return state
    elif actual_column is None and expected_column:
        # Card not found in any column -- may have been archived/deleted
        logger.warning("monitor_performer.card_not_on_board", card_id=card_id)
        state["phase"] = "idle"
        state["agent_dispatch"] = {}
        state["agent_dispatch_at"] = None
        state["current_card"] = None
        await _teardown_workspace(state)
        return state
```

---

### Step 4 -- Tests (`test_monitor_performer.py`)

Add tests for:
1. Card in expected column (IN_PROGRESS for monitoring_performer) -> no reconciliation, normal monitoring
2. Card moved to TODO -> state reset to idle, agent_dispatch cleared
3. Card moved to DONE -> card marked complete, phase set to idle
4. Card moved to BLOCKED -> phase set to blocked
5. Card not found in any column -> phase set to idle, warning logged
6. Board snapshot is empty -> reconciliation skipped, normal monitoring
7. Phase not in PHASE_TO_EXPECTED_COLUMN -> reconciliation skipped
8. Workspace teardown called on reconciliation

## Complexity Tracking

No constitution violations.

| Change | Scope | Justification |
|--------|-------|---------------|
| Reconciliation helper in monitor_performer | ~40 LOC | Extracted pure function + wiring; runs before status poll |
| Phase-to-column mapping | ~5 LOC | Module-level constant |
| ~8 new tests | ~100 LOC | Each mismatch direction + no-op case |
