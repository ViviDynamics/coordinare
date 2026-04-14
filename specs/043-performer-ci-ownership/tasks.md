# Tasks: 043 — Performer CI Ownership

**Input**: Design documents from `/specs/043-performer-ci-ownership/`
**Prerequisites**: plan.md, spec.md, research.md, data-model.md, contracts/

**Tests**: Included — constitution requires Testing Discipline (Principle II).

**Organization**: Three user stories mapped from spec success criteria:
- **US1**: CI detection service (convention-based command detection)
- **US2**: Performer-side CI enforcement (persona directives + pre-commit check)
- **US3**: Coordinare-side CI gate (defence-in-depth in `_advance_stage`)

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story (US1, US2, US3)
- Exact file paths included in every task

---

## Phase 1: Setup

**Purpose**: Branch, dependencies, project scaffolding

- [x] T001 Create feature branch `043-performer-ci-ownership` from `main` (already done)
- [x] T002 Create empty `src/coordinare/services/ci_detection.py` with module docstring

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: CI detection service — used by both performer-side (US2) and coordinare-side (US3)

**⚠️ CRITICAL**: US2 and US3 both depend on the detection service from this phase.

- [x] T003 [P] Define `CIDetectionResult` dataclass in `src/coordinare/services/ci_detection.py` per `contracts/ci-detection.schema.json` (fields: lint_command, test_command, stack, detected_from)
- [x] T004 [P] Define `CIRunResult` dataclass in `agent/performer/src/performer/workspace.py` (fields: success, exit_code, stdout, stderr, command, duration_seconds)
- [x] T005 Implement `detect(workspace_path: Path) -> CIDetectionResult` in `src/coordinare/services/ci_detection.py` — Ruby detection: check for `Gemfile` + `.rubocop.yml` → lint=`bundle exec rubocop`, test=`bundle exec rspec`; `Gemfile` + `Rakefile` only → test=`bundle exec rake test`
- [x] T006 Extend `detect()` with Python detection: `pyproject.toml` with `ruff` in deps → lint=`ruff check .`, test=`pytest`; `pyproject.toml` with `flake8` → lint=`flake8`, test=`pytest`
- [x] T007 Extend `detect()` with Node detection: `package.json` with `"lint"` script → lint=`npm run lint`, test=`npm test`; `package.json` with `"test"` only → test=`npm test`
- [x] T008 Extend `detect()` with Makefile detection: `Makefile` with `lint` target → lint=`make lint`; `Makefile` with `ci` target → lint=`make ci`; `Makefile` with `test` target → test=`make test`
- [x] T009 Implement `run_command(cmd: str, cwd: Path, timeout: int = 120) -> CIRunResult` async helper in `agent/performer/src/performer/workspace.py` using `asyncio.create_subprocess_exec`
- [x] T010 [P] Write tests for Ruby detection in `tests/unit/services/test_ci_detection.py` — create tmp_path fixtures with Gemfile/.rubocop.yml/Rakefile combinations, assert correct lint/test commands
- [x] T011 [P] Write tests for Python detection in `tests/unit/services/test_ci_detection.py` — pyproject.toml with ruff/flake8 deps, assert correct commands
- [x] T012 [P] Write tests for Node detection in `tests/unit/services/test_ci_detection.py` — package.json with/without lint script, assert correct commands
- [x] T013 [P] Write tests for Makefile detection in `tests/unit/services/test_ci_detection.py` — Makefile with lint/ci/test targets
- [x] T014 [P] Write test for unknown stack (no matching files) → returns stack="unknown", both commands None
- [x] T015 [P] Write tests for `run_command` in `tests/unit/test_workspace.py` — mock subprocess, test success/failure/timeout paths

**Checkpoint**: CI detection service and run_command helper are complete and tested. US2 and US3 can now proceed in parallel.

---

## Phase 3: User Story 2 — Performer-Side CI Enforcement (Priority: P1) 🎯 MVP

**Goal**: Every code-touching performer runs CI locally before committing. If CI fails, the performer fixes the issue or reports it as blocked.

**Independent Test**: Run a performer in dry-run mode with a workspace containing a lint violation. Verify it catches the violation before committing (or reports it in output).

### Tests for User Story 2

- [x] T016 [P] [US2] Write test in `agent/performer/tests/unit/test_main.py`: implementer role calls `_run_ci_check` before committing, verify lint command runs in workspace
- [x] T017 [P] [US2] Write test: `_run_ci_check` returns failure → performer returns `changes_requested` with lint error in reason

### Implementation for User Story 2

- [x] T018 [US2] Add `_run_ci_check(stand, score)` helper in `agent/performer/src/performer/main.py` — calls `ci_detection.detect(workspace_path)`, runs lint_command via `run_command`, returns (success, error_output)
- [x] T019 [US2] Wire `_run_ci_check` into the implementer commit path in `agent/performer/src/performer/main.py` — call before `push_branch`, bail with error output if lint fails
- [x] T020 [P] [US2] Wire `_run_ci_check` into the QA commit path — via persona directive (QA commits via backend tool-use, not Python code)
- [x] T021 [P] [US2] Wire `_run_ci_check` into the security commit path — via persona directive
- [x] T022 [P] [US2] Wire `_run_ci_check` into the tech_writer commit path — via persona directive
- [x] T023 [US2] Update implementer persona in `src/coordinare/services/persona_service.py` — add CI-ownership directive per research.md R-5
- [x] T024 [P] [US2] Update QA persona with CI-ownership directive
- [x] T025 [P] [US2] Update security persona with CI-ownership directive
- [x] T026 [P] [US2] Update tech_writer persona with CI-ownership directive
- [x] T027 [P] [US2] Update reviewer persona with explicit lint instruction per spec section 4
- [x] T028 [P] [US2] Update closer persona with explicit lint instruction per spec section 4

