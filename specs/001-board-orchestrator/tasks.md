# Tasks: Board Orchestrator Daemon

**Input**: Design documents from `/specs/001-board-orchestrator/`
**Prerequisites**: plan.md (required), spec.md (required), research.md, data-model.md, contracts/

**Tests**: Included per constitution and feature spec testing requirements (unit, integration, contract).

**Organization**: Tasks are grouped by user story to enable independent implementation and testing.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Parallelizable (different files, no dependency on incomplete tasks)
- **[Story]**: User story label for story-phase tasks only (`[US1]`, `[US2]`, `[US3]`, `[US4]`)
- Every task includes at least one concrete file path

## Path Conventions

- Single backend project: `src/coordinare/` and `tests/`

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Project baseline, tooling, and skeletons needed by all stories

- [X] T001 Create package/module skeleton per plan in `src/coordinare/graph/nodes/__init__.py`, `src/coordinare/services/__init__.py`, and `src/coordinare/models/__init__.py`
- [X] T002 Define project dependencies and scripts in `pyproject.toml`
- [X] T003 [P] Configure ruff lint/format settings in `pyproject.toml`
- [X] T004 [P] Configure strict mypy settings in `pyproject.toml`
- [X] T005 [P] Configure pytest/asyncio/coverage settings in `pyproject.toml`
- [X] T006 Create runtime config template in `config.example.yaml`
- [X] T007 Create developer/runtime orchestration file in `docker-compose.yml`

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Core state, config, graph, and service primitives that block all stories

**⚠️ CRITICAL**: No user story implementation begins until this phase is complete

- [X] T008 Implement layered project configuration model in `src/coordinare/config.py`
- [X] T009 [P] Implement card domain models (`CardStatus`, `CardTransition`, `Card`) in `src/coordinare/models/card.py`
- [X] T010 [P] Implement review domain model and enums in `src/coordinare/models/review.py`
- [X] T011 [P] Implement notification domain model in `src/coordinare/models/notification.py`
- [X] T012 Implement workflow state schema and initial state helper in `src/coordinare/graph/state.py`
- [X] T013 Implement routing helpers for graph conditionals in `src/coordinare/graph/routing.py`
- [X] T014 Implement graph builder with node registration and conditional edges in `src/coordinare/graph/builder.py`
- [X] T015 Implement structured logging bootstrap in `src/coordinare/__init__.py`
- [X] T016 Implement daemon lifecycle loop and error/backoff handling in `src/coordinare/daemon.py`
- [X] T017 Implement CLI entrypoint startup wiring in `src/coordinare/__main__.py`
- [X] T018 Implement GitHub Projects client foundation (auth, project/field discovery cache) in `src/coordinare/services/github.py`
- [X] T019 [P] Implement shared pytest fixtures for mocked external systems in `tests/conftest.py`
- [X] T020 [P] Add configuration model unit tests in `tests/unit/test_config.py`
- [X] T021 [P] Add card model unit tests in `tests/unit/models/test_card.py`
- [X] T022 [P] Add review model unit tests in `tests/unit/models/test_review.py`
- [X] T023 [P] Add state schema unit tests in `tests/unit/test_state.py`

**Checkpoint**: Foundation complete; user story phases can proceed

---

## Phase 3: User Story 1 - Board Monitoring & Card Dispatch (Priority: P1) 🎯 MVP

**Goal**: Pick next eligible card from ToDo / Backlog, assess sufficiency, dispatch to agent over SSH, enforce one-card-at-a-time.

**Independent Test**: With empty In Progress/In Review and cards in ToDo / Backlog, coordinare moves exactly one card to In Progress and dispatches context to agent.

### Tests for User Story 1

