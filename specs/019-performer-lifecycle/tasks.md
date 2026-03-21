# Tasks: Performer Lifecycle

**Input**: Design documents from `/specs/019-performer-lifecycle/`
**Prerequisites**: plan.md (required), spec.md (required for user stories), research.md, data-model.md, quickstart.md

**Organization**: Tasks are grouped by user story to enable independent implementation and testing of each story.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to (e.g., US1, US2, US3)
- Include exact file paths in descriptions

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Config model extensions and CoordinareState fields that all user stories depend on

- [X] T001 Add `PerformerRoleConfig` and `PerformersConfig` Pydantic models to `src/coordinare/config.py`: `PerformerRoleConfig(backend: str = "opencode", transport: str | None = None, image: str | None = None, executable: str | None = None, host: str | None = None, port: int | None = None, timeout_seconds: int | None = None)`; `PerformersConfig` with optional fields for all 8 roles; add `performers: PerformersConfig = PerformersConfig()` to `ProjectConfiguration`
- [X] T002 Write unit tests for performer config parsing in `tests/unit/test_config.py`: test `PerformersConfig` defaults all roles to None; test `PerformerRoleConfig` defaults; test config with two roles configured; test missing `performers` key loads as default; test invalid role field type raises ValidationError
- [X] T003 Extend `CoordinareState` in `src/coordinare/graph/state.py`: add `performer_stage: str` (default `"implementing"`), `performer_services: dict[str, Any]` (default `{}`), `lifecycle_sequence: list[str]` (default `["implementing"]`)

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Service bootstrap and lifecycle sequence derivation — MUST complete before user stories

**⚠️ CRITICAL**: No user story work can begin until this phase is complete

- [X] T004 Implement `_build_lifecycle_sequence(config: ProjectConfiguration) -> list[str]` in `src/coordinare/__main__.py`: derive ordered sequence from canonical order `["advocate", "assessing", "architecting", "implementing", "reviewing", "security", "qa", "documenting"]` filtered by which roles are non-None in `config.performers`; if no roles configured AND no legacy `agent_service`, raise startup error (FR-014); if no roles configured but legacy `agent_service` exists, fall back to `["implementing"]`
- [X] T005 Implement `_build_performer_services(config, circuit_breakers) -> dict[str, AgentServiceProtocol]` in `src/coordinare/__main__.py`: iterate configured roles, build transport + AgentService for each, wrap in `ResilientAgentService`; return mapping of role name → service
- [X] T006 Update `_bootstrap_services` in `src/coordinare/__main__.py` to call `_build_lifecycle_sequence` and `_build_performer_services`, storing results in `service_state["performer_services"]`, `service_state["lifecycle_sequence"]`, and `service_state["performer_stage"]` (set to first element of sequence)
- [X] T007 Write unit tests for lifecycle sequence derivation in `tests/unit/test_bootstrap.py` (new file): test canonical ordering is respected; test roles absent from config are excluded; test empty config with legacy agent_service falls back to `["implementing"]`; test empty config with no legacy service raises error

**Checkpoint**: Foundation ready — `performer_services` registry and `lifecycle_sequence` are available in `CoordinareState`.

---

## Phase 3: User Story 1 — Sequential Role Advancement (Priority: P1) 🎯 MVP

**Goal**: Card progresses through a configurable sequence of performer roles automatically. On success the card advances; on error it blocks.

**Independent Test**: Dispatch a card with stub performer services for each role (returning immediate success), verify `performer_stage` advances through all configured stages and the card ends in `monitoring_pr`.

### Implementation for User Story 1

