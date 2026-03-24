# Implementation Plan: Human Override Controls

**Branch**: `031-human-override-controls` | **Date**: 2026-03-24 | **Spec**: [spec.md](./spec.md)

## Summary

Add dashboard API endpoints and PR comment command parsing to allow humans to skip roles, restart from a specific role, or veto the lifecycle. Overrides are stored as a `pending_override` in state and consumed by the graph on the next cycle. Four files are modified; no new dependencies.

## Technical Context

**Language/Version**: Python 3.12+
**Primary Dependencies**: FastAPI (existing), structlog (existing), re (stdlib) -- **no new dependencies required**
**Storage**: N/A -- `pending_override` is held in `CoordinareState` (in-memory)
**Testing**: pytest + pytest-asyncio (existing)
**Target Platform**: Linux server (coordinare daemon)
**Project Type**: Single project -- existing `src/coordinare/` layout
**Performance Goals**: Override check is an O(1) dict lookup at the start of each node; negligible overhead
**Constraints**: Must not break existing lifecycle advancement; overrides only apply when a card is active
**Scale/Scope**: ~4 modified files, ~15 new unit tests

## Constitution Check

| Principle | Status | Notes |
|-----------|--------|-------|
| I. Code Quality First | PASS | Override logic extracted into a shared helper; nodes check-and-clear in one step |
| II. Testing Discipline | PASS | Each override action tested via API and via PR comment; edge cases covered |
| III. User Experience | PASS | Clear error messages for invalid roles; HTTP 400 for bad requests |
| IV. Performance by Design | PASS | O(1) state lookup; no additional API calls |
| V. Clarity Before Action | PASS | No unresolved clarifications |

## Project Structure

### Source Code (files changed)

```text
src/coordinare/
├── graph/
│   ├── state.py                                 # MODIFIED -- add pending_override field
│   └── nodes/
│       ├── dispatch_performer.py                # MODIFIED -- check pending_override at entry
│       ├── monitor_performer.py                 # MODIFIED -- check pending_override at entry
│       └── classify_human_feedback.py           # MODIFIED -- parse /coordinare commands from PR comments
└── dashboard.py                                 # MODIFIED -- add POST /api/skip-role, /api/restart-from/{role}, /api/veto

tests/unit/
├── graph/nodes/
│   ├── test_dispatch_performer.py               # MODIFIED -- override check tests
│   ├── test_monitor_performer.py                # MODIFIED -- override check tests
│   └── test_classify_human_feedback.py          # MODIFIED -- command parsing tests
└── test_dashboard.py                            # MODIFIED -- API endpoint tests
```

## Detailed Implementation Plan

### Step 1 -- Extend CoordinareState (`state.py`)

Add the `pending_override` field:

```python
pending_override: dict[str, Any] | None  # {"action": "skip"|"restart"|"veto", "target_stage": str | None}
```

Update `initial_state()` with `pending_override: None`.

---

### Step 2 -- Add Override Helper (`monitor_performer.py` or new shared module)

```python
def _apply_pending_override(state: CoordinareState) -> CoordinareState | None:
    """Check for and apply a pending override. Returns updated state or None."""
    override = state.get("pending_override")
    if override is None:
        return None

    action = override.get("action")
    state["pending_override"] = None  # Clear immediately (FR-008)

    if action == "skip":
        updates = _advance_stage(state)
        for k, v in updates.items():
            state[k] = v
        return state

    if action == "restart":
        target = override.get("target_stage", "")
        lifecycle = list(state.get("lifecycle_sequence") or [])
        if target in lifecycle:
            state["performer_stage"] = target
            state["phase"] = "dispatching"
            state["agent_dispatch"] = {}
            state["agent_dispatch_at"] = None
        return state

    if action == "veto":
        state["phase"] = "blocked"
        state["open_questions"] = ["Lifecycle vetoed by human override."]
        return state

    return None
```

---

### Step 3 -- Add Dashboard Endpoints (`dashboard.py`)

```python
@app.post("/api/skip-role")
async def skip_role():
    state = _daemon._state
    if state.get("phase") not in ("monitoring_performer", "monitoring_pr", "dispatching"):
        return JSONResponse({"error": "No active card to override"}, status_code=400)
    state["pending_override"] = {"action": "skip"}
    return JSONResponse({"status": "override_queued", "action": "skip"})

@app.post("/api/restart-from/{role}")
async def restart_from(role: str):
    state = _daemon._state
    lifecycle = list(state.get("lifecycle_sequence") or [])
    if role not in lifecycle:
        return JSONResponse({"error": f"Role {role!r} not in lifecycle"}, status_code=400)
    if state.get("phase") not in ("monitoring_performer", "monitoring_pr", "dispatching"):
        return JSONResponse({"error": "No active card to override"}, status_code=400)
    state["pending_override"] = {"action": "restart", "target_stage": role}
    return JSONResponse({"status": "override_queued", "action": "restart", "target_stage": role})

@app.post("/api/veto")
async def veto():
    state = _daemon._state
    if state.get("phase") not in ("monitoring_performer", "monitoring_pr", "dispatching"):
        return JSONResponse({"error": "No active card to override"}, status_code=400)
    state["pending_override"] = {"action": "veto"}
    return JSONResponse({"status": "override_queued", "action": "veto"})
```

---

### Step 4 -- Add Command Parsing (`classify_human_feedback.py`)

Parse `/coordinare` commands before running concern classification:

```python
import re

_COMMAND_RE = re.compile(
    r"/coordinare\s+(skip-\w+|restart-from\s+\w+|veto)",
    re.IGNORECASE,
)

def _parse_coordinare_commands(reviews: list[dict]) -> dict[str, Any] | None:
    """Extract the first /coordinare command from review bodies."""
    for review in reviews:
        body = str(review.get("body", ""))
        match = _COMMAND_RE.search(body)
        if match:
            cmd = match.group(1).lower().strip()
            if cmd == "veto":
                return {"action": "veto"}
            if cmd.startswith("skip-"):
                return {"action": "skip"}
            if cmd.startswith("restart-from"):
                parts = cmd.split()
                target = parts[1] if len(parts) > 1 else ""
                return {"action": "restart", "target_stage": target}
    return None
```

Call this at the top of `classify_human_feedback`, before concern classification:

```python
command = _parse_coordinare_commands(pending_reviews)
if command is not None:
    state["pending_override"] = command
    state["pending_reviews"] = []
    state["phase"] = "monitoring_performer"  # Let the graph apply it
    return state
```

---

### Step 5 -- Wire Override Checks into Nodes

At the top of both `dispatch_performer` and `monitor_performer`:

```python
override_result = _apply_pending_override(state)
if override_result is not None:
    return override_result
```

## Complexity Tracking

No constitution violations.

| Change | Scope | Justification |
|--------|-------|---------------|
| 3 dashboard endpoints | ~30 LOC | Thin handlers that set a state field; validation is minimal |
| Command parser in classify_human_feedback | ~25 LOC | Regex-based; extracted into testable function |
| Override check in 2 nodes | ~5 LOC each | Single function call at entry point |
| ~15 new tests | ~180 LOC | API endpoints, command parsing, override application, edge cases |