- [X] T024 [P] [US1] Add GitHub board/issue/card-move contract tests mapped to `specs/001-board-orchestrator/contracts/github-graphql.md` in `tests/contract/test_github_queries.py`
- [X] T025 [P] [US1] Add SSH dispatch/health contract tests mapped to `specs/001-board-orchestrator/contracts/agent-ssh-interface.md` in `tests/contract/test_agent_interface.py`
- [X] T026 [P] [US1] Add `check_board` node unit tests in `tests/unit/graph/nodes/test_check_board.py`
- [X] T027 [P] [US1] Add `assess_card` node unit tests in `tests/unit/graph/nodes/test_assess_card.py`
- [X] T028 [P] [US1] Add `dispatch_card` node unit tests in `tests/unit/graph/nodes/test_dispatch_card.py`
- [X] T029 [P] [US1] Add GitHub service polling/move tests in `tests/unit/services/test_github.py`
- [X] T030 [P] [US1] Add agent SSH dispatch/health service tests in `tests/unit/services/test_agent_ssh.py`
- [X] T031 [P] [US1] Add Claude sufficiency service tests in `tests/unit/services/test_claude.py`
- [X] T032 [US1] Add dispatch-loop integration test in `tests/integration/test_graph_execution.py`

### Implementation for User Story 1

- [X] T033 [P] [US1] Implement poll board items operation in `src/coordinare/services/github.py`
- [X] T034 [P] [US1] Implement issue detail retrieval operation in `src/coordinare/services/github.py`
- [X] T035 [P] [US1] Implement card status transition mutation in `src/coordinare/services/github.py`
- [X] T036 [US1] Implement Claude card sufficiency analysis service in `src/coordinare/services/claude.py`
- [X] T037 [US1] Implement SSH agent dispatch/health service in `src/coordinare/services/agent_ssh.py`
- [X] T038 [US1] Implement board polling and dispatch gating node in `src/coordinare/graph/nodes/check_board.py`
- [X] T039 [US1] Implement card assessment node in `src/coordinare/graph/nodes/assess_card.py`
- [X] T040 [US1] Implement card dispatch node in `src/coordinare/graph/nodes/dispatch_card.py`
- [X] T041 [US1] Wire US1 node sequence and routes in `src/coordinare/graph/builder.py`

**Checkpoint**: US1 independently functional and testable

---

## Phase 4: User Story 2 - PR Review & Merge Cycle (Priority: P2)

**Goal**: Monitor PR reviews, ignore bots/CoPilot, relay human feedback, squash-merge on approval, move card to Done.

**Independent Test**: For a card in In Review, human feedback is relayed, bot feedback is ignored, approval leads to squash merge and Done transition.

### Tests for User Story 2

- [X] T042 [P] [US2] Add PR review/merge GraphQL contract tests mapped to `specs/001-board-orchestrator/contracts/github-graphql.md` in `tests/contract/test_github_queries.py`
- [X] T043 [P] [US2] Add `monitor_pr` node unit tests in `tests/unit/graph/nodes/test_monitor_pr.py`
- [X] T044 [P] [US2] Add `relay_feedback` node unit tests in `tests/unit/graph/nodes/test_relay_feedback.py`
- [X] T045 [P] [US2] Add `merge_pr` node unit tests in `tests/unit/graph/nodes/test_merge_pr.py`
- [X] T046 [P] [US2] Add GitHub review/merge service tests in `tests/unit/services/test_github.py`
- [X] T047 [US2] Add review-to-merge integration test in `tests/integration/test_graph_execution.py`

### Implementation for User Story 2

- [X] T048 [P] [US2] Implement PR review retrieval in `src/coordinare/services/github.py`
- [X] T049 [P] [US2] Implement mergeability check in `src/coordinare/services/github.py`
- [X] T050 [P] [US2] Implement squash merge mutation in `src/coordinare/services/github.py`
- [X] T051 [US2] Implement agent feedback relay command in `src/coordinare/services/agent_ssh.py`
- [X] T052 [US2] Implement PR monitoring node with human/bot filtering in `src/coordinare/graph/nodes/monitor_pr.py`
- [X] T053 [US2] Implement review feedback relay node in `src/coordinare/graph/nodes/relay_feedback.py`
- [X] T054 [US2] Implement merge-and-close node in `src/coordinare/graph/nodes/merge_pr.py`
- [X] T055 [US2] Wire review lifecycle routes in `src/coordinare/graph/builder.py`

**Checkpoint**: US2 independently functional and testable

---

## Phase 5: User Story 3 - Notification System (Priority: P3)

**Goal**: Send email + Slack notifications on all transitions with required contextual content.

**Independent Test**: Any column transition produces both email and Slack notifications containing status, task context, and applicable questions/PR summaries.

### Tests for User Story 3

