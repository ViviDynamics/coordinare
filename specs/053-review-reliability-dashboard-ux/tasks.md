# Tasks: Review Reliability and Dashboard UX Completion

**Input**: Design documents from `/specs/053-review-reliability-dashboard-ux/`
**Prerequisites**: plan.md (required), spec.md (required), research.md, data-model.md, quickstart.md

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to (e.g., US1, US2)
- Include exact file paths in descriptions

---

## Phase 1: Setup

**Purpose**: Shared groundwork for reliability and dashboard updates

- [X] T001 Add role output-contract helper(s) in `agent/performer/src/performer/main.py` to identify JSON-required roles and expected keys per role
- [X] T002 Add shared prompt-tail helper signature in backend adapters (`agent/performer/src/performer/backends/codex.py`, `agent/performer/src/performer/backends/opencode.py`, `agent/performer/src/performer/backends/claude_code.py`) to switch behavior by `score.role`
- [X] T003 Add dashboard snapshot placeholders for board summary in `src/coordinare/dashboard.py` (`board_summary` + `last_poll_at`) without changing render behavior yet

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Reliability primitives that all story-level behavior depends on

**CRITICAL**: User-story work should start only after this phase

- [X] T004 Implement role-aware prompt tail in all backend prompt builders so reviewer-style roles end with strict JSON-only instructions instead of commit/push instructions
- [X] T005 Implement structured format-recovery flow for JSON-required roles in `agent/performer/src/performer/main.py` (repair attempt before terminal parse failure)
- [X] T006 Add machine-detectable backend format error marker/prefix in performer terminal error reason in `agent/performer/src/performer/main.py`
- [X] T007 Add monitor-side classification in `src/coordinare/graph/nodes/monitor_performer.py` to route format failures through retryable system-error path before final blocking
- [X] T008 [P] Add/extend unit tests for parse recovery and marker behavior in `agent/performer/tests/unit/test_main.py`
- [X] T009 [P] Add/extend unit tests for monitor classification/routing in `tests/unit/graph/nodes/test_monitor_performer.py`

**Checkpoint**: Non-JSON reviewer output no longer hard-blocks immediately; reliability path is test-covered

---

## Phase 3: User Story 1 - Reviewer output failures do not hard-block normal flow (Priority: P1)

**Goal**: Parse failures in reviewer-style stages self-heal or fail diagnostically without brittle immediate block behavior

**Independent Test**: Simulated first-pass non-JSON reviewer output recovers and continues lifecycle

- [X] T010 [US1] Add test: first non-JSON output triggers recovery and second valid JSON completes reviewer path in `agent/performer/tests/unit/test_main.py`
- [X] T011 [US1] Add test: exhausted recovery includes redacted diagnostic preview and retry metadata in `agent/performer/tests/unit/test_main.py`
- [X] T012 [US1] Add test: monitor_performer treats format-marker error as retryable system error in `tests/unit/graph/nodes/test_monitor_performer.py`
- [X] T013 [US1] Wire final error->system_error fallback budget logic for format failures in `src/coordinare/graph/nodes/monitor_performer.py`

---

## Phase 4: User Story 2 - Active Performers idle observability (Priority: P1)

**Goal**: Idle tile shows board summary and poll timing

**Independent Test**: With no active sessions, dashboard shows board counts and last poll

- [X] T014 [US2] Compute board counts from `state["board_snapshot"]` and include in snapshot payload in `src/coordinare/dashboard.py`
- [X] T015 [US2] Include `last_poll_at` in snapshot payload serialization in `src/coordinare/dashboard.py`
- [X] T016 [US2] Update `renderActivePerformers(s)` idle message to include board summary + last poll + filter hint in `src/coordinare/dashboard.py`
- [X] T017 [US2] Add unit tests for new snapshot fields and idle render contract in `tests/unit/test_dashboard.py`

---

## Phase 5: User Story 3 - Functional History page (Priority: P2)

**Goal**: `/history` renders real cycle history data

**Independent Test**: `/history` shows cycle table rows and updates via SSE

- [X] T018 [US3] Replace history stub markup in `_DASHBOARD_HTML` with table/empty-state containers in `src/coordinare/dashboard.py`
- [X] T019 [US3] Add `renderHistoryPage(s)` and route wiring so `/history` renders from `s.cycle_history` in `src/coordinare/dashboard.py`
- [X] T020 [US3] Update unit tests to assert no "Coming soon" placeholder and history render hooks in `tests/unit/test_dashboard.py`
- [X] T021 [US3] Update e2e route test expectations for `/history` in `tests/e2e/test_dashboard_browser.py`

---

## Phase 6: User Story 4 - Performers page drilldown details (Priority: P2)

