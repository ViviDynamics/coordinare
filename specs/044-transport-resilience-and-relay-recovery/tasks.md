# Tasks: 044 — Transport Resilience & Relay Recovery

**Input**: Design documents from `/specs/044-transport-resilience-and-relay-recovery/`
**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/

**Tests**: Included per constitution Principle II.

**Organization**: Five user stories mapped from the five oddities:
- **US1**: Transport resilience — skip non-JSON lines (O-1, HIGH)
- **US2**: Relay retry budget — blocked after 3 failures (O-5, HIGH)
- **US3**: CI tool verification — verify installed before running (O-4, MEDIUM)
- **US4**: Persona directive — prevent backend from running CI independently (O-1 root cause)
- **US5**: Tech writer batch commits (O-3, LOW)

## Format: `[ID] [P?] [Story] Description`

---

## Phase 1: Setup

- [x] T001 Create feature branch `044-transport-resilience-and-relay-recovery` from `main` (already done)

---

## Phase 2: Foundational

**Purpose**: No shared foundational work — all stories are independent.

**Checkpoint**: Proceed directly to user stories.

---

## Phase 3: User Story 1 — Transport Resilience (Priority: P1) 🎯 MVP

**Goal**: Transport survives non-JSON lines mixed into performer stdout without erroring. First valid JSON line is accepted.

**Independent Test**: Feed the transport a multi-line buffer with ANSI codes, bare text, and a valid JSON response on the last line. Verify the JSON response is returned.

### Tests for User Story 1

- [x] T002 [P] [US1] Write test in `tests/unit/transport/test_subprocess_transport.py`: `_parse_response` with valid JSON returns ProtocolResponse (existing behavior preserved)
- [x] T003 [P] [US1] Write test: `_parse_response` with ANSI-prefixed line + valid JSON line returns the JSON response
- [x] T004 [P] [US1] Write test: `_parse_response` with multiple non-JSON lines + valid JSON at end returns the JSON response
- [x] T005 [P] [US1] Write test: `_parse_response` with only non-JSON lines raises TransportError
- [x] T006 [P] [US1] Write test: `_parse_response` with empty input raises TransportError

### Implementation for User Story 1

- [x] T007 [US1] In `src/coordinare/transport/subprocess_transport.py`, modify `_parse_response` (line 194-204): split `data` by newlines, iterate lines, skip non-JSON (log at debug), return first valid `ProtocolResponse.model_validate_json()`, raise `TransportError` if none found

**Checkpoint**: Transport survives the exact byte sequences from the 2026-04-14 incident.

---

## Phase 4: User Story 2 — Relay Retry Budget (Priority: P1)

**Goal**: After 3 consecutive session expiries on the same relay, card transitions to blocked with diagnostic message instead of looping forever.

**Independent Test**: Mock 3 consecutive session_expired events during a relay. Verify the 3rd triggers phase=blocked with transport error in open_questions.

### Tests for User Story 2

- [x] T008 [P] [US2] Write test in `tests/unit/graph/nodes/test_monitor_performer.py`: session_expired during relay with system_error_count < 3 resumes monitoring_pr (existing behavior)
- [x] T009 [P] [US2] Write test: session_expired during relay with system_error_count >= 3 transitions to blocked with diagnostic in open_questions
- [x] T010 [P] [US2] Write test: system_error_count resets to 0 after successful dispatch (existing behavior preserved)

### Implementation for User Story 2

- [x] T011 [US2] In `src/coordinare/graph/nodes/monitor_performer.py`, in the session_expired handler (around line 841-884): before resuming monitoring_pr, check if `system_error_count >= 3`; if so, set phase=blocked with open_questions containing the last transport error reason

**Checkpoint**: The 2026-04-14 dispatch loop would have stopped after 3 cycles with a clear blocked notification.

---

## Phase 5: User Story 3 — CI Tool Verification (Priority: P2)

**Goal**: CI detection returns lint_command=None when the detected tool isn't actually installed in the workspace.

**Independent Test**: Create a tmp_path with Gemfile + .rubocop.yml but no rubocop installed. Verify detect() returns lint_command=None.

### Tests for User Story 3

- [x] T012 [P] [US3] Write test in `tests/unit/services/test_ci_detection.py`: `_verify_tool` returns True when tool is available (mock subprocess returning 0)
- [x] T013 [P] [US3] Write test: `_verify_tool` returns False when tool not installed (mock subprocess returning non-zero)
- [x] T014 [P] [US3] Write test: `_verify_tool` returns False on timeout (mock subprocess hanging)
- [x] T015 [P] [US3] Write test: detect() with Gemfile + .rubocop.yml but rubocop not installed returns lint_command=None

