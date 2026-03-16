# Tasks: Dashboard "Check Board Now" Button (016-force-poll)

**Input**: Design documents from `/specs/016-force-poll/`
**Prerequisites**: plan.md ✓, spec.md ✓, research.md ✓, contracts/force_poll.py ✓, quickstart.md ✓

**Organization**: Two user stories, both implemented in `src/coordinare/dashboard.py` and `tests/unit/dashboard/`. US1 (trigger endpoint) is the MVP; US2 (button state reflects daemon activity) builds directly on top.

---

## Phase 1: Setup

**Purpose**: No new files or packages needed — this phase is minimal.

- [X] T001 Verify `tests/unit/dashboard/__init__.py` exists (already present from 015); confirm `tests/contract/` directory exists or create it at repo root

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: The `cycle_active` field must be in the SSE snapshot before either user story's UI or endpoint can be fully tested.

- [X] T002 Add `"cycle_active": daemon._cycle_active` to the `build_snapshot()` return dict in `src/coordinare/dashboard.py` (line ~257, alongside existing fields like `"cycles_completed"`)
- [X] T003 Add unit test asserting `cycle_active` is present and correct in the SSE snapshot in `tests/unit/dashboard/test_force_poll.py`

**Checkpoint**: SSE stream now carries `cycle_active` boolean — both user stories can proceed.

---

## Phase 3: User Story 1 — Trigger Immediate Poll (Priority: P1) 🎯 MVP

**Goal**: `POST /api/force-poll` endpoint fires `_webhook_trigger` and returns 202; returns 409 when a cycle is already active.

**Independent Test**: `curl -X POST http://127.0.0.1:8090/api/force-poll` returns `{"status":"accepted"}` with 202 when daemon is idle. Repeat immediately → 409 if cycle is active.

### Implementation

- [X] T004 [US1] Add `POST /api/force-poll` endpoint to `create_dashboard_app()` in `src/coordinare/dashboard.py`: return 202 + fire `daemon._webhook_trigger.set()` when `daemon._cycle_active` is False; return 409 when True
- [X] T005 [US1] Add unit test: POST returns 202 and sets `_webhook_trigger` when `_cycle_active=False` in `tests/unit/dashboard/test_force_poll.py`; assert response time < 50 ms to satisfy SC-001 (measured via `time.perf_counter()` around `TestClient.post()`)
- [X] T006 [US1] Add unit test: POST returns 409 and does NOT set `_webhook_trigger` when `_cycle_active=True` in `tests/unit/dashboard/test_force_poll.py`
- [X] T007 [US1] Add contract test covering all 6 invariants from `specs/016-force-poll/contracts/force_poll.py` in `tests/contract/test_force_poll_contract.py`

**Checkpoint**: US1 complete — endpoint functional and tested. MVP deliverable.

---

## Phase 4: User Story 2 — Button State Reflects Daemon Activity (Priority: P2)

**Goal**: "Check Board Now" button in the dashboard HTML is enabled when idle, disabled when active, debounced on click, and shows error feedback on failure.

**Independent Test**: Open dashboard while daemon is idle → button is enabled. Start a cycle → button disables. Cycle ends → button re-enables.

### Implementation

- [X] T008 [US2] Add "Check Board Now" button HTML and `<span>` message element to `_DASHBOARD_HTML` in `src/coordinare/dashboard.py`, placed below the phase indicator; include `aria-label="Trigger immediate board poll"` on the button element for WCAG 2.1 AA compliance
- [X] T009 [US2] Add `.action-btn` CSS rule (enabled and `:disabled` states) to the `<style>` block in `_DASHBOARD_HTML` in `src/coordinare/dashboard.py`
- [X] T010 [US2] Add `forcePoll()` JavaScript function to `_DASHBOARD_HTML` in `src/coordinare/dashboard.py`: disables button on click, POSTs to `/api/force-poll`, shows "Cycle already running" on 409, shows "Could not reach server" on network error
- [X] T011 [US2] Wire `cycle_active` and `running` from SSE `state_update` payload into the existing `onmessage` handler in `_DASHBOARD_HTML`: set `btn.disabled = s.cycle_active || !s.running`; clear message text only when both `cycle_active` is false and `running` is true (covers stopped/error state — US2 scenario 3)
- [X] T012 [US2] Add unit test: button HTML element with id `force-poll-btn` is present in `_DASHBOARD_HTML` string and includes `aria-label="Trigger immediate board poll"` in `tests/unit/dashboard/test_force_poll.py`
- [X] T013 [US2] Add unit test: `forcePoll()` function definition is present in `_DASHBOARD_HTML` string in `tests/unit/dashboard/test_force_poll.py`

**Checkpoint**: Full feature complete — button visible, state-driven, and error-handled.

---

## Phase 5: Polish & Cross-Cutting Concerns

- [X] T014 [P] Run full test suite and confirm coverage does not regress below 90%: `.venv/bin/pytest --cov=src/coordinare --cov-fail-under=90`
- [X] T015 [P] Run ruff lint on changed files: `.venv/bin/ruff check src/coordinare/dashboard.py tests/unit/dashboard/test_force_poll.py tests/contract/test_force_poll_contract.py`
- [X] T016 Validate quickstart.md Scenario 1 manually: start coordinare, open dashboard, confirm button present and functional end-to-end

---

## Dependencies & Execution Order

- **T001** → no dependencies
- **T002, T003** → depend on T001 (foundational; block all story work)
- **T004** → depends on T002
- **T005, T006** → depend on T004 (can run in parallel with each other)
- **T007** → depends on T004
- **T008** → depends on T002 (needs `cycle_active` in SSE)
- **T009** → can run in parallel with T008 (same file, different section — do sequentially)
- **T010, T011** → depend on T008, T009
- **T012, T013** → depend on T010, T011
- **T014, T015** → depend on all prior tasks [P with each other]
- **T016** → depends on T014, T015

### Parallel Opportunities

- T005 and T006 are independent tests — write both before verifying
- T014 and T015 (lint + coverage) run in parallel
- T008 and T009 touch the same file but different sections — safe to batch in one edit pass

---

## Implementation Strategy

### MVP (US1 only — T001–T007)

1. T001: confirm test directories
2. T002–T003: add `cycle_active` to SSE snapshot + test
3. T004–T007: endpoint + tests

**STOP and validate**: `curl -X POST .../api/force-poll` → 202; confirm daemon wakes.

### Full Feature (add US2 — T008–T013)

4. T008–T011: button HTML, CSS, JS
5. T012–T013: presence tests

### Done

6. T014–T016: lint, coverage, manual validation

---

## Notes

- All changes are in `src/coordinare/dashboard.py` — no new modules
- `_DASHBOARD_HTML` is a large inline string; use targeted Edit operations to insert button HTML, CSS, and JS in the correct sections
- Use `MagicMock` for daemon in tests: set `daemon._cycle_active` and `daemon._webhook_trigger` explicitly
- The `fastapi.testclient.TestClient` can be used synchronously for endpoint tests