- [X] T008 [P] [US1] Create `src/coordinare/graph/nodes/dispatch_performer.py`: read `performer_stage` from state, resolve service from `performer_services[performer_stage]`; if service is None, call `_advance_stage(state)` to skip; otherwise run health check, prepare workspace, dispatch card, and return `{"phase": "monitoring_performer", ...}`; inject persona instructions via `load_personas_hot` (018 pattern); when `relay_feedback` is present in state, include it in the dispatch payload sent to the service (FR-010); contain zero role-specific logic (FR-003)
- [X] T009 [P] [US1] Create `src/coordinare/graph/nodes/monitor_performer.py`: read `performer_stage` from state, resolve service from `performer_services[performer_stage]`, poll `check_status(session_id)`; on terminal success states (`pr_opened`, `plan_committed`, `approved`, `security_passed`, `qa_passed`, `docs_committed`) call `_advance_stage`; on error set `phase = "blocked"` (FR-006); on in-progress return unchanged state; contain zero role-specific logic (FR-004)
- [X] T010 [US1] Implement `_advance_stage(state, status=None) -> dict` as a shared helper (in `monitor_performer.py` or a shared module): find current stage index in `lifecycle_sequence`, advance to next; if no more roles, transition to `phase = "monitoring_pr"` (FR-007); reset `agent_dispatch` and `agent_dispatch_at`
- [X] T011 [US1] Update `src/coordinare/graph/builder.py`: replace `dispatch_card` node with `dispatch_performer`; replace `monitor_agent` node with `monitor_performer`; update routing: `"dispatching"` → `dispatch_performer`, `"monitoring_performer"` → `monitor_performer`; keep existing `"monitoring_pr"` → `monitor_pr` edge
- [X] T012 [US1] Update `src/coordinare/graph/routing.py` (if phase-based routing conditions reference old node names or old phase strings): change `"monitoring_agent"` to `"monitoring_performer"` in all routing conditions
- [X] T013 [US1] Deprecate old nodes: modify `src/coordinare/graph/nodes/dispatch_card.py` to be a thin wrapper that delegates to `dispatch_performer`; modify `src/coordinare/graph/nodes/monitor_agent.py` to delegate to `monitor_performer`; add deprecation log warnings; write minimal tests in `tests/unit/graph/nodes/test_dispatch_card.py` and `tests/unit/graph/nodes/test_monitor_agent.py` verifying the wrappers delegate to `dispatch_performer` and `monitor_performer` respectively
- [X] T014 [P] [US1] Write unit tests in `tests/unit/graph/nodes/test_dispatch_performer.py`: test dispatch resolves correct service based on `performer_stage`; test skip when service is None (advances to next stage); test health check failure sets blocked; test persona instructions are injected; test backward-compat with implementer-only lifecycle; test that when state contains `relay_feedback`, the dispatch payload includes the feedback content
- [X] T015 [P] [US1] Write unit tests in `tests/unit/graph/nodes/test_monitor_performer.py`: test `pr_opened` triggers advancement to next stage; test advancement from final stage transitions to `monitoring_pr`; test `error` status sets `phase = "blocked"` and does not advance `performer_stage`; test in-progress returns unchanged state; test all terminal success states are recognized; include a test case with 4 configured roles (implementer, reviewer, security, QA) verifying full sequence advancement ending in `monitoring_pr` (SC-001)
- [X] T016 [US1] Run full test suite: `.venv/bin/pytest tests/unit/ -q` — confirm no regressions

**Checkpoint**: US1 complete. A card with multiple configured roles advances through all stages sequentially. Quickstart Scenarios 1, 2, and 3 pass.

---

## Phase 4: User Story 2 — Shared Dispatch and Monitor Nodes (Priority: P1)

**Goal**: Verify that dispatch_performer and monitor_performer are truly generic — adding a new role requires only config, no graph code changes.

**Independent Test**: Register a new test-only role in `performer_services` and verify dispatch_performer dispatches to it without code changes.

### Implementation for User Story 2

- [X] T017 [US2] Write unit tests in `tests/unit/graph/nodes/test_dispatch_performer.py` (extend existing): test that adding a new role name (e.g., `"custom_role"`) to `performer_services` and `lifecycle_sequence` results in dispatch to that role's service — no graph code changes needed (SC-002); test that dispatch_performer contains no role-specific branching
- [X] T018 [US2] Write unit tests in `tests/unit/graph/nodes/test_monitor_performer.py` (extend existing): test that monitor_performer polls the correct service for each stage in a multi-role lifecycle; verify no role-specific logic in the node
- [X] T019 [US2] Verify backward compatibility: write test in `tests/unit/test_bootstrap.py` that creates a config with no `performers:` key and confirms `lifecycle_sequence = ["implementing"]` and the legacy `agent_service` is used as the implementer's service