**Checkpoint**: Performers now run CI locally before committing. A QA-authored test with a lint violation (PR #94 scenario) would be caught at QA time.

---

## Phase 4: User Story 3 — Coordinare-Side CI Gate (Priority: P2)

**Goal**: Defence-in-depth: coordinare runs lint on the PR branch before transitioning to `monitoring_pr`. If lint fails, the card routes back to the implementer with the failure in `relay_feedback`.

**Independent Test**: Mock a workspace where lint fails. Verify `_advance_stage` does NOT transition to `monitoring_pr` and instead routes to implementer.

### Tests for User Story 3

- [x] T029 [P] [US3] Write test in `tests/unit/graph/nodes/test_monitor_performer.py`: `_advance_stage` on final stage with lint passing → transitions to `monitoring_pr`
- [x] T030 [P] [US3] Write test: `_advance_stage` on final stage with lint failing → sets `performer_stage=implementing`, `relay_feedback=[lint error]`, `phase=dispatching`
- [x] T031 [P] [US3] Write test: `_advance_stage` with no CI detected (unknown stack) → transitions to `monitoring_pr` normally (skip gate, log warning)

### Implementation for User Story 3

- [x] T032 [US3] In `src/coordinare/graph/nodes/monitor_performer.py`, extend `_advance_stage` — when transitioning to `monitoring_pr` (last stage), get workspace path from `state.get("workspace_path")`; if None or path doesn't exist (already torn down), skip gate and log warning; otherwise call `ci_detection.detect(workspace_path)` and run `lint_command` via `subprocess.run` (sync function)
- [x] T033 [US3] If lint fails: set `performer_stage="implementing"`, populate `relay_feedback` with lint output, set `phase="dispatching"` — reuses the existing relay mechanism from spec 042
- [x] T034 [US3] If lint passes or no CI detected: proceed to `monitoring_pr` as before
- [x] T035 [US3] Add structlog logging for CI gate: `ci_gate.lint_passed`, `ci_gate.lint_failed`, `ci_gate.no_ci_detected`

**Checkpoint**: Coordinare-side gate catches lint failures that the performer missed. The PR #94 scenario would be blocked at the gate.

---

## Phase 5: Polish & Cross-Cutting Concerns

**Purpose**: Documentation, spec updates, final validation

- [x] T036 [P] Update `specs/043-performer-ci-ownership/spec.md` with implementation notes from completed tasks
- [x] T037 [P] Run `.venv/bin/ruff check src/ tests/ agent/` — verify lint clean
- [x] T038 Run `.venv/bin/pytest tests/ -q` — verify all tests pass (including new CI detection + gate tests)
- [x] T039 Run `.venv/bin/pytest agent/performer/tests/ -q` — verify performer tests pass
- [x] T040 Run quickstart.md validation — execute the test commands listed in quickstart.md

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No dependencies — start immediately
- **Foundational (Phase 2)**: Depends on Setup — BLOCKS all user stories
- **US2 Performer-Side (Phase 3)**: Depends on Foundational (T003-T015)
- **US3 Coordinare Gate (Phase 4)**: Depends on Foundational (T003-T015). Can run in parallel with US2.
- **Polish (Phase 5)**: Depends on US2 + US3 completion

### User Story Dependencies

- **US2 (P1 — MVP)**: Needs CI detection service (Phase 2). No dependency on US3.
- **US3 (P2)**: Needs CI detection service (Phase 2). No dependency on US2. Can run in parallel.

### Within Each User Story

- Tests written first (TDD per constitution)
- Models/helpers before integration
- Core implementation before persona updates

### Parallel Opportunities

Within Phase 2:
- T010, T011, T012, T013, T014, T015 can all run in parallel (different test files/fixtures)
- T003, T004 can run in parallel (different files)

Within Phase 3 (US2):
- T016, T017 in parallel (different test scenarios)
- T020, T021, T022 in parallel (different role paths)
- T024, T025, T026, T027, T028 in parallel (different persona strings)

Within Phase 4 (US3):
- T029, T030, T031 in parallel (different test scenarios)

---

## Parallel Example: Foundational Phase

```bash
# Launch all detection tests in parallel:
Task T010: "Ruby detection tests"
Task T011: "Python detection tests"
Task T012: "Node detection tests"
Task T013: "Makefile detection tests"
Task T014: "Unknown stack test"
Task T015: "run_command tests"
```

## Parallel Example: US2 Persona Updates

```bash
# Launch all persona updates in parallel:
Task T024: "QA persona"
Task T025: "Security persona"
Task T026: "Tech_writer persona"
Task T027: "Reviewer persona"
Task T028: "Closer persona"
```

---

## Implementation Strategy

### MVP First (US2 Only)

1. Complete Phase 1: Setup (T001-T002)
2. Complete Phase 2: Foundational (T003-T015) — CI detection service
3. Complete Phase 3: US2 — Performer-side enforcement (T016-T028)
4. **STOP and VALIDATE**: Run a performer against a repo with a lint violation. Verify it catches the issue before committing.
5. Deploy — performers now self-validate CI before pushing

### Full Delivery (US2 + US3)

1. MVP above
2. Complete Phase 4: US3 — Coordinare gate (T029-T035)
3. Complete Phase 5: Polish (T036-T040)
4. Full validation — both layers active

---

## Notes

- Total tasks: 40
- US2 (performer-side): 13 tasks — MVP, highest impact
- US3 (coordinare gate): 7 tasks — defence-in-depth
- Foundational: 13 tasks — shared by both stories
- Setup + Polish: 7 tasks
- Parallel opportunities: 24 of 40 tasks are parallelizable
- MVP scope: Phase 1 + Phase 2 + Phase 3 (US2 only) = 28 tasks
