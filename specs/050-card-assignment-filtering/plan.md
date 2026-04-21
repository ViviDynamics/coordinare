# Implementation Plan: Card Assignment Filtering

**Branch**: `050-card-assignment-filtering` | **Date**: 2026-04-21 | **Spec**: specs/050-card-assignment-filtering/spec.md

## Summary

Add an optional `assignee_filter` config field to `CoordinareConfig`. When set, `check_board` skips TODO cards whose GitHub issue assignees do not include the configured login. The `POLL_BOARD_QUERY` is extended to fetch assignees alongside labels. No new dependencies required — all changes are in the existing config, github service, and check_board node.

## Technical Context

**Language/Version**: Python 3.12+  
**Primary Dependencies**: pydantic-settings (existing), gql[aiohttp] (existing), structlog (existing)  
**Storage**: N/A — filter applied in-memory per poll cycle  
**Testing**: pytest (existing)  
**Target Platform**: Linux server (coordinare daemon)  
**Performance Goals**: No new API calls — assignees fetched alongside existing POLL_BOARD_QUERY  
**Constraints**: Must not change behavior when `assignee_filter` is unset (null/empty)  
**Scale/Scope**: Single-login filter; ≤50 board items per poll

## Constitution Check

| Gate | Status | Notes |
|------|--------|-------|
| Lint & Format | ✓ PASS | ruff enforced in CI |
| Type Check | ✓ PASS | All new fields typed |
| Unit Tests | ✓ PASS | New tests for filter logic in check_board + config |
| Coverage | ✓ PASS | New code paths covered |
| No dead code | ✓ PASS | No unused branches |

## Project Structure

```text
src/coordinare/
├── config.py                          # Add assignee_filter field
├── services/github.py                 # Add assignees to POLL_BOARD_QUERY + poll_board()
├── graph/nodes/check_board.py         # Apply assignee filter after advocate filter
└── dashboard.py                       # Expose assignee_filter in SSE snapshot

tests/unit/
├── test_check_board.py                # Tests for assignee filter logic
└── test_config.py                     # Test assignee_filter config parsing
```

**Structure Decision**: Single project, all changes in existing files. No new files needed.

## Implementation Notes

### 1. GitHub Query — add `assignees` to Issue content node

In `POLL_BOARD_QUERY` (github.py line 82), add to the `... on Issue` block:
```graphql
assignees(first: 10) {
  nodes { login }
}
```

### 2. `poll_board()` — parse item_assignees

In the `content` parsing block (github.py ~line 610), alongside `item_labels`, parse:
```python
item_assignees: dict[str, list[str]] = {}
# ...
assignee_nodes = content.get("assignees", {})
if isinstance(assignee_nodes, dict):
    item_assignees[item_id] = [
        str(n.get("login", "")).lower()
        for n in assignee_nodes.get("nodes", [])
        if isinstance(n, dict)
    ]
```
Add `"item_assignees": item_assignees` to the return dict.

### 3. `CoordinareConfig` — add `assignee_filter`

```python
assignee_filter: str | None = Field(default=None)
```
Placed after `priority` in `CoordinareConfig`. No nested config needed — it's a single string.

### 4. `check_board.py` — filter eligible_todo by assignee

After the advocate-label filter (~line 386), before priority sort:
```python
assignee_filter = getattr(config, "assignee_filter", None) if config else None
if assignee_filter:
    filter_login = str(assignee_filter).strip().lower()
    item_assignees = board.get("item_assignees", {})
    pre_assignee = eligible_todo
    eligible_todo = [
        item_id for item_id in eligible_todo
        if filter_login in item_assignees.get(item_id, [])
    ]
    skipped = len(pre_assignee) - len(eligible_todo)
    if skipped:
        logger.info(
            "check_board.assignee_filtered",
            skipped=skipped,
            assignee_filter=filter_login,
        )
```

### 5. SSE snapshot — expose assignee_filter

In `DashboardStore.build_snapshot()` (dashboard.py), add:
```python
"assignee_filter": getattr(config, "assignee_filter", None),
```

### 6. Dashboard — idle state indicator

In `renderActivePerformers(s)`, when showing the idle message, append the filter hint if `s.assignee_filter` is set:
```javascript
var filterHint = s.assignee_filter ? ' &nbsp;&#183;&nbsp; Filter: ' + esc(s.assignee_filter) : '';
container.innerHTML = '<div class="ap-idle">No active performers ... ' + filterHint + '</div>';
```

## Complexity Tracking

No constitution violations. This is a small, additive change.
