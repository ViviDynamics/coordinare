# Tasks: Human Override Controls

**Input**: Design documents from `/specs/031-human-override-controls/`
**Prerequisites**: plan.md (required), spec.md (required for user stories)

**Tests**: Included — the spec explicitly calls for ~15 unit tests.

**Organization**: Tasks are grouped by user story to enable independent implementation and testing of each story.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to (e.g., US1, US2)
- Include exact file paths in descriptions

---

## Phase 1: Setup

**Purpose**: Extend state schema and add the shared override helper

- [x] T001 Add `pending_override` field to CoordinareState in src/coordinare/graph/state.py
- [x] T002 Add `_apply_pending_override()` helper function in src/coordinare/graph/nodes/monitor_performer.py

**Checkpoint**: State field and helper ready — user story implementation can begin

---

## Phase 2: User Story 1 — Dashboard API Overrides (Priority: P1) 🎯 MVP

**Goal**: Operators use dashboard API endpoints to skip a role, restart from a specific role, or veto the lifecycle. The API validates the request and stores a pending override in state.

**Independent Test**: Call each API endpoint and verify the pending override is stored in state; call graph nodes and verify the override is consumed and applied.

### Tests for User Story 1

- [x] T003 [P] [US1] Add tests for POST /api/skip-role (active card, no active card) in tests/unit/test_dashboard.py
- [x] T004 [P] [US1] Add tests for POST /api/restart-from/{role} (valid role, invalid role, no active card) in tests/unit/test_dashboard.py
- [x] T005 [P] [US1] Add tests for POST /api/veto (active card, no active card) in tests/unit/test_dashboard.py
- [x] T006 [P] [US1] Add tests for _apply_pending_override (skip, restart, veto, skip-last-role, None) in tests/unit/graph/nodes/test_monitor_performer.py
- [x] T007 [P] [US1] Add tests for override check at entry of monitor_performer in tests/unit/graph/nodes/test_monitor_performer.py
- [x] T008 [P] [US1] Add tests for override check at entry of dispatch_performer in tests/unit/graph/nodes/test_dispatch_performer.py

### Implementation for User Story 1

- [x] T009 [US1] Add POST /api/skip-role endpoint in src/coordinare/dashboard.py
- [x] T010 [US1] Add POST /api/restart-from/{role} endpoint in src/coordinare/dashboard.py
- [x] T011 [US1] Add POST /api/veto endpoint in src/coordinare/dashboard.py
- [x] T012 [US1] Wire _apply_pending_override check at the top of monitor_performer in src/coordinare/graph/nodes/monitor_performer.py
- [x] T013 [US1] Wire _apply_pending_override check at the top of dispatch_performer in src/coordinare/graph/nodes/dispatch_performer.py
- [x] T014 [US1] Handle skip-role on final lifecycle role (transition to monitoring_pr) in src/coordinare/graph/nodes/monitor_performer.py

**Checkpoint**: Dashboard API overrides fully functional and testable independently

---

## Phase 3: User Story 2 — PR Comment Commands (Priority: P2)

**Goal**: Human reviewers post `/coordinare skip-<role>`, `/coordinare restart-from <role>`, or `/coordinare veto` as PR comments. The coordinare parses these during `classify_human_feedback` and sets a pending override.

**Independent Test**: Add mock PR comments with `/coordinare` commands and verify the state is updated with the correct pending override; verify normal comments still go through classification.

### Tests for User Story 2

- [x] T015 [P] [US2] Add tests for _parse_coordinare_commands (skip, restart-from, veto, no command, mixed text) in tests/unit/graph/nodes/test_classify_human_feedback.py
- [x] T016 [P] [US2] Add tests for classify_human_feedback integration with /coordinare commands in tests/unit/graph/nodes/test_classify_human_feedback.py

### Implementation for User Story 2

- [x] T017 [US2] Add COMMAND_RE regex and _parse_coordinare_commands() function in src/coordinare/graph/nodes/classify_human_feedback.py
- [x] T018 [US2] Wire command parsing at the top of classify_human_feedback before concern classification in src/coordinare/graph/nodes/classify_human_feedback.py
- [x] T019 [US2] Handle invalid role in restart-from command (post error comment or log warning) in src/coordinare/graph/nodes/classify_human_feedback.py

**Checkpoint**: PR comment commands produce identical outcomes to dashboard API overrides

---

## Phase 4: Polish & Cross-Cutting Concerns

**Purpose**: Edge cases and integration validation

- [x] T020 Add edge case test: both dashboard and PR comment override in same cycle (dashboard wins per FR-010) in tests/unit/graph/nodes/test_monitor_performer.py
- [x] T021 Add edge case test: override arrives when card is in idle phase (no-op) in tests/unit/graph/nodes/test_monitor_performer.py
- [x] T022 Add edge case test: restart-from targets role earlier than first role in tests/unit/graph/nodes/test_monitor_performer.py
- [x] T023 Run full test suite and lint check
- [x] T024 Mark spec 031 tasks as complete

---

## Dependencies & Execution Order

### Phase Dependencies

- **Phase 1 (Setup)**: No dependencies — can start immediately
- **Phase 2 (US1 — Dashboard API)**: Depends on Phase 1 completion
- **Phase 3 (US2 — PR Comments)**: Depends on Phase 1 completion; independent of Phase 2
- **Phase 4 (Polish)**: Depends on Phases 2 and 3 completion

### User Story Dependencies

- **User Story 1 (P1)**: Can start after Phase 1 — no dependency on US2
- **User Story 2 (P2)**: Can start after Phase 1 — no dependency on US1 (both set `pending_override`; the helper in Phase 1 consumes it)

### Within Each User Story

- Tests can be written first (all marked [P] within each story)
- Implementation follows tests
- API endpoints before node wiring

### Parallel Opportunities

- All test tasks within US1 (T003-T008) can run in parallel
- All test tasks within US2 (T015-T016) can run in parallel
- US1 and US2 can be implemented in parallel after Phase 1
- Polish edge case tests (T020-T022) can run in parallel

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Complete Phase 1: Setup (T001-T002)
2. Complete Phase 2: US1 Dashboard API (T003-T014)
3. **STOP and VALIDATE**: Test dashboard overrides independently
4. Deploy if ready — operators can already control the lifecycle

### Incremental Delivery

1. Setup → Foundation ready
2. Add US1 (Dashboard API) → Test independently → MVP
3. Add US2 (PR Comments) → Test independently → Full feature
4. Polish → Edge cases covered → Ship

---

## Notes

- Total tasks: 24
- US1 tasks: 12 (6 tests + 6 implementation)
- US2 tasks: 5 (2 tests + 3 implementation)
- Edge case tasks: 3
- Setup tasks: 2
- Housekeeping: 2
- Parallel opportunities: test tasks within each story; US1 and US2 after setup