- [X] T056 [P] [US3] Add notification content model tests in `tests/unit/models/test_notification.py`
- [X] T057 [P] [US3] Add email service tests in `tests/unit/services/test_email.py`
- [X] T058 [P] [US3] Add Slack service tests in `tests/unit/services/test_slack.py`
- [X] T059 [P] [US3] Add `notify` node unit tests in `tests/unit/graph/nodes/test_notify.py`
- [X] T060 [US3] Add notification pipeline integration test in `tests/integration/test_notification_flow.py`

### Implementation for User Story 3

- [X] T061 [US3] Implement SMTP notification service in `src/coordinare/services/email.py`
- [X] T062 [US3] Implement Slack notification service in `src/coordinare/services/slack.py`
- [X] T063 [US3] Implement transition notification node in `src/coordinare/graph/nodes/notify.py`
- [X] T064 [US3] Wire notification node to transition edges in `src/coordinare/graph/builder.py`

**Checkpoint**: US3 independently functional and testable

---

## Phase 6: User Story 4 - Blocked Card & Clarity Requests (Priority: P4)

**Goal**: Move blocked work to Blocked with explicit questions, notify team, detect answers, and resume flow.

**Independent Test**: Insufficient/blocked work transitions to Blocked with comment + notifications, then resumes when team responses are detected.

### Tests for User Story 4

- [X] T065 [P] [US4] Add `handle_blocked` node unit tests in `tests/unit/graph/nodes/test_handle_blocked.py`
- [X] T066 [P] [US4] Add `monitor_agent` node unit tests in `tests/unit/graph/nodes/test_monitor_agent.py`
- [X] T067 [P] [US4] Add GitHub comment mutation tests in `tests/unit/services/test_github.py`
- [X] T068 [P] [US4] Add agent status command tests in `tests/unit/services/test_agent_ssh.py`
- [X] T069 [US4] Add blocked-to-resume integration test in `tests/integration/test_graph_execution.py`

### Implementation for User Story 4

- [X] T070 [P] [US4] Implement GitHub add-comment mutation in `src/coordinare/services/github.py`
- [X] T071 [P] [US4] Implement agent status polling command in `src/coordinare/services/agent_ssh.py`
- [X] T072 [US4] Implement agent status monitor node in `src/coordinare/graph/nodes/monitor_agent.py`
- [X] T073 [US4] Implement blocked handling node (move/comment/state update) in `src/coordinare/graph/nodes/handle_blocked.py`
- [X] T074 [US4] Implement unblock answer-detection path in `src/coordinare/graph/nodes/check_board.py`
- [X] T075 [US4] Implement blocked reminder scheduling logic in `src/coordinare/graph/nodes/handle_blocked.py`
- [X] T076 [US4] Wire blocked/recovery routes in `src/coordinare/graph/builder.py`

**Checkpoint**: US4 independently functional and testable

---

## Phase 7: Polish & Cross-Cutting Concerns

**Purpose**: Observability, deployment readiness, and global validation across stories

- [X] T077 [P] Implement health/ready endpoints in `src/coordinare/health.py`
- [X] T078 [P] Implement Prometheus metrics export in `src/coordinare/metrics.py`
- [X] T079 [P] Add health/ready/metrics contract tests mapped to `specs/001-board-orchestrator/contracts/health-api.md` in `tests/contract/test_health_api.py`
- [X] T080 Add daemon lifecycle integration test (startup/shutdown/recovery) in `tests/integration/test_daemon_lifecycle.py`
- [X] T081 Add quickstart end-to-end validation updates in `specs/001-board-orchestrator/quickstart.md`
- [X] T082 [P] Add notification latency validation for <=2 minutes (SC-002) in `tests/integration/test_notification_flow.py`
- [X] T083 [P] Add config-only onboarding validation (SC-007) in `tests/integration/test_daemon_lifecycle.py`
- [X] T084 Add 24-hour simulated reliability soak validation for zero-loss/zero-duplication/zero-invalid-state (SC-008) in `tests/integration/test_graph_execution.py`
- [X] T085 Add CI performance regression gate for SC-001/SC-004/SC-006 in `.github/workflows/pr-ci.yml`
- [X] T086 Run final quality gates (`ruff`, `mypy`, `pytest --cov`) via `pyproject.toml` toolchain

---

## Dependencies & Execution Order

### Phase Dependencies

