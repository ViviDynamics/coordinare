# Tasks: Docker/Compose Runtime Entry and Visibility

**Input**: Design documents from `/specs/002-docker-cli-output/`
**Prerequisites**: plan.md (required), spec.md (required for user stories), research.md, data-model.md, contracts/

**Tests**: Required by constitution. Unit, integration, and contract coverage tasks are included.

**Organization**: Tasks are grouped by user story to enable independent implementation and testing of each story.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to (e.g., [US1], [US2], [US3])
- Include exact file paths in descriptions

## Path Conventions

- Single backend project: `src/coordinare/`, `scripts/`, root runtime artifacts, and `tests/`

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Add baseline runtime artifacts and execution scaffolding

- [X] T001 Create shell runtime entry script scaffold in `scripts/run-coordinare.sh`
- [X] T002 Create Docker image scaffold in `Dockerfile`
- [X] T003 Create Docker Compose scaffold for coordinare service in `docker-compose.yml`
- [X] T004 Create runtime configuration templates in `config.example.yaml` and `.env.example`
- [X] T005 [P] Update local configuration ignore rules in `.gitignore` and `.dockerignore`

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Shared runtime and observability behavior required by all user stories

**⚠️ CRITICAL**: No user story work can begin until this phase is complete

- [X] T006 Implement runtime-output mode/config parsing (human/structured, log level) in `src/coordinare/config.py`
- [X] T007 [P] Implement runtime event categorization helpers (startup, heartbeat, activity, state_change, failure, shutdown) in `src/coordinare/lib/runtime_events.py`
- [X] T008 [P] Implement known-sensitive-field redaction helper in `src/coordinare/lib/redaction.py`
- [X] T009 Integrate redaction + runtime categories into logger configuration in `src/coordinare/__init__.py`
- [X] T010 Implement non-zero runtime failure exit wiring in daemon lifecycle in `src/coordinare/daemon.py`
- [X] T011 Define and implement startup-vs-runtime failure classification in `src/coordinare/daemon.py` and `src/coordinare/__main__.py`
- [X] T012 Add runtime mode metadata to CLI startup path in `src/coordinare/__main__.py`
- [X] T013 Add shared smoke test helpers for shell/compose invocation assertions in `tests/integration/runtime/test_runtime_helpers.py`

**Checkpoint**: Foundation ready — user story implementation can begin

---

## Phase 3: User Story 1 - Run from Command Line with Clear Output (Priority: P1) 🎯 MVP

**Goal**: Users can run the app via shell script and get clear startup/activity/error console output.

**Independent Test**: Execute `scripts/run-coordinare.sh --config config.yaml`; verify startup status, heartbeat/activity visibility, and non-zero exit behavior on runtime failure.

### Implementation for User Story 1

- [X] T014 [US1] Implement shell script execution flow and argument passthrough in `scripts/run-coordinare.sh`
- [X] T015 [US1] Add shell-mode startup messaging and failing-phase context in `src/coordinare/__main__.py`
- [X] T016 [US1] Add heartbeat emission scheduling in daemon loop in `src/coordinare/daemon.py`
- [X] T017 [US1] Add CLI output mode toggle support (`--structured-output`) in `src/coordinare/__main__.py`
- [X] T018 [US1] Add shell runtime smoke test for startup/activity/failure visibility in `tests/integration/runtime/test_shell_runtime.py`
- [X] T019 [US1] Document shell runtime usage and expected output in `specs/002-docker-cli-output/quickstart.md`

**Checkpoint**: User Story 1 functional and independently testable

---

## Phase 4: User Story 2 - Run via Docker Compose with Equivalent Visibility (Priority: P2)

**Goal**: Users can run via Docker Compose with behavior/output parity to shell mode.

**Independent Test**: Execute `docker compose up --build coordinare`; verify compose logs include startup/activity/failure semantics equivalent to shell mode.

### Implementation for User Story 2

- [X] T020 [US2] Implement production-ready coordinare image build stages in `Dockerfile`
- [X] T021 [US2] Implement coordinare service runtime wiring (env, volumes, command) in `docker-compose.yml`
- [X] T022 [P] [US2] Add compose-specific runtime config mapping notes in `config.example.yaml`
- [X] T023 [US2] Ensure compose entrypoint uses same runtime semantics as shell mode in `docker-compose.yml`
- [X] T024 [US2] Add compose runtime smoke test for output parity and non-zero exit behavior in `tests/integration/runtime/test_compose_runtime.py`
- [X] T025 [US2] Document compose run/log/stop workflows in `specs/002-docker-cli-output/quickstart.md`

**Checkpoint**: User Stories 1 and 2 independently runnable and verifiable

---

## Phase 5: User Story 3 - Understand Runtime State from Console Output (Priority: P3)

**Goal**: Users can infer runtime state transitions and failure context from logs in either run mode.

**Independent Test**: Observe recent output from both run modes and confirm state (`idle/active/blocked/recovery`) plus latest significant action can be identified.

