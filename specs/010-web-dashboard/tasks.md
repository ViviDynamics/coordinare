# Tasks: Live Web Dashboard

**Input**: Design documents from `/specs/010-web-dashboard/`
**Prerequisites**: plan.md ✓, spec.md ✓, research.md ✓, data-model.md ✓, contracts/api.md ✓, quickstart.md ✓

**Organization**: Tasks are grouped by user story to enable independent implementation and testing of each story.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to (US1, US2, US3, US4)
- Include exact file paths in descriptions

---

## Phase 1: Setup (Config Fields)

**Purpose**: Add dashboard config to `ProjectConfiguration` — required before any startup wiring.

- [x] T001 Add `dashboard_port: int = Field(default=8090)` and `dashboard_host: str = "127.0.0.1"` to `ProjectConfiguration` in `src/coordinare/config.py`
- [x] T002 [P] Write tests for new config fields (default values, `COORDINARE_DASHBOARD_PORT` env override, `COORDINARE_DASHBOARD_HOST` env override) in `tests/unit/test_config.py`

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Core dashboard infrastructure that ALL user stories depend on. No user story work can begin until this phase is complete.

**⚠️ CRITICAL**: No user story work can begin until this phase is complete.

- [x] T003 Create `src/coordinare/dashboard.py` with module docstring and all stdlib imports (`asyncio`, `collections`, `datetime`, `json`, `time`)
- [x] T004 [P] Implement `format_phase_label(phase: str) -> str` pure function (`phase.replace("_", " ").title()`) in `src/coordinare/dashboard.py`
- [x] T005 [P] Implement `SSEBroadcaster` class (subscribe/unsubscribe/broadcast with per-client `asyncio.Queue(maxsize=32)`) in `src/coordinare/dashboard.py`
- [x] T006 [P] Implement `DashboardStore` shell class with `broadcaster: SSEBroadcaster`, `last_cycle_duration: float | None`, and stub `build_snapshot()` returning empty dict in `src/coordinare/dashboard.py`
- [x] T007 Implement `check_port_available(host: str, port: int) -> None` helper (socket bind probe, logs `dashboard_port_conflict` and calls `sys.exit(1)` on failure) in `src/coordinare/dashboard.py`

**Checkpoint**: Foundation ready — user story implementation can now begin.

---

## Phase 3: User Story 1 — Current Status at a Glance (Priority: P1) 🎯 MVP

**Goal**: An operator opens the dashboard and immediately sees current workflow phase, active card details, agent session ID, and open questions — populated from the daemon's in-process state.

**Independent Test**: Navigate to `http://localhost:8090/` with the daemon running in each phase (`idle`, `dispatching`, `monitoring_agent`, `blocked`). Confirm phase is correctly labelled and card details are present/absent appropriately. The initial `state_update` SSE event must arrive within 1 second of page load.

### Implementation for User Story 1

