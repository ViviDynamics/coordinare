# Tasks: QA Performer

**Input**: Design documents from `/specs/023-qa-performer/`

## Format: `[ID] [P?] [Story] Description`

---

## Phase 1: Setup

- [X] T001 Add `QA_MAX_CYCLES: int = 3` to `agent/performer/src/performer/config.py`
- [X] T002 Add `"qa_passed"` and `"qa_failed"` to `PerformanceState` in `agent/performer/src/performer/models.py`; add `qa_failures: list[dict]`, `qa_new_tests: list[str]`, `qa_cycle: int = 0` fields to Performance
- [X] T003 Add `"qa_passed"` and `"qa_failed"` to `PerformerStatusType` in `agent/performer/src/performer/protocol.py`; add `report: dict | None = None` and `failures: list[dict]` to PerformerResponse (reuse existing `findings` or add `failures`)
- [X] T004 Add `"qa_passed"` and `"qa_failed"` to `StatusType` in `src/coordinare/protocol.py`; add `report` and `failures` fields to ProtocolResponse
- [X] T005 Update contract test in `tests/contract/test_agent_protocol.py`
- [X] T006 Write model unit tests in `agent/performer/tests/unit/test_models.py`

---

## Phase 2: Foundational

- [X] T007 No new GitHub helper needed — QA uses existing `commit_file()` from 020 for new test files

---

## Phase 3: US1 — All Acceptance Criteria Pass (P1) 🎯 MVP

- [X] T008 [US1] Add QA branch in `handle_status` in `agent/performer/src/performer/main.py`: parse backend output JSON; commit new test files via `commit_file`; if no failures return `qa_passed` with report
- [X] T009 [US1] Add `qa_passed`/`qa_failed` to terminal state guards and message loop break conditions
- [X] T010 [US1] Verify `qa_passed` in `TERMINAL_SUCCESS_STATES` in `src/coordinare/graph/nodes/monitor_performer.py`
- [X] T011 [P] [US1] Write unit tests: qa_passed with report; new tests committed; implementer unaffected

---

## Phase 4: US2 — Report Failures to Implementer (P1)

- [X] T012 [US2] In QA branch: if failures exist, increment `qa_cycle`; if max cycles → blocked; else return `qa_failed` with failures list
- [X] T013 [US2] Add `qa_failed` handling to `src/coordinare/graph/nodes/monitor_performer.py`: extract failures, store as `relay_feedback`, reset to `implementing`, set `phase = "dispatching"`
- [X] T014 [P] [US2] Write performer tests: qa_failed with failures; max cycle → blocked
- [X] T015 [P] [US2] Write coordinare test: qa_failed routes to implementer

---

## Phase 5: US3 — Environment Failure (P2)

- [X] T016 [US3] Verify existing `blocked` path handles environment failures (backend returns blocked with questions) — no new code needed if blocked path is role-agnostic
- [X] T017 [P] [US3] Write unit test: QA backend returns blocked with environment error

---

## Phase 6: Polish

- [X] T018 Run full test suite and linter
- [X] T019 Verify backward compatibility
- [X] T020 Verify `qa_passed` in TERMINAL_SUCCESS_STATES