**Checkpoint**: US2 complete. SC-002 verified — new roles require only config entries.

---

## Phase 5: User Story 3 — Human Feedback Classification and Rerouting (Priority: P2)

**Goal**: PR comments are classified by concern type and routed to the earliest affected performer role, re-entering the lifecycle.

**Independent Test**: Post a PR comment with a known concern and verify `performer_stage` is reset to the expected role.

### Implementation for User Story 3

- [X] T020 [P] [US3] Create `src/coordinare/graph/nodes/classify_human_feedback.py`: define `CONCERN_TO_STAGE` mapping (implementation → implementing, architecture → architecting, security → security, documentation → documenting, qa → qa, review → reviewing); read PR comments via github service; use assessment backend (Claude) to classify comments; on approval → set `phase = "merging"` (FR-011); on concern → reset `performer_stage` to earliest affected role (FR-008), include PR comments as `relay_feedback` in state (FR-010); if target stage not in `lifecycle_sequence`, fall back to `"implementing"` (FR-009)
- [X] T021 [US3] Update `src/coordinare/graph/builder.py`: add `classify_human_feedback` node; add routing edge from `"monitoring_pr"` → `classify_human_feedback` when new unprocessed PR comments are detected (wire into existing `monitor_pr` → classify flow)
- [X] T022 [US3] Update `src/coordinare/graph/routing.py`: add routing condition for `relay_feedback` phase → `dispatch_performer` so feedback-triggered re-dispatch goes through the shared dispatch node
- [X] T023 [P] [US3] Write unit tests in `tests/unit/graph/nodes/test_classify_human_feedback.py`: test implementation concern routes to `"implementing"`; test architecture concern routes to `"architecting"`; test security concern routes to `"security"`; test documentation concern routes to `"documenting"`; test human approval sets `phase = "merging"` without re-dispatch; test unknown concern defaults to `"implementing"`; test concern targeting unconfigured role falls back to `"implementing"`; test PR comments are included in state as `relay_feedback`
- [X] T024 [US3] Run full test suite: `.venv/bin/pytest tests/unit/ -q` — confirm no regressions

**Checkpoint**: US3 complete. Quickstart Scenarios 4 and 5 pass. SC-003 and SC-005 verified.

---

## Phase 6: User Story 4 — Per-Role Backend Configuration (Priority: P2)

**Goal**: Each role can use a different AI backend/transport, configured independently in `config.yaml`.

**Independent Test**: Configure two roles with different backends, dispatch a card, verify each role uses the correct backend.

### Implementation for User Story 4

- [X] T025 [US4] Update `_build_performer_services` in `src/coordinare/__main__.py` to read each role's `PerformerRoleConfig` fields (backend, transport, executable, host, port, image, timeout_seconds) and construct the appropriate transport/service for each role independently; roles with different backends should produce different `AgentService` instances
- [X] T026 [US4] Write unit tests in `tests/unit/test_bootstrap.py` (extend): test two roles with different `backend` values produce distinct services; test role with `transport: "subprocess"` and role with `transport: "kubernetes"` each get the correct transport; test role with `executable` override uses that executable; test role absent from config is not in `performer_services`
- [X] T027 [US4] Write integration-style test: configure `config.yaml` with implementer (opencode/subprocess) and security (claude-code/subprocess), build services, verify each service has the expected backend identifier

**Checkpoint**: US4 complete. SC-002 and SC-006 verified.

---

## Phase 7: Polish & Cross-Cutting Concerns

- [X] T028 Update `src/coordinare/dashboard.py` phase descriptions to include `"monitoring_performer"` label (replace or supplement `"monitoring_agent"` description); update `format_phase_label` to handle the new phase string
- [X] T029 Update `src/coordinare/graph/state.py` `AdvocateServiceProtocol` or any protocol types that reference old phase names
- [X] T030 Run full test suite: `.venv/bin/pytest tests/unit/ -q` — confirm all pass and coverage does not decrease
- [X] T031 Run linter: `.venv/bin/ruff check src/coordinare/graph/nodes/dispatch_performer.py src/coordinare/graph/nodes/monitor_performer.py src/coordinare/graph/nodes/classify_human_feedback.py src/coordinare/__main__.py src/coordinare/graph/builder.py src/coordinare/graph/routing.py` — zero warnings
- [X] T032 Validate Quickstart Scenario 1 (backward compat): config with no `performers` key → `lifecycle_sequence = ["implementing"]`; identical behavior to pre-019
- [X] T033 Validate SC-004: board column is always "In Progress" while any performer role is active — verify dashboard never exposes `performer_stage` to end users