- [x] T008 [US1] Implement `DashboardStore.build_snapshot(daemon, metrics, health) -> dict` with phase, phase_label, active_card_title, active_card_column, pr_url, agent_session_id, open_questions fields (metrics/health/history fields can be stubs returning 0/null/[]) in `src/coordinare/dashboard.py`
- [x] T009 [US1] Implement `create_dashboard_app(store, daemon, metrics, health) -> FastAPI` with `GET /` returning `HTMLResponse` and `GET /events` SSE generator (subscribe → yield initial state_update → loop with 15s timeout/keepalive → unsubscribe in finally) in `src/coordinare/dashboard.py`
- [x] T010 [US1] Add HTTP request logging middleware (`@app.middleware("http")`) logging `method`, `path`, `status_code`, `response_time_ms`, and `streaming=True` for `/events` in `src/coordinare/dashboard.py`
- [x] T011 [US1] Write `_DASHBOARD_HTML` string constant with inline CSS + JS: phase display (title-cased, colour-coded), active-card section (title/column/PR link or "No active card"), agent-session line, open-questions list (hidden unless blocked phase); JS connects via `EventSource('/events')` and updates DOM on `state_update` events in `src/coordinare/dashboard.py`
- [x] T012 [US1] Add `dashboard_store: DashboardStore | None = None` optional parameter to `CoordinareDaemon.__init__()` and store as `self._dashboard_store` in `src/coordinare/daemon.py`
- [x] T013 [US1] Wire dashboard into `_run()`: call `check_port_available`, create `DashboardStore`, call `create_dashboard_app()`, create `uvicorn.Server` for dashboard on `config.dashboard_host:config.dashboard_port`, add `dashboard_task` alongside `daemon_task` and `server_task`, log `dashboard_started`, add clean shutdown to `finally` block in `src/coordinare/__main__.py`
- [x] T014 [P] [US1] Write tests for `format_phase_label` (known phases, unknown future phase) in `tests/unit/test_dashboard.py`
- [x] T015 [P] [US1] Write tests for `GET /` returns 200 HTML and `GET /events` first event is `state_update` with correct shape in `tests/unit/test_dashboard.py`
- [x] T016 [P] [US1] Write test for `check_port_available` raises `SystemExit(1)` when port already bound in `tests/unit/test_dashboard.py`
- [x] T017 [P] [US1] Write test for HTTP middleware logs `method`, `path`, `status_code`, `response_time_ms` fields in `tests/unit/test_dashboard.py`

**Checkpoint**: US1 complete — dashboard is navigable and shows live phase/card/agent/questions on page load.

---

## Phase 4: User Story 2 — Subsystem Health and Key Metrics (Priority: P2)

**Goal**: The operator sees health status for every registered subsystem and a dashboard-level metrics bar (cycles completed, last cycle duration, error count, daemon start time).

**Independent Test**: With one subsystem degraded, load the dashboard and confirm it is visually distinct from healthy subsystems. Confirm cycle count increments between page loads. Daemon start time is prominently shown.

### Implementation for User Story 2

- [x] T018 [US2] Expand `DashboardStore.build_snapshot()` to populate `subsystems` list from `HealthRegistry.snapshot().probes` (name, status, required, checked_at, details) in `src/coordinare/dashboard.py`
- [x] T019 [US2] Expand `DashboardStore.build_snapshot()` to populate `cycles_completed` (from `METRICS.cycles_completed_total._value.get()`), `last_cycle_duration_seconds` (from `self.last_cycle_duration`), `error_count_since_start` (from `daemon.state.get("error_count", 0)`), `daemon_start_time` (from `METRICS.build_info`) in `src/coordinare/dashboard.py`
- [x] T020 [US2] Add subsystem-health table section to `_DASHBOARD_HTML`: status badge (green/amber/red) per probe, required/optional label, details tooltip; add metrics bar section showing cycles, duration, error count, and daemon start time prominently in `src/coordinare/dashboard.py`
- [x] T021 [P] [US2] Write tests for `build_snapshot()` subsystem fields (all healthy, one degraded, unavailable probe) in `tests/unit/test_dashboard.py`
- [x] T022 [P] [US2] Write tests for `build_snapshot()` metrics fields (cycles_completed, last_cycle_duration null before first cycle, daemon_start_time present) in `tests/unit/test_dashboard.py`

**Checkpoint**: US2 complete — health and metrics visible alongside phase/card.

---

## Phase 5: User Story 3 — Recent Cycle History (Priority: P3)

**Goal**: A rolling list of the 20 most recent cycle completions (timestamp, phase, duration, success/error) is visible, with an empty-state message before the first cycle.

**Independent Test**: Run daemon through 5 cycles (mix of success/error), load dashboard, confirm last 5 entries appear in reverse-chronological order with correct outcomes. Confirm empty-state message when no cycles have run.

### Implementation for User Story 3