**Goal**: Click role rows on `/performers` to open detail panel

**Independent Test**: Row click opens detail; back returns to list

- [X] T022 [US4] Add selectable role rows (`data-role`) and detail panel container on `/performers` page markup in `src/coordinare/dashboard.py`
- [X] T023 [US4] Reuse existing performer detail render logic for `/performers` selected role context in `src/coordinare/dashboard.py`
- [X] T024 [US4] Add list/detail state handlers for `/performers` route in `src/coordinare/dashboard.py`
- [X] T025 [US4] Add e2e test for performers row drilldown and back behavior in `tests/e2e/test_dashboard_browser.py`

---

## Phase 7: User Story 5 - Desktop layout correction for workflow + performers cards (Priority: P2)

**Goal**: Workflow and performers cards each use one desktop column

**Independent Test**: At >=900px both cards render side-by-side as one-column cards

- [X] T026 [US5] Remove `.full` usage for workflow and compact performers cards in dashboard markup (`src/coordinare/dashboard.py`)
- [X] T027 [US5] Update desktop CSS grid behavior to keep cards side-by-side while preserving mobile stacking in `src/coordinare/dashboard.py`
- [X] T028 [US5] Remove/disable mandatory workflow expand-collapse affordance from default UX in `src/coordinare/dashboard.py`
- [X] T029 [US5] Add/update e2e layout assertions in `tests/e2e/test_dashboard_browser.py`

---

## Phase 8: UX / Design Hardening

**Purpose**: Convert the UX plan into explicit implementation and test coverage

- [X] T030 [P] [US4] Add keyboard-accessible performer row interactions (Enter/Space) and semantic button/role wiring in `src/coordinare/dashboard.py`
- [X] T031 [US4] Add visible focus states for clickable rows and in-page back controls in `src/coordinare/dashboard.py`
- [X] T032 [P] [US2] Standardize idle/empty-state copy patterns across dashboard, performers, and history sections in `src/coordinare/dashboard.py`
- [X] T033 [US5] Normalize status badge usage and wording consistency across routes (`active/idle`, `success/error`, subsystem labels) in `src/coordinare/dashboard.py`
- [X] T034 [US4] Add e2e coverage for keyboard drilldown and detail-state stability during SSE updates in `tests/e2e/test_dashboard_browser.py`

---

## Phase 9: Polish & Cross-Cutting

- [X] T035 Run `agent/performer` unit tests for parse-reliability updates: `.venv/bin/pytest agent/performer/tests/unit/test_main.py -q`
- [X] T036 Run coordinare unit tests touched by monitor/dashboard updates: `.venv/bin/pytest tests/unit/graph/nodes/test_monitor_performer.py tests/unit/test_dashboard.py -q`
- [X] T037 Run dashboard e2e smoke subset: `.venv/bin/pytest tests/e2e/test_dashboard_browser.py -q`
- [X] T038 Run lint on changed files: `.venv/bin/ruff check src/coordinare/dashboard.py src/coordinare/graph/nodes/monitor_performer.py agent/performer/src/performer/main.py agent/performer/src/performer/backends/codex.py agent/performer/src/performer/backends/opencode.py agent/performer/src/performer/backends/claude_code.py`

---

## Dependencies & Execution Order

### Phase dependencies

- Setup (Phase 1): no dependencies
- Foundational (Phase 2): depends on Setup; blocks reliable behavior stories
- US1 (Phase 3): depends on Foundational
- US2 (Phase 4): depends on Setup snapshot fields; can run in parallel with US1 after Phase 1
- US3/US4/US5 (Phases 5-7): can proceed in parallel after dashboard foundation in US2
- UX hardening (Phase 8): depends on completion of core dashboard behavior in Phases 5-7
- Polish (Phase 9): after all implementation phases

### Story dependencies

- US1 is independent of dashboard UX stories after foundational reliability work
- US2 feeds data used by US3 but can ship independently
- US4 and US5 are UI-only and can proceed independently once routing remains stable
- Phase 8 UX tasks depend on US3-US5 UI surfaces existing and should run before final polish

## Implementation Strategy

1. Complete reliability foundation first (Phases 1-3) to remove operational blocker.
2. Deliver idle summary + history page (Phases 4-5) as first dashboard UX increment.
3. Deliver performers drilldown + layout fix (Phases 6-7).
4. Apply UX hardening and accessibility consistency pass (Phase 8).
5. Run focused unit/e2e/lint pass (Phase 9).

## Notes

- Keep dashboard route shell and SSE connection model unchanged.
- Prefer additive snapshot changes to avoid breaking existing consumers.
- Ensure all format-failure diagnostics remain redacted before surfacing in operator-visible state.
