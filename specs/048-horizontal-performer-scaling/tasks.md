# Tasks: Horizontal Performer Scaling

**Input**: Design documents from `/specs/048-horizontal-performer-scaling/`
**Prerequisites**: plan.md (required), spec.md (required), research.md, data-model.md, contracts/

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to (e.g., US1, US2, US3)
- Include exact file paths in descriptions

## Phase 1: Setup

**Purpose**: New files and shared infrastructure for slot management

- [x] T001 [P] Create SlotManager, RolePool, PerformerSlot, SlotState classes in src/coordinare/services/slot_manager.py — SlotManager with acquire/release/active_count/is_at_capacity/utilization/sync_from_sessions methods; RolePool with max_concurrency + services list + active_slots dict; PerformerSlot dataclass with role/card_id/session_id/service_index/started_at/state fields; SlotState enum (running/stopping/crashed)
- [x] T002 Add SINGLETON_STAGES frozenset ({"assessing", "closing_review"}) to src/coordinare/lifecycle.py

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Config extension, startup wiring, and SlotManager core logic — all user stories depend on these

**⚠️ CRITICAL**: No user story work can begin until this phase is complete

- [x] T003 Add max_concurrency field (int, default 1, ge=0) to PerformerRoleConfig in src/coordinare/config.py
- [x] T004 Implement SlotManager.acquire(stage, card_id) in src/coordinare/services/slot_manager.py — check active_count < max_concurrency (read from config hot-reload), clamp singletons to 1 with warning; return AgentService from the pool's services list at the first free index, or None if at capacity
- [x] T005 Implement SlotManager.release(stage, card_id) in src/coordinare/services/slot_manager.py — remove the slot from active_slots, freeing the service index for reuse
- [x] T006 Implement SlotManager.sync_from_sessions(active_sessions) in src/coordinare/services/slot_manager.py — rebuild active_slots from session phases; free slots for sessions that completed or crashed since last sync
- [x] T007 Implement SlotManager.utilization() in src/coordinare/services/slot_manager.py — return list of {role, active, max, queued} dicts for dashboard
- [x] T008 Modify _build_performer_services in src/coordinare/__main__.py — create max_concurrency AgentService instances per role (each with its own transport); stash full per-role lists on _build_performer_services._service_lists for SlotManager; performer_services keeps dict[str, AgentService] (primary per role) for backward compat; clamp SINGLETON_STAGES; log warning when max_concurrency=0 disables a role
- [x] T009 Create and inject SlotManager into state during daemon bootstrap in src/coordinare/__main__.py — after building performer_services, create SlotManager with RolePools; add to initial state
- [x] T010 Add slot_manager field (Any | None, default None) to CoordinareState in src/coordinare/graph/state.py and initial_state()
- [x] T011 [P] Write unit tests for SlotManager.acquire in tests/unit/services/test_slot_manager.py — cover: free slot available, at capacity returns None, singleton clamped to 1, max_concurrency=0 returns None
- [x] T012 [P] Write unit tests for SlotManager.release in tests/unit/services/test_slot_manager.py — cover: slot freed, release unknown card_id is no-op
- [x] T013 [P] Write unit tests for SlotManager.sync_from_sessions in tests/unit/services/test_slot_manager.py — cover: completed sessions freed, crashed sessions freed, active sessions retained
- [x] T014 [P] Write unit tests for SlotManager.utilization in tests/unit/services/test_slot_manager.py — cover: mixed active/idle/queued roles

**Checkpoint**: SlotManager fully tested. Can acquire, release, sync, and report utilization.

---

## Phase 3: User Story 1 — Multiple cards served concurrently (Priority: P1) 🎯 MVP

**Goal**: Cards dispatch to parallel performer instances based on per-role max_concurrency. When a slot frees up, the next queued card dispatches within one poll cycle.

**Independent Test**: Configure `implementer.max_concurrency: 2`, place 3 cards in TODO. Verify 2 implementers run concurrently; when one finishes, the third starts.

- [x] T015 [US1] Modify dispatch_performer to resolve service via SlotManager.acquire instead of direct dict lookup in src/coordinare/graph/nodes/dispatch_performer.py — call slot_manager.acquire(performer_stage, card_id); if None (at capacity), return state with phase unchanged ("dispatching") so the card retries next cycle; if service returned, proceed with dispatch as normal
- [x] T016 [US1] Modify monitor_performer to call SlotManager.release on terminal statuses in src/coordinare/graph/nodes/monitor_performer.py — after any terminal marker (pr_opened, approved, changes_requested, security_passed/failed, qa_passed/failed, docs_committed, error, blocked), call slot_manager.release(stage, card_id)
- [x] T017 [US1] Call SlotManager.sync_from_sessions at the start of each daemon cycle in src/coordinare/daemon.py — before _invoke_multi_session, sync the SlotManager with current active_sessions to free stale slots from crashed/expired sessions
- [x] T018 [US1] Update performer_services type annotation throughout codebase — change dict[str, Any] to dict[str, list[Any]] where performer_services is referenced (state.py, dispatch_performer.py, __main__.py)
- [x] T019 [US1] Write test for dispatch_performer at capacity in tests/unit/graph/nodes/test_dispatch_performer.py — mock SlotManager.acquire returning None; verify phase stays "dispatching"
- [x] T020 [US1] Write test for dispatch_performer with free slot in tests/unit/graph/nodes/test_dispatch_performer.py — mock SlotManager.acquire returning a service; verify dispatch proceeds normally
- [x] T021 [US1] Write test for slot freed on terminal status in tests/unit/graph/nodes/test_monitor_performer.py — verify SlotManager.release called after pr_opened/approved/error