- [x] T023 [US3] Add `history: deque[dict]` (maxlen=20) to `DashboardStore.__init__()` and implement `record_cycle(*, duration_seconds, phase, outcome)` method that `appendleft`s a `CycleHistoryEntry` dict in `src/coordinare/dashboard.py`
- [x] T024 [US3] Expand `DashboardStore.build_snapshot()` to populate `cycle_history` from `list(self.history)` (already newest-first due to appendleft) in `src/coordinare/dashboard.py`
- [x] T025 [US3] Add history section to `_DASHBOARD_HTML`: reverse-chronological table with timestamp, phase label, duration, outcome badge (success=green, error=red); show empty-state message `"No cycles completed yet"` when list is empty in `src/coordinare/dashboard.py`
- [x] T026 [US3] Call `self._dashboard_store.record_cycle(duration_seconds=_cycle_elapsed, phase=..., outcome="success")` after successful `ainvoke` in `src/coordinare/daemon.py`
- [x] T027 [US3] Call `self._dashboard_store.record_cycle(duration_seconds=0.0, phase=..., outcome="error")` in the `except Exception` handler in `src/coordinare/daemon.py`
- [x] T028 [P] [US3] Write tests for `record_cycle()` appends correct fields, history caps at 20 entries (drops oldest), newest-first ordering, empty-state scenario in `tests/unit/test_dashboard.py`

**Checkpoint**: US3 complete — cycle history visible with correct entries and empty-state handling.

---

## Phase 6: User Story 4 — Live Auto-Refresh (Priority: P4)

**Goal**: State changes are pushed to all open browser tabs within 5 seconds via SSE broadcasting, and a disconnected banner appears/clears automatically when the connection is lost/restored.

**Independent Test**: Open dashboard in browser, trigger a phase transition, confirm the phase label updates within 5 seconds with no manual interaction. Stop and restart the daemon — confirm "Disconnected" banner appears and then clears within 10 seconds.

### Implementation for User Story 4

- [x] T029 [US4] Call `self._dashboard_store.broadcaster.broadcast(self._dashboard_store.build_snapshot(self, METRICS, HEALTH))` after `record_cycle()` on both success and error paths in `src/coordinare/daemon.py`
- [x] T030 [US4] Add `#disconnected-banner` (fixed overlay, hidden by default) to `_DASHBOARD_HTML`; add `EventSource.onerror` handler to show banner and `EventSource.onopen` handler to hide banner in `src/coordinare/dashboard.py`
- [x] T031 [P] [US4] Write tests for `SSEBroadcaster`: broadcast delivers to all subscribers, `put_nowait` drops silently for full queue (no exception), `unsubscribe` removes queue, iterating snapshot prevents RuntimeError when set mutates during broadcast in `tests/unit/test_dashboard.py`
- [x] T032 [P] [US4] Write test for keepalive comment (`": keepalive\n\n"`) yielded when `asyncio.wait_for` times out (mock TimeoutError) in `tests/unit/test_dashboard.py`

**Checkpoint**: US4 complete — dashboard updates live and handles disconnects gracefully.

---

## Phase 7: Polish & Cross-Cutting Concerns

- [x] T033 Run `ruff check src/coordinare/dashboard.py src/coordinare/config.py src/coordinare/__main__.py src/coordinare/daemon.py` and fix any lint issues
- [x] T034 Run full test suite (`.venv/bin/pytest tests/unit/test_dashboard.py tests/unit/test_config.py -v`) and confirm all pass
- [x] T035 [P] Manual E2E walkthrough of all 7 quickstart scenarios in `specs/010-web-dashboard/quickstart.md`
- [x] T036 [P] Verify `_DASHBOARD_HTML` constant is under 15 KB and renders correctly in Chrome and Firefox

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No dependencies — start immediately
- **Foundational (Phase 2)**: Depends on Phase 1 — **BLOCKS all user stories**
- **US1 (Phase 3)**: Depends on Phase 2 — no other story dependencies
- **US2 (Phase 4)**: Depends on Phase 2 — independent of US1 in principle; uses `build_snapshot()` from US1 (share module)
- **US3 (Phase 5)**: Depends on Phase 2 and US1's `DashboardStore` shell; `record_cycle()` is additive
- **US4 (Phase 6)**: Depends on US1 (SSE endpoint exists) and US3 (record_cycle called from daemon)
- **Polish (Phase 7)**: Depends on all user story phases