---

## Edge Cases (scoping decision)

The following edge cases from spec.md are **deferred** to future work (not in 019 scope):

- **PR closed without merging**: Will be addressed when the `monitor_pr` node is enhanced (future spec)
- **Two conflicting human comments**: `classify_human_feedback` picks the earliest affected role; no special conflict resolution needed — this is handled by design (routes to earliest)
- **Performer service unreachable at dispatch time**: Handled by existing `ResilientAgentService` circuit breaker; no new code needed

The following are **in-scope** and covered by existing tasks:

- **No entry in `performer_services` for current stage**: T008 skip logic (dispatch_performer advances when service is None)
- **`classify_human_feedback` cannot determine target role**: T020/T023 — defaults to `"implementing"`
- **Next role pushes to same branch**: Assumed by spec (all roles push to same branch); no special handling needed

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No dependencies — can start immediately
- **Foundational (Phase 2)**: Depends on Setup (Phase 1) — BLOCKS all user stories
- **US1 (Phase 3)**: Depends on Foundational (Phase 2) — core lifecycle machinery
- **US2 (Phase 4)**: Depends on US1 (Phase 3) — extends dispatch/monitor generality tests
- **US3 (Phase 5)**: Depends on US1 (Phase 3) — classify_human_feedback re-enters the lifecycle built in US1
- **US4 (Phase 6)**: Depends on Foundational (Phase 2) — can proceed in parallel with US1/US3
- **Polish (Phase 7)**: Depends on all user stories being complete

### User Story Dependencies

- **US1 (P1)**: Depends on Foundational only — core MVP
- **US2 (P1)**: Depends on US1 — verifies generality of US1 implementation
- **US3 (P2)**: Depends on US1 — needs lifecycle advancement to re-enter
- **US4 (P2)**: Depends on Foundational only — config/bootstrap work; can parallel with US1

### Within Each User Story

- Models/config before services
- Services before graph nodes
- Graph nodes before builder/routing changes
- Builder/routing before integration tests

### Parallel Opportunities

- T008 and T009 can run in parallel (different new files)
- T014 and T015 can run in parallel (different test files)
- T020 and T023 can run in parallel (implementation and test files)
- US4 (Phase 6) can start in parallel with US1 once Foundational is done

---

## Parallel Example: User Story 1

```bash
# Launch new node implementations in parallel:
Task T008: "Create dispatch_performer.py"
Task T009: "Create monitor_performer.py"

# Launch tests in parallel:
Task T014: "Write test_dispatch_performer.py"
Task T015: "Write test_monitor_performer.py"
```

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Complete Phase 1: Setup (config models + state fields)
2. Complete Phase 2: Foundational (bootstrap + lifecycle sequence)
3. Complete Phase 3: User Story 1 (dispatch_performer + monitor_performer + graph wiring)
4. **STOP and VALIDATE**: Test lifecycle advancement end-to-end
5. This delivers the core multi-role sequential lifecycle

### Incremental Delivery

1. Setup + Foundational → config and bootstrap ready
2. US1 → multi-role lifecycle works → MVP!
3. US2 → generality verified → no role-specific code in nodes
4. US3 → human feedback classification and re-routing → full lifecycle loop
5. US4 → per-role backend flexibility → operator configurability
6. Polish → dashboard updates, lint, coverage

### Notes

- [P] tasks = different files, no dependencies
- [Story] label maps task to specific user story
- Existing `dispatch_card` and `monitor_agent` are deprecated, not deleted — thin wrappers delegate to new nodes
- Phase string rename: `"monitoring_agent"` → `"monitoring_performer"` throughout
- Backward compat: no `performers` key in config → implementer-only, identical to pre-019