### Implementation for User Story 3

- [x] T016 [US3] Add `_verify_tool(command: str, cwd: Path) -> bool` in `src/coordinare/services/ci_detection.py`: runs `{first_word} --version` via subprocess.run with 5s timeout, returns True if exit 0
- [x] T017 [US3] Wire `_verify_tool` into each detector: after determining lint_command, verify it; if not available, set lint_command=None and log warning. Skip verification for `make` targets (no --version support)

**Checkpoint**: Fresh clone without `bundle install` no longer triggers rubocop, preventing stdout contamination at the source.

---

## Phase 6: User Story 4 — Persona Directive Update (Priority: P2)

**Goal**: Performer personas tell the AI backend to NOT independently run CI commands via tool-use.

**Independent Test**: Read the implementer persona and verify it contains the "do NOT run CI commands yourself" directive.

- [x] T018 [US4] Update `_CI_COMMITTER_DIRECTIVE` in `src/coordinare/services/persona_service.py`: append "Do NOT run lint or test commands yourself via tool-use — the coordinare runs CI checks automatically before and after your work. Focus only on reading and fixing code."
- [x] T019 [P] [US4] Write test in `tests/unit/test_persona_service.py`: verify implementer persona contains "Do NOT run lint" directive

**Checkpoint**: Backend won't independently run rubocop via tool-use, eliminating the stdout contamination source.

---

## Phase 7: User Story 5 — Tech Writer Batch Commits (Priority: P3)

**Goal**: Tech writer produces 1 commit per lifecycle pass instead of 14+.

**Independent Test**: Mock the tech writer with 5 doc files. Verify exactly 1 git commit + 1 push occurs.

### Tests for User Story 5

- [x] T020 [P] [US5] Write test in `agent/performer/tests/unit/test_workspace.py`: `commit_files` with 3 files produces 1 commit + 1 push
- [x] T021 [P] [US5] Write test: `commit_files` with empty file list is a no-op (no git commands)

### Implementation for User Story 5

- [x] T022 [US5] Add `commit_files(stand, files, message)` async helper in `agent/performer/src/performer/workspace.py`: write each file to disk, `git add` all paths, single `git commit -m message`, single `git push`
- [x] T023 [US5] In `agent/performer/src/performer/main.py` tech writer handler (line 917-929): replace the per-file `commit_file` loop with single `commit_files(stand, doc_files_list, f"docs(#{issue_num}): update wiki and card documentation")`

**Checkpoint**: PR #94-style tech writer runs produce 1 clean commit instead of 14.

---

## Phase 8: Polish & Cross-Cutting Concerns

- [x] T024 [P] Run `.venv/bin/ruff check src/ tests/ agent/performer/src/` — verify lint clean
- [x] T025 Run `.venv/bin/pytest tests/ -q` — verify all coordinare tests pass
- [x] T026 Run `.venv/bin/pytest agent/performer/tests/ -q` — verify performer tests pass
- [x] T027 Update spec 044 tasks.md with completion status

---

## Dependencies & Execution Order

### Phase Dependencies

- **US1 (Transport)**: No dependencies — can start immediately
- **US2 (Retry Budget)**: No dependencies — can start immediately, parallel with US1
- **US3 (CI Verification)**: No dependencies — parallel with US1/US2
- **US4 (Persona)**: No dependencies — parallel
- **US5 (Batch Commits)**: No dependencies — parallel
- **Polish**: Depends on all stories complete

### Parallel Opportunities

All 5 user stories are independent — different files, no shared state changes. Can run in any order or all in parallel.

Within each story, tests can run in parallel (marked [P]).

---

## Implementation Strategy

### MVP First (US1 + US2)

1. US1: Transport resilience — prevents the crash
2. US2: Relay retry budget — prevents the loop even if transport still fails
3. Together they break both links of the chain that caused the incident

### Full Delivery

4. US3: CI tool verification — prevents the contamination source
5. US4: Persona directive — prevents backend from running CI independently
6. US5: Tech writer batch — commit hygiene improvement

---

## Notes

- Total tasks: 27
- US1 (transport): 6 tasks — MVP, breaks the crash
- US2 (retry budget): 4 tasks — MVP, breaks the loop
- US3 (CI verification): 6 tasks — prevents source
- US4 (persona): 2 tasks — quick win
- US5 (batch commits): 4 tasks — polish
- Polish: 4 tasks
- All 5 stories are fully parallel — no cross-dependencies
