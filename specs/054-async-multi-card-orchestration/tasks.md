# Tasks: Async Multi-Card Orchestration Eligibility

**Input**: Design documents from `/specs/054-async-multi-card-orchestration/`  
**Prerequisites**: plan.md (required), spec.md (required), research.md, data-model.md, quickstart.md

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel
- **[Story]**: User story mapping (US1, US2, US3)

---

## Phase 1: Setup

- [X] T001 Add feature scaffolding comments and typed placeholders for session eligibility in `src/coordinare/daemon.py` and `src/coordinare/graph/state.py`
- [X] T002 Add baseline test fixtures for active-session eligibility scenarios in `tests/unit/test_daemon_coverage.py`

---

## Phase 2: Foundational (Blocking)

- [X] T003 Implement per-cycle session eligibility derivation in `src/coordinare/daemon.py` using board/dependency state
- [X] T004 Implement `session_skip_reasons` state field in `src/coordinare/graph/state.py` and state initialization
- [X] T005 [P] Add unit tests for eligibility classification (`eligible`, `blocked_column`, `dependency_blocked`) in `tests/unit/test_daemon_coverage.py`
- [X] T006 [P] Add unit tests for skip-reason state shape in `tests/unit/graph/nodes/test_check_board.py`

**Checkpoint**: eligible/ineligible sessions are classified deterministically each cycle.

---

## Phase 3: User Story 1 - Eligible cards advance concurrently (P1)

- [X] T007 [US1] Refactor `_invoke_multi_session` in `src/coordinare/daemon.py` to invoke eligible sessions concurrently
- [X] T008 [US1] Add failure-isolation merge logic so failed session ticks do not cancel successful ones in `src/coordinare/daemon.py`
- [X] T009 [US1] Add async fanout tests (multi-eligible sessions in same cycle) in `tests/unit/test_daemon_coverage.py`
- [X] T010 [US1] Add regression test for `max_concurrent_cards=1` compatibility in `tests/unit/test_daemon_coverage.py`

---

## Phase 4: User Story 2 - Ineligible cards are not worked (P1)

- [X] T011 [US2] Ensure BLOCKED-column sessions are skipped without performer invocation in `src/coordinare/daemon.py`
- [X] T012 [US2] Ensure unresolved-dependency sessions are skipped without performer invocation in `src/coordinare/daemon.py`
- [X] T013 [US2] Add tests verifying zero invocation count for skipped sessions in `tests/unit/test_daemon_coverage.py`
- [X] T014 [US2] Add resume-on-unblock test (dependency satisfied -> session resumes) in `tests/unit/test_daemon_coverage.py`

---

## Phase 5: User Story 3 - Post-merge rebase dispatch for in-flight branches (P1)

- [X] T015 [US3] Extend main-sha-change handling to dispatch/reconcile rebase targets for eligible open-PR sessions in `src/coordinare/graph/nodes/check_board.py` and/or `src/coordinare/daemon.py`
- [X] T016 [US3] Add branch-level failure isolation tests for rebase dispatch in `tests/unit/graph/nodes/test_check_board.py`
- [X] T017 [US3] Add regression test ensuring no rebase dispatch when no eligible open PR sessions exist in `tests/unit/graph/nodes/test_check_board.py`

---

## Phase 6: User Story 4 - Skip reason observability (P2)

- [X] T018 [US4] Expose `session_skip_reasons` in snapshot build path in `src/coordinare/dashboard.py`
- [X] T019 [US4] Render concise skip reason diagnostics in active performer view in `src/coordinare/dashboard.py`
- [X] T020 [US4] Add dashboard snapshot tests for skip reason payload coverage in `tests/unit/test_dashboard.py`

---

## Phase 7: User Story 5 - QA freshness gate against latest main (P1)

- [X] T021 [US5] Extend QA contract expectations for freshness check payload in `agent/performer/src/performer/main.py` and related protocol docs/tests
- [X] T022 [US5] Implement QA freshness validation (latest main included in branch) and failure reporting path in `agent/performer/src/performer/main.py`
- [X] T023 [US5] Add QA unit tests for behind-main fail / up-to-date pass / indeterminate blocker in `agent/performer/tests/unit/test_main.py`
- [X] T024 [US5] Update/extend coordinare handling tests where QA freshness failures feed remediation loops in `tests/unit/graph/nodes/test_monitor_performer.py`

---

## Phase 8: Polish & Validation

- [X] T025 Run focused daemon/session tests: `uv run pytest tests/unit/test_daemon_coverage.py -q`
- [X] T026 Run check_board/dashboard tests: `uv run pytest tests/unit/graph/nodes/test_check_board.py tests/unit/test_dashboard.py -q`
- [X] T027 Run QA/monitor tests: `uv run pytest agent/performer/tests/unit/test_main.py tests/unit/graph/nodes/test_monitor_performer.py -q`
- [X] T028 Run lint on touched files: `uv run ruff check src/coordinare/daemon.py src/coordinare/graph/state.py src/coordinare/dashboard.py src/coordinare/graph/nodes/check_board.py agent/performer/src/performer/main.py tests/unit/test_daemon_coverage.py tests/unit/graph/nodes/test_check_board.py tests/unit/test_dashboard.py agent/performer/tests/unit/test_main.py tests/unit/graph/nodes/test_monitor_performer.py`
- [X] T029 Run full local gate: `bin/build`

---

## Dependencies & Execution Order

- Phase 1 -> Phase 2 -> Phases 3/4/5 -> Phase 6 -> Phase 7 -> Phase 8.
- US1 and US2 share the same scheduler surface.
- US3 depends on merge-detection/rebase infrastructure.
- US5 depends on rebase freshness context and QA contract updates.
