# Implementation Plan: Card Prioritization

**Branch**: `025-card-prioritization` | **Date**: 2026-03-24 | **Spec**: [spec.md](./spec.md)

## Summary

Add priority-aware card selection to `check_board`. When a `priority_field` is configured, TODO cards are sorted by their GitHub Project V2 custom field value before the first eligible card is selected. An optional `priority_order` list allows operators to define semantic sort precedence. No new dependencies required. Backward compatible: unconfigured priority field preserves existing behavior.

## Technical Context

**Language/Version**: Python 3.12+
**Primary Dependencies**: pydantic-settings (existing), structlog (existing) -- no new dependencies
**Storage**: N/A -- config read from `config.yaml`; no persistence
**Testing**: pytest + pytest-asyncio (existing)
**Scale/Scope**: 2 modified files, ~10 new unit tests; no new service dependencies

## Constitution Check

| Principle | Status | Notes |
|-----------|--------|-------|
| I. Code Quality First | PASS | Priority sorting is a single pure helper called from `check_board` |
| II. Testing Discipline | PASS | All sorting edge cases independently testable |
| III. User Experience | N/A | No UI changes |
| IV. Performance by Design | PASS | In-memory sort of a small list; no API calls added |
| V. Clarity Before Action | PASS | No open clarifications |

## Source Code (files changed)

```text
src/coordinare/config.py                          # MODIFIED -- add PriorityConfig model and field
src/coordinare/graph/nodes/check_board.py         # MODIFIED -- sort eligible_todo by priority
tests/unit/graph/nodes/test_check_board.py       # MODIFIED -- ~10 new priority sorting tests
tests/unit/test_config.py                        # MODIFIED -- PriorityConfig parsing tests
```

## Detailed Implementation Plan

### Step 1 -- Add PriorityConfig (`src/coordinare/config.py`)

Add a `PriorityConfig(BaseModel)` with `field_name: str | None = None` and `priority_order: list[str] = []`. Add `priority: PriorityConfig` to `ProjectConfiguration`.

### Step 2 -- Priority Sorting in `check_board` (`src/coordinare/graph/nodes/check_board.py`)

Add a `_sort_by_priority(item_ids, field_values, priority_order) -> list[str]` helper. Items without a value sort last; when `priority_order` is set, values rank by list index; ties preserve board position (stable sort). In the TODO branch, after advocate-label filtering, if `config.priority.field_name` is set, read per-item values from `board.get("item_field_values", {})`, sort, and select `sorted_todo[0]`.

### Step 3 -- Extend `poll_board` Response (if needed)

If `item_field_values` is not already in the `poll_board` response dict, add the configured custom field to the existing GraphQL query and map it through.

## Complexity Tracking

| Change | Scope | Justification |
|--------|-------|---------------|
| `PriorityConfig` model | ~10 LOC | Two-field Pydantic model |
| `_sort_by_priority` helper | ~20 LOC | Pure function; testable in isolation |
| `check_board` modification | ~8 LOC | Conditional sort before `eligible_todo[0]` |