**Checkpoint**: Per-role concurrency works. Cards queue when at capacity and dispatch when slots free up.

---

## Phase 4: User Story 2 — Assessor and closer singletons (Priority: P2)

**Goal**: Assessor and closer are hard-capped at max_concurrency=1 regardless of configuration.

**Independent Test**: Configure `assessor.max_concurrency: 5`. Verify only one assessor runs at a time.

- [x] T022 [US2] Verify singleton clamping in SlotManager construction in src/coordinare/services/slot_manager.py — when building RolePool for a singleton role, clamp max_concurrency to min(configured, 1) and log a warning if configured > 1
- [x] T023 [US2] Write test for singleton enforcement in tests/unit/services/test_slot_manager.py — configure assessor max_concurrency=5, verify acquire only succeeds for 1 slot; second acquire returns None
- [x] T024 [US2] Write test for closer singleton in tests/unit/services/test_slot_manager.py — configure closer max_concurrency=3, verify clamped to 1

**Checkpoint**: Singletons enforced. Assessor and closer never run more than one instance.

---

## Phase 5: User Story 3 — Dashboard utilization view (Priority: P3)

**Goal**: Dashboard shows per-role active/max/queued counts so the operator can see which roles are saturated.

**Independent Test**: Multiple roles with different max_concurrency. Dashboard shows correct utilization per role.

- [x] T025 [US3] Add role_utilization to dashboard snapshot in src/coordinare/dashboard.py — call slot_manager.utilization() and include in the SSE payload
- [x] T026 [US3] Add dashboard JS rendering for role utilization in src/coordinare/dashboard.py — render a "Performer Utilization" card with a per-role table showing role name, active/max bar, and queued count
- [~] T027 [US3] Add role_utilization to notification event payload for card_dispatched in src/coordinare/graph/nodes/notify.py — deferred; dashboard visibility is sufficient for this spec; Slack utilization can be added in a follow-up
- [x] T028 [P] [US3] Write test for dashboard snapshot containing role_utilization in tests/unit/test_dashboard.py — verify field present with correct shape per contract schema
- [x] T029 [P] [US3] Write contract test for dashboard-utilization.json schema in tests/contract/test_dashboard_utilization_contract.py

**Checkpoint**: Full utilization visibility on dashboard and optionally in Slack.

---

## Phase 6: Polish & Cross-Cutting Concerns

- [x] T030 Handle max_concurrency=0 as "role disabled" in dispatch_performer — when SlotManager has no pool for a stage (max=0), skip the role via _advance_stage (same as no service configured)
- [x] T031 Handle config hot-reload reducing max_concurrency in SlotManager — verify active count can exceed max temporarily (draining); new dispatches blocked until active < max
- [x] T032 Run all quickstart.md scenarios (5 scenarios) against a live or mocked environment
- [x] T033 Run .venv/bin/pytest tests/ -q — all tests pass
- [x] T034 Run .venv/bin/ruff check src/ tests/ — lint clean

---

## Dependencies & Execution Order

### Phase Dependencies

- **Phase 1 (Setup)**: No dependencies — can start immediately
- **Phase 2 (Foundational)**: Depends on Phase 1 — BLOCKS all user stories
- **Phase 3 (US1)**: Depends on Phase 2 — the MVP (per-role concurrency)
- **Phase 4 (US2)**: Depends on Phase 2 — independent of US1 (singleton logic is in SlotManager)
- **Phase 5 (US3)**: Depends on Phase 3 (needs SlotManager wired into dispatch to have utilization data)
- **Phase 6 (Polish)**: Depends on all prior phases

### User Story Dependencies

- **US1 (P1)**: Foundational only — MVP standalone
- **US2 (P2)**: Foundational only — independent (singleton enforcement is in SlotManager.acquire)
- **US3 (P3)**: Depends on US1 (needs SlotManager active in the dispatch pipeline)

### Parallel Opportunities

- T001 + T002 (setup) can run in parallel
- T011 + T012 + T013 + T014 (foundational tests) can run in parallel
- T028 + T029 (US3 tests) can run in parallel
- US1 and US2 can be worked on in parallel after Phase 2

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Complete Phase 1: Setup (T001-T002)
2. Complete Phase 2: Foundational (T003-T014)
3. Complete Phase 3: US1 — Per-role concurrency (T015-T021)
4. **STOP and VALIDATE**: Test with 2+ cards and max_concurrency > 1
5. Deploy if ready — concurrent performers are the core value

### Incremental Delivery

1. Setup + Foundational → SlotManager ready
2. US1 → concurrent dispatching → deploy (MVP!)
3. US2 → singleton enforcement → deploy
4. US3 → dashboard utilization → deploy

---

## Notes

- [P] tasks = different files, no dependencies
- performer_services stays dict[str, AgentService] (primary per role) for backward compat; full per-role lists stashed on _build_performer_services._service_lists and registered on the SlotManager
- dispatch_performer and monitor_performer both resolve via SlotManager.acquire (idempotent for monitor) so each card gets its dedicated transport
- SlotManager is stateful (acquire/release) with sync_from_sessions reconciliation each cycle
- Config hot-reload: SlotManager reads max_concurrency from config on each acquire() call, not cached
- Backward compatibility: max_concurrency defaults to 1, so existing single-card deployments see no behavior change
