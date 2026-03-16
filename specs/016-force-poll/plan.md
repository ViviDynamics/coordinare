# Implementation Plan: Dashboard "Check Board Now" Button

**Branch**: `016-force-poll` | **Date**: 2026-03-15 | **Spec**: [spec.md](./spec.md)
**Input**: Feature specification from `/specs/016-force-poll/spec.md`

## Summary

Add a "Check Board Now" button to the coordinare dashboard that fires the existing `_webhook_trigger` asyncio.Event, causing the daemon to begin a poll cycle immediately. The feature requires one new FastAPI endpoint (`POST /api/force-poll`), a one-field addition to the SSE snapshot payload (`cycle_active`), and a small HTML/JS delta in the inline dashboard template.

No new dependencies. No new daemon state. No new data model.

## Technical Context

**Language/Version**: Python 3.12+
**Primary Dependencies**: FastAPI + Starlette (existing), asyncio stdlib, vanilla JavaScript (inline in dashboard HTML)
**Storage**: N/A — trigger is ephemeral; no persistence required
**Testing**: pytest + pytest-asyncio (existing); httpx TestClient for endpoint contract tests
**Target Platform**: Linux server (same as coordinare daemon)
**Project Type**: Single project — all changes in `src/coordinare/dashboard.py` and inline HTML
**Performance Goals**: POST /api/force-poll must respond in < 50 ms (it is a single asyncio.Event.set() call)
**Constraints**: Must not block the daemon event loop; must not introduce new Python packages
**Scale/Scope**: Single operator per dashboard instance; concurrent tab safety handled by existing SSE fan-out

## Constitution Check

| Principle | Status | Notes |
|-----------|--------|-------|
| I. Code Quality | ✅ PASS | Single-responsibility endpoint; no dead code |
| II. Testing Discipline | ✅ PASS | Contract tests for endpoint; unit tests for SSE field; button JS logic tested |
| III. UX Consistency | ✅ PASS | Button follows existing disabled/active patterns in dashboard |
| IV. Performance by Design | ✅ PASS | Budget defined: < 50 ms endpoint response; SSE-driven state = no polling overhead |
| V. Clarity Before Action | ✅ PASS | No NEEDS CLARIFICATION markers remain |

**Quality Gates**: lint, type check, unit tests, contract tests, coverage ≥ 90% (no regression).

## Project Structure

### Documentation (this feature)

```text
specs/016-force-poll/
├── plan.md              ← this file
├── research.md          ← Phase 0 output
├── contracts/
│   └── force_poll.py    ← Phase 1 output
├── quickstart.md        ← Phase 1 output
└── tasks.md             ← Phase 2 output (/speckit.tasks)
```

### Source Code (changes only)

```text
src/coordinare/
└── dashboard.py         ← all backend + HTML changes

tests/
├── unit/
│   └── dashboard/
│       └── test_force_poll.py     ← new: endpoint + SSE field unit tests
└── contract/
    └── test_force_poll_contract.py ← new: contract invariant tests
```

**Structure Decision**: Single-project, no new modules. All changes are additive to `dashboard.py`.

## Phase 0: Research — Complete

See [research.md](./research.md). All decisions resolved:

- **Trigger**: `daemon._webhook_trigger.set()` (existing mechanism)
- **Endpoint**: `POST /api/force-poll` → 202 (idle) / 409 (cycle active)
- **SSE field**: `cycle_active: bool` added to `build_snapshot()` return dict
- **Button state**: driven by SSE `cycle_active`; client-side in-flight disable on click
- **Dependencies**: none new

## Phase 1: Design

### Backend Changes (`src/coordinare/dashboard.py`)

#### 1. `DashboardStore.build_snapshot()` — add `cycle_active` field

```python
# In build_snapshot() return dict, add:
"cycle_active": daemon._cycle_active,
```

This single addition exposes the daemon's in-cycle state to the SSE stream. The field is `False` while the daemon is idle/waiting and `True` during an active `graph.ainvoke()` call.

#### 2. `create_dashboard_app()` — add `POST /api/force-poll` endpoint

```python
@app.post("/api/force-poll")
async def force_poll() -> JSONResponse:
    if daemon._cycle_active:
        return JSONResponse({"status": "cycle_in_progress"}, status_code=409)
    daemon._webhook_trigger.set()
    return JSONResponse({"status": "accepted"}, status_code=202)
```

Idempotency: `asyncio.Event.set()` is idempotent — calling it when already set is a no-op. Combined with the 409 guard on `_cycle_active`, rapid duplicate POSTs are absorbed.

#### 3. `_DASHBOARD_HTML` — "Check Board Now" button

**Placement**: Below the phase indicator, above the cycle history table.

**HTML addition** (within the status card area):
```html
<button id="force-poll-btn" class="action-btn" onclick="forcePoll()">
  Check Board Now
</button>
<span id="force-poll-msg" style="margin-left:8px;font-size:12px;"></span>
```

**CSS addition**:
```css
.action-btn { ... }                /* enabled state */
.action-btn:disabled { ... }       /* disabled/in-flight state */
```

**JS addition**:
```javascript
async function forcePoll() {
    const btn = document.getElementById('force-poll-btn');
    const msg = document.getElementById('force-poll-msg');
    btn.disabled = true;
    msg.textContent = '';
    try {
        const res = await fetch('/api/force-poll', { method: 'POST' });
        if (res.status === 409) {
            msg.textContent = 'Cycle already running';
            btn.disabled = false;   // re-enable immediately; SSE will keep it correct
        }
        // 202: stay disabled until SSE cycle_active=false
    } catch {
        msg.textContent = 'Could not reach server';
        btn.disabled = false;
    }
}

// In the existing SSE onmessage handler, add:
// btn.disabled = s.cycle_active;
// if (!s.cycle_active) msg.textContent = '';
```

### Data Model

No persistent entities. See [contracts/force_poll.py](./contracts/force_poll.py) for the endpoint contract and SSE field addition.

### Testing Strategy

| Test file | Covers |
|-----------|--------|
| `tests/unit/dashboard/test_force_poll.py` | POST 202 when idle; POST 409 when cycle_active; SSE snapshot includes cycle_active field |
| `tests/contract/test_force_poll_contract.py` | All 6 contract invariants from `contracts/force_poll.py` |

All tests use FastAPI `TestClient` (synchronous) or `httpx.AsyncClient` (async). No real daemon needed — `daemon` is a `MagicMock` with `_cycle_active` and `_webhook_trigger` set explicitly.
