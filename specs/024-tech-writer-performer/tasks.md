# Tasks: Tech Writer Performer

**Input**: Design documents from `/specs/024-tech-writer-performer/`

## Format: `[ID] [P?] [Story] Description`

---

## Phase 1: Setup

- [X] T001 Add `"docs_committed"` to `PerformanceState` in `agent/performer/src/performer/models.py` (if not already present); add `docs_files_modified: list[str] = field(default_factory=list)` field to Performance
- [X] T002 Add `"docs_committed"` to `PerformerStatusType` in `agent/performer/src/performer/protocol.py` (if not already present); add `files_modified: list[str]` field to PerformerResponse
- [X] T003 Add `"docs_committed"` to `StatusType` in `src/coordinare/protocol.py` (if not already present); add `files_modified` field to ProtocolResponse
- [X] T004 Update contract test in `tests/contract/test_agent_protocol.py`

---

## Phase 2: US1 — Produce Complete Documentation (P1) 🎯 MVP

- [X] T005 [US1] Add tech writer branch in `handle_status` in `agent/performer/src/performer/main.py`: gated on `perf.role == "documenting"` when backend returns `done`; parse output JSON; commit each documentation file via `commit_file`; return `docs_committed` with `files_modified` list
- [X] T006 [US1] Add `docs_committed` to terminal state guards and message loop break conditions
- [X] T007 [US1] Verify `docs_committed` is in `TERMINAL_SUCCESS_STATES` in `src/coordinare/graph/nodes/monitor_performer.py` (already added in 019)
- [X] T008 [P] [US1] Write unit tests: docs_committed with files; empty diff → docs_committed with empty files_modified; implementer unaffected

---

## Phase 3: US2 — Update After Human Feedback (P2)

- [X] T009 [US2] Verify relay_feedback is forwarded to the tech writer backend on re-dispatch — same mechanism as all other roles; no new code needed
- [X] T010 [P] [US2] Write unit test: tech writer re-dispatch with relay_feedback produces updated docs_committed

---

## Phase 4: US3 — Detect Conventions (P2)

- [X] T011 [US3] Verify convention detection is a backend prompting concern — the performer passes the full diff and existing files to the backend; no performer code needed
- [X] T012 [P] [US3] Write unit test: tech writer dispatch includes architecture_plan_path in payload (via 020 dispatch_performer)

---

## Phase 5: Polish

- [X] T013 Run full test suite and linter
- [X] T014 Verify backward compatibility
- [X] T015 Verify `docs_committed` in TERMINAL_SUCCESS_STATES