### Implementation for User Story 3

- [X] T026 [US3] Add explicit state-transition event emission in runtime flow in `src/coordinare/daemon.py`
- [X] T027 [P] [US3] Add structured payload mapping for state-change and failure events in `src/coordinare/lib/runtime_events.py`
- [X] T028 [US3] Add failure-context enrichment for actionable diagnostics in `src/coordinare/daemon.py`
- [X] T029 [US3] Add output-contract verification for state inference in `tests/contract/test_runtime_output_contract.py`
- [X] T030 [US3] Add cross-mode state-visibility integration test in `tests/integration/runtime/test_runtime_state_visibility.py`
- [X] T031 [US3] Update runtime output/state examples in `specs/002-docker-cli-output/contracts/shell-runtime-contract.md` and `specs/002-docker-cli-output/contracts/compose-runtime-contract.md`

**Checkpoint**: All user stories independently functional with clear runtime state observability

---

## Phase 6: Polish & Cross-Cutting Concerns

**Purpose**: Finalize quality and delivery readiness across all stories

- [X] T032 [P] Validate quickstart end-to-end for shell mode in `specs/002-docker-cli-output/quickstart.md`
- [X] T033 [P] Validate quickstart end-to-end for compose mode in `specs/002-docker-cli-output/quickstart.md`
- [X] T034 Run lint and type checks for modified code paths via `pyproject.toml` toolchain
- [X] T035 Run full affected test suite for runtime and contract coverage in `tests/integration/runtime/` and `tests/contract/`
- [X] T036 [P] Final docs consistency pass across plan/research/contracts/quickstart in `specs/002-docker-cli-output/`
- [X] T037 [P] Add unit tests for redaction helper behavior in `tests/unit/lib/test_redaction.py`
- [X] T038 [P] Add unit tests for runtime event categorization in `tests/unit/lib/test_runtime_events.py`
- [X] T039 [P] Add unit tests for CLI runtime mode and flag parsing in `tests/unit/test_main_runtime_args.py`
- [X] T040 [P] Add unit tests for daemon runtime failure exit behavior in `tests/unit/test_daemon_exit_behavior.py`
- [X] T041 Run coverage gate and verify configured threshold from `pyproject.toml` using test report output
- [X] T042 Add startup and heartbeat timing verification test against SC-001 and SC-005 in `tests/integration/runtime/test_runtime_timing.py`
- [X] T043 [P] Add classification behavior tests for startup and runtime failures in `tests/integration/runtime/test_failure_classification.py`

---

## Dependencies & Execution Order

### Phase Dependencies

- **Phase 1 (Setup)**: No dependencies
- **Phase 2 (Foundational)**: Depends on Setup completion; blocks all user stories
- **Phase 3 (US1)**: Depends on Foundational completion
- **Phase 4 (US2)**: Depends on Foundational completion; should align behavior with US1 output semantics
- **Phase 5 (US3)**: Depends on Foundational completion; validates/extends observability semantics used by US1/US2
- **Phase 6 (Polish)**: Depends on completion of all user story phases

### User Story Dependencies

- **US1 (P1)**: First MVP slice; no dependency on other stories once foundational tasks are complete
- **US2 (P2)**: Reuses US1 runtime semantics, but remains independently testable through compose flow
- **US3 (P3)**: Cross-mode observability enhancement; independently testable via state-inference checks

### Within Each User Story

- Runtime entrypoint/config before tests that exercise it
- Output semantics before contract assertions
- Story docs updated after behavior is implemented

### Parallel Opportunities

- Setup: T005 parallel with T001–T004
- Foundational: T007 and T008 parallel; then integration tasks T009–T012
- US2: T022 parallel with T020/T021
- US3: T027 parallel with T026
- Polish: T032, T033, T036 can run in parallel

---

## Parallel Example: User Story 2

```bash
# Parallelizable implementation tasks:
Task: "T020 [US2] Implement production-ready coordinare image build stages in Dockerfile"
Task: "T021 [US2] Implement coordinare service runtime wiring in docker-compose.yml"
Task: "T022 [P] [US2] Add compose-specific runtime config mapping notes in config.example.yaml"
```

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Complete Phase 1 (Setup)
2. Complete Phase 2 (Foundational)
3. Complete Phase 3 (US1)
4. Validate shell-script runtime flow and output behavior
5. Demo/deploy MVP if accepted

### Incremental Delivery

1. Add US1 for non-Docker runtime operability
2. Add US2 for compose runtime operability
3. Add US3 for robust state observability
4. Complete polish and validation phase

### Parallel Team Strategy

1. Team aligns on Setup + Foundational first
2. Then parallelize by story where possible:
   - Developer A: US1 shell runtime
   - Developer B: US2 compose runtime
   - Developer C: US3 output/state observability

---

## Notes

- All tasks follow required checklist format: `- [ ] T### [P?] [US?] Description with file path`
- Story phases preserve independent testability for each user story
- MVP recommendation is US1 only after foundational completion
