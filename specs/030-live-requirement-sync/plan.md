# Implementation Plan: Live Requirement Sync

**Branch**: `030-live-requirement-sync` | **Date**: 2026-03-24 | **Spec**: [spec.md](./spec.md)

## Summary

Add requirement-change detection to `monitor_performer` so the coordinare notices when a GitHub issue's description or acceptance criteria are updated mid-flight. A configurable policy (`ignore`, `warn`, `re-dispatch`) controls the response. Two files are modified; no new dependencies.

## Technical Context

**Language/Version**: Python 3.12+
**Primary Dependencies**: structlog (existing), pydantic-settings (existing) -- **no new dependencies required**
**Storage**: N/A -- comparison is stateless per poll cycle; `requirements_changed` flag held in `CoordinareState`
**Testing**: pytest + pytest-asyncio (existing)
**Target Platform**: Linux server (coordinare daemon)
**Project Type**: Single project -- existing `src/coordinare/` layout
**Performance Goals**: One additional `get_issue_details` call per poll cycle; < 1s additional latency
**Constraints**: Must not break existing `monitor_performer` behavior; GitHub API failures must be non-fatal
**Scale/Scope**: ~2 modified files, ~8 new unit tests

## Constitution Check

| Principle | Status | Notes |
|-----------|--------|-------|
| I. Code Quality First | PASS | Change detection is a pure comparison extracted into a helper function |
| II. Testing Discipline | PASS | Each policy mode independently testable; GitHub failure path tested |
| III. User Experience | N/A | No UI changes |
| IV. Performance by Design | PASS | Single async API call; non-blocking on failure |
| V. Clarity Before Action | PASS | No unresolved clarifications |

## Project Structure

### Source Code (files changed)

```text
src/coordinare/
├── config.py                                    # MODIFIED -- add requirement_change_policy field
└── graph/
    ├── state.py                                 # MODIFIED -- add requirements_changed, requirements_changed_details
    └── nodes/
        └── monitor_performer.py                 # MODIFIED -- add requirement re-fetch and comparison logic

tests/unit/graph/nodes/
└── test_monitor_performer.py                    # MODIFIED -- add ~8 requirement sync tests
```

## Detailed Implementation Plan

### Step 1 -- Add Config Field (`config.py`)

Add to `ProjectConfiguration`:

```python
requirement_change_policy: Literal["ignore", "warn", "re-dispatch"] = "warn"
```

---

### Step 2 -- Extend CoordinareState (`state.py`)

Add two fields:

```python
requirements_changed: bool  # True when a mid-flight requirement change is detected
requirements_changed_details: dict[str, Any] | None  # Summary of what changed
```

Update `initial_state()` with defaults: `requirements_changed: False`, `requirements_changed_details: None`.

---

### Step 3 -- Add Change Detection to `monitor_performer` (`monitor_performer.py`)

Extract a helper function and call it early in `monitor_performer`, before the status poll:

```python
async def _check_requirements_changed(
    state: CoordinareState,
) -> dict[str, Any] | None:
    """Re-fetch issue details and compare against dispatched card context.

    Returns a dict of state updates if requirements changed, or None.
    """
    github = state.get("github_service")
    card = state.get("current_card")
    config = state.get("config")
    if github is None or not isinstance(card, dict):
        return None

    issue_id = str(card.get("issue_id", ""))
    if not issue_id:
        return None

    try:
        details = await github.get_issue_details(issue_id)
    except Exception:
        logger.warning("monitor_performer.requirement_refetch_failed", exc_info=True)
        return None

    new_description = str(details.get("body", ""))
    old_description = str(card.get("description", ""))

    if new_description == old_description:
        return None  # No change

    new_criteria = parse_acceptance_criteria(new_description)
    policy = getattr(config, "requirement_change_policy", "warn") if config else "warn"

    if policy == "re-dispatch":
        updated_card = dict(card)
        updated_card["description"] = new_description
        updated_card["acceptance_criteria"] = new_criteria
        return {
            "current_card": updated_card,
            "requirements_changed": True,
            "requirements_changed_details": {"old": old_description, "new": new_description},
            "phase": "dispatching",
            "agent_dispatch": {},
            "agent_dispatch_at": None,
        }

    if policy == "warn":
        logger.warning(
            "monitor_performer.requirements_changed",
            card_id=card.get("id"),
            policy=policy,
        )

    return {
        "requirements_changed": True,
        "requirements_changed_details": {"old": old_description, "new": new_description},
    }
```

In `monitor_performer`, call early and short-circuit on re-dispatch:

```python
req_updates = await _check_requirements_changed(state)
if req_updates is not None:
    for k, v in req_updates.items():
        state[k] = v
    if req_updates.get("phase") == "dispatching":
        return state  # Re-dispatch immediately
```

---

### Step 4 -- Tests (`test_monitor_performer.py`)

Add tests for:
1. Description unchanged -> no flag set, performer continues
2. Description changed + warn policy -> flag set, warning logged, performer continues
3. Description changed + re-dispatch policy -> phase set to dispatching, card updated
4. Description changed + ignore policy -> flag set silently, no warning
5. GitHub API failure on re-fetch -> warning logged, performer continues
6. Missing issue_id in card -> check skipped gracefully
7. Acceptance criteria change detected via parsed comparison
8. Config is None -> defaults to warn policy

## Complexity Tracking

No constitution violations.

| Change | Scope | Justification |
|--------|-------|---------------|
| 1 helper function in monitor_performer | ~40 LOC | Extracted for testability; called once per poll cycle |
| Config + state field additions | ~5 LOC each | Minimal schema extensions |