- **Phase 1 (Setup)**: No dependencies
- **Phase 2 (Foundational)**: Depends on Phase 1; blocks all user stories
- **Phase 3 (US1)**: Depends on Phase 2
- **Phase 4 (US2)**: Depends on US1 outputs + Phase 2
- **Phase 5 (US3)**: Depends on Phase 2; can proceed after core transition model exists
- **Phase 6 (US4)**: Depends on US1 flow + Phase 2
- **Phase 7 (Polish)**: Depends on completion of all user story phases

### User Story Dependencies

- **US1 (P1)**: First MVP slice; no dependency on other stories after foundation
- **US2 (P2)**: Depends on US1 dispatch/review lifecycle
- **US3 (P3)**: Mostly independent after foundation; integrates with transition events
- **US4 (P4)**: Depends on US1 dispatch/check_board behavior

### Within-Story Ordering

- Tests first (must fail) -> Models -> Services -> Nodes -> Graph wiring -> Integration validation

### Parallel Opportunities

- Setup: `T003-T005` in parallel
- Foundation: `T009-T011`, `T019-T023` in parallel where files are distinct
- US1 tests: `T024-T031` in parallel
- US1 GitHub methods: `T033-T035` parallel
- US2 tests: `T042-T046` parallel
- US2 GitHub methods: `T048-T050` parallel
- US3 tests: `T056-T059` parallel
- US4 tests: `T065-T068` parallel
- Polish: `T077-T079`, `T082-T083` parallel

---

## Parallel Example: User Story 1

```bash
Task: "T024 [US1] Contract tests for GitHub poll/move in tests/contract/test_github_queries.py"
Task: "T025 [US1] Contract tests for SSH interface in tests/contract/test_agent_interface.py"
Task: "T026 [US1] Unit tests for check_board node in tests/unit/graph/nodes/test_check_board.py"
Task: "T027 [US1] Unit tests for assess_card node in tests/unit/graph/nodes/test_assess_card.py"
Task: "T028 [US1] Unit tests for dispatch_card node in tests/unit/graph/nodes/test_dispatch_card.py"
```

## Parallel Example: User Story 2

```bash
Task: "T042 [US2] Contract tests for PR review/merge in tests/contract/test_github_queries.py"
Task: "T043 [US2] Unit tests for monitor_pr node in tests/unit/graph/nodes/test_monitor_pr.py"
Task: "T044 [US2] Unit tests for relay_feedback node in tests/unit/graph/nodes/test_relay_feedback.py"
Task: "T045 [US2] Unit tests for merge_pr node in tests/unit/graph/nodes/test_merge_pr.py"
```

## Parallel Example: User Story 3

```bash
Task: "T056 [US3] Notification model tests in tests/unit/models/test_notification.py"
Task: "T057 [US3] Email service tests in tests/unit/services/test_email.py"
Task: "T058 [US3] Slack service tests in tests/unit/services/test_slack.py"
Task: "T059 [US3] Notify node tests in tests/unit/graph/nodes/test_notify.py"
```

## Parallel Example: User Story 4

```bash
Task: "T065 [US4] handle_blocked node tests in tests/unit/graph/nodes/test_handle_blocked.py"
Task: "T066 [US4] monitor_agent node tests in tests/unit/graph/nodes/test_monitor_agent.py"
Task: "T067 [US4] GitHub comment tests in tests/unit/services/test_github.py"
Task: "T068 [US4] Agent status tests in tests/unit/services/test_agent_ssh.py"
```

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Complete Phase 1 (Setup)
2. Complete Phase 2 (Foundational)
3. Complete Phase 3 (US1)
4. Validate US1 independently before expanding scope

### Incremental Delivery

1. Deliver US1 (dispatch loop)
2. Add US2 (review/merge completion)
3. Add US3 (visibility/notifications)
4. Add US4 (blocked/recovery path)
5. Complete Phase 7 polish and gates

### Team Parallelization

1. Team completes Setup + Foundational together
2. Then split by story track where dependencies permit:
   - Track A: US1 -> US2
   - Track B: US3
   - Track C: US4

---

## Notes

- All tasks conform to `- [ ] T### [P?] [US?] Description with file path`
- Story phases are independently testable increments
- External integrations are validated with contract tests before implementation wiring