### User Story Dependencies

- **US1 (P1)**: Blocks US2/US3/US4 (provides the app skeleton and `build_snapshot()` structure)
- **US2 (P2)**: Extends `build_snapshot()` — can start after US1 dashboard module exists
- **US3 (P3)**: Extends `DashboardStore` — can start after US1; adds `record_cycle()` to daemon independently of US2
- **US4 (P4)**: Completes the live-update loop — depends on US1 (SSE endpoint) and US3 (daemon hook exists)

### Within Each User Story

- Module exists (T003) before any story implementation
- `build_snapshot()` stub (T006) before T008 expands it
- `create_dashboard_app()` (T009) before startup wiring (T013)
- Daemon parameter (T012) before daemon hook calls (T026/T027/T029)
- HTML sections written alongside or after the data is available in `build_snapshot()`
- Test tasks marked [P] can run alongside implementation

### Parallel Opportunities

- T002, T004, T005, T006 in Phase 2 can all run in parallel once T003 (module file) exists
- T014–T017 (US1 tests) can run in parallel with each other after T009 exists
- T021/T022 (US2 tests) run in parallel with each other
- T028 (US3 tests) independent of US2 tasks
- T031/T032 (US4 tests) run in parallel with each other
- T033 and T034 in Phase 7 are sequential; T035/T036 parallel with each other after T034

---

## Parallel Example: User Story 1

```bash
# Once T003 (module file) exists, these can run together:
Task: T004 format_phase_label function in src/coordinare/dashboard.py
Task: T005 SSEBroadcaster class in src/coordinare/dashboard.py
Task: T006 DashboardStore shell in src/coordinare/dashboard.py

# Once T008 (build_snapshot) and T009 (app) exist, these can run together:
Task: T014 format_phase_label tests
Task: T015 GET / and GET /events tests
Task: T016 check_port_available tests
Task: T017 middleware logging tests
```

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Complete Phase 1: Config fields
2. Complete Phase 2: Foundational module + helpers
3. Complete Phase 3: US1 — working dashboard showing current state on page load
4. **STOP and VALIDATE**: Navigate to `http://localhost:8090/` with daemon running — confirm phase, card, agent session visible
5. Deploy/demo if ready

### Incremental Delivery

1. **Phase 1+2+3** → MVP: readable snapshot dashboard on first load
2. **+ Phase 4 (US2)** → Health and metrics visible
3. **+ Phase 5 (US3)** → Cycle history table added
4. **+ Phase 6 (US4)** → Live updates and disconnected banner working end-to-end
5. **+ Phase 7** → Full polish and validation

### Task Count Summary

| Phase | Story | Task Count |
|-------|-------|-----------|
| Phase 1: Setup | — | 2 |
| Phase 2: Foundational | — | 5 |
| Phase 3 | US1 (P1) | 10 |
| Phase 4 | US2 (P2) | 5 |
| Phase 5 | US3 (P3) | 6 |
| Phase 6 | US4 (P4) | 4 |
| Phase 7: Polish | — | 4 |
| **Total** | | **36** |

---

## Notes

- [P] tasks = different files or independent scope, no incomplete dependencies
- [Story] label maps each task to a specific user story for traceability
- `_DASHBOARD_HTML` is a Python string constant — no build step, no npm
- Prometheus `_value.get()` / `_sum` / `_count` private attrs: if they prove fragile in tests, store metrics in plain `int`/`float` fields on `DashboardStore` instead
- Commit after each phase checkpoint at minimum
- Run `.venv/bin/pytest` (not `python -m pytest`) and `.venv/bin/ruff check` per project conventions
