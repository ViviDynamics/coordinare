# Tasks: Board Orchestrator Daemon

**Input**: Design documents from `/specs/001-board-orchestrator/`
**Prerequisites**: plan.md (required), spec.md (required), research.md, data-model.md, contracts/

**Tests**: Included per Constitution Principle II (Testing Discipline — NON-NEGOTIABLE).

**Organization**: Tasks are grouped by user story to enable independent implementation and testing of each story.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to (e.g., US1, US2, US3, US4)
- Include exact file paths in descriptions

## Path Conventions

- **Single project**: `src/coordinare/` and `tests/` at repository root (per plan.md)

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Project initialization, dependency management, and tooling configuration

- [ ] T001 Create project directory structure with all packages and `__init__.py` files per plan.md source code layout
- [ ] T002 Initialize Python project with `pyproject.toml` including all dependencies: langgraph, anthropic, gql[aiohttp], asyncssh, fastapi, uvicorn, structlog, slack-sdk, aiosmtplib, prometheus-client, pydantic-settings, and dev deps: pytest, pytest-asyncio, pytest-cov, ruff, mypy
- [ ] T003 [P] Configure ruff linting and formatting rules in `pyproject.toml` (ruff section)
- [ ] T004 [P] Configure mypy strict mode in `pyproject.toml` (mypy section)
- [ ] T005 [P] Configure pytest with asyncio mode and coverage settings in `pyproject.toml` (pytest section)

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Core infrastructure that MUST be complete before ANY user story can be implemented

**CRITICAL**: No user story work can begin until this phase is complete

- [ ] T006 Implement ProjectConfiguration model with pydantic-settings, YAML file loading, env var override (COORDINARE_ prefix), and all validation rules from data-model.md in `src/coordinare/config.py`
- [ ] T007 [P] Implement CardStatus enum, CardTransition model, and Card model with all fields and validation rules from data-model.md in `src/coordinare/models/card.py`
- [ ] T008 [P] Implement CoordinareState TypedDict with all fields (current_card, board_snapshot, phase, pending_reviews, last_poll_at, error_count, github_field_cache) in `src/coordinare/graph/state.py`
- [ ] T009 Implement GitHub service core: gql client initialization, authentication, FindProject query (Q1), GetProjectFields query (Q2) with field/option ID caching in `src/coordinare/services/github.py`
- [ ] T010 Create LangGraph StateGraph skeleton with node registration placeholders and compile method with configurable checkpointer in `src/coordinare/graph/builder.py`
- [ ] T011 [P] Implement conditional edge routing functions (route_from_board_check, route_from_review, route_from_agent_status) in `src/coordinare/graph/routing.py`
- [ ] T012 [P] Configure structured JSON logging with structlog (processors, formatters, log level from config) in `src/coordinare/__init__.py`
- [ ] T013 Implement daemon lifecycle: async event loop, signal handlers (SIGTERM/SIGINT), poll-invoke-sleep cycle with configurable interval, exponential backoff on errors in `src/coordinare/daemon.py`
- [ ] T014 Implement CLI entry point that loads config, builds graph, starts daemon and health server in `src/coordinare/__main__.py`
- [ ] T015 Create shared test fixtures: mock GitHub gql client, mock SSH connection, mock SMTP server, mock Slack webhook, sample Card/Review factories in `tests/conftest.py`
- [ ] T016 [P] Unit tests for ProjectConfiguration: YAML loading, env var override precedence, validation rules (min reviewers, poll interval bounds, token non-empty, command placeholder) in `tests/unit/test_config.py`
- [ ] T017 [P] Unit tests for Card model: creation, validation (non-empty title, PR required for IN_REVIEW, questions required for BLOCKED), CardStatus enum values, CardTransition recording in `tests/unit/models/test_card.py`
- [ ] T018 [P] Unit tests for CoordinareState: default values, field types, state update patterns in `tests/unit/test_state.py`

**Checkpoint**: Foundation ready — user story implementation can now begin

---

## Phase 3: User Story 1 — Board Monitoring & Card Dispatch (Priority: P1) MVP

**Goal**: The coordinare monitors a board, picks the next card from ToDo when In Progress and In Review are empty, assesses card sufficiency, and dispatches it to the configured agent via SSH.

**Independent Test**: Set up a board with cards in ToDo, run the coordinare, verify the first card moves to In Progress and the agent receives the card's context. Verify one-at-a-time constraint is enforced.

### Tests for User Story 1

> **NOTE: Write these tests FIRST, ensure they FAIL before implementation**

- [ ] T019 [P] [US1] Contract tests for GitHub poll (Q3), get issue details (Q4), and move card (M1) query/mutation schemas in `tests/contract/test_github_queries.py`
- [ ] T020 [P] [US1] Contract tests for agent SSH dispatch (C1) and health check (C4) command/response schemas in `tests/contract/test_agent_interface.py`
- [ ] T021 [P] [US1] Unit tests for check_board node: returns correct routing for empty board, card in ToDo, card already in progress, card in review in `tests/unit/graph/nodes/test_check_board.py`
- [ ] T022 [P] [US1] Unit tests for assess_card node: sufficient card passes, insufficient card returns blocked signal, Claude tool use mocked in `tests/unit/graph/nodes/test_assess_card.py`
- [ ] T023 [P] [US1] Unit tests for dispatch_card node: moves card to In Progress, calls SSH dispatch, updates state with session_id in `tests/unit/graph/nodes/test_dispatch_card.py`
- [ ] T024 [P] [US1] Unit tests for GitHub service poll_board, get_issue_details, and move_card methods with mocked gql client in `tests/unit/services/test_github.py`
- [ ] T025 [P] [US1] Unit tests for agent SSH service dispatch_card and check_health methods with mocked asyncssh in `tests/unit/services/test_agent_ssh.py`
- [ ] T026 [P] [US1] Unit tests for Claude service assess_card_sufficiency method with mocked anthropic client in `tests/unit/services/test_claude.py`
- [ ] T027 [US1] Integration test for full card dispatch flow: poll board → assess card → dispatch to agent, using MemorySaver and mocked services in `tests/integration/test_graph_execution.py`

### Implementation for User Story 1

- [ ] T028 [P] [US1] Implement GitHub service poll_board method (Q3: PollBoard query, parse items by status column) in `src/coordinare/services/github.py`
- [ ] T029 [P] [US1] Implement GitHub service get_issue_details method (Q4: full issue body, comments, timeline, linked PRs) in `src/coordinare/services/github.py`
- [ ] T030 [P] [US1] Implement GitHub service move_card method (M1: updateProjectV2ItemFieldValue mutation) in `src/coordinare/services/github.py`
- [ ] T031 [US1] Implement Claude service with assess_card_sufficiency method: structured tool use to evaluate card clarity and generate questions in `src/coordinare/services/claude.py`
- [ ] T032 [US1] Implement agent SSH service: connect, dispatch_card (C1), check_health (C4) with timeout handling and retry logic in `src/coordinare/services/agent_ssh.py`
- [ ] T033 [US1] Implement check_board node: call github.poll_board, compare columns, determine routing (idle/dispatch/monitor) in `src/coordinare/graph/nodes/check_board.py`
- [ ] T034 [US1] Implement assess_card node: call github.get_issue_details, call claude.assess_card_sufficiency, route to dispatch or blocked in `src/coordinare/graph/nodes/assess_card.py`
- [ ] T035 [US1] Implement dispatch_card node: call github.move_card to In Progress, call agent_ssh.dispatch_card with card context JSON in `src/coordinare/graph/nodes/dispatch_card.py`
- [ ] T036 [US1] Wire US1 nodes (check_board → assess_card → dispatch_card) into graph builder with conditional routing for idle/dispatch paths in `src/coordinare/graph/builder.py`

**Checkpoint**: Board monitoring and card dispatch fully functional — cards move from ToDo to In Progress and agent receives context

---

## Phase 4: User Story 2 — PR Review & Merge Cycle (Priority: P2)

**Goal**: When the agent opens a PR, the coordinare monitors it for human reviews, ignores bot/CoPilot comments, relays human feedback to the agent, and squash-merges on approval.

**Independent Test**: Create a PR linked to a card in In Review, leave human review comments, verify feedback is relayed. Approve the PR and verify squash-merge occurs and card moves to Done.

### Tests for User Story 2

> **NOTE: Write these tests FIRST, ensure they FAIL before implementation**

- [ ] T037 [P] [US2] Unit tests for Review, ReviewerType, ReviewState models: human/bot classification logic, is_actionable derivation in `tests/unit/models/test_review.py`
- [ ] T038 [P] [US2] Unit tests for monitor_pr node: detects new reviews, filters human vs bot, routes to relay/merge/blocked in `tests/unit/graph/nodes/test_monitor_pr.py`
- [ ] T039 [P] [US2] Unit tests for relay_feedback node: calls agent SSH feedback command, updates state in `tests/unit/graph/nodes/test_relay_feedback.py`
- [ ] T040 [P] [US2] Unit tests for merge_pr node: checks mergeability, squash-merges, moves card to Done, triggers next pickup in `tests/unit/graph/nodes/test_merge_pr.py`
- [ ] T041 [P] [US2] Unit tests for GitHub service get_pr_reviews, check_mergeability, squash_merge methods in `tests/unit/services/test_github.py`
- [ ] T042 [US2] Integration test for review-merge flow: monitor PR → classify reviews → relay feedback → approve → merge, using MemorySaver in `tests/integration/test_graph_execution.py`

### Implementation for User Story 2

- [ ] T043 [P] [US2] Implement Review model with ReviewerType enum, ReviewState enum, and is_actionable classification logic per data-model.md in `src/coordinare/models/review.py`
- [ ] T044 [P] [US2] Implement GitHub service get_pr_reviews method (Q5: reviews with author typename/login, state) in `src/coordinare/services/github.py`
- [ ] T045 [P] [US2] Implement GitHub service check_mergeability method (Q6: mergeable, mergeStateStatus, reviewDecision) in `src/coordinare/services/github.py`
- [ ] T046 [P] [US2] Implement GitHub service squash_merge method (M3: mergePullRequest with SQUASH method) in `src/coordinare/services/github.py`
- [ ] T047 [US2] Implement agent SSH service relay_feedback method (C3: feedback command with review JSON) in `src/coordinare/services/agent_ssh.py`
- [ ] T048 [US2] Implement monitor_pr node: call github.get_pr_reviews, classify with human_reviewers list, detect approval/changes_requested/conflict in `src/coordinare/graph/nodes/monitor_pr.py`
- [ ] T049 [US2] Implement relay_feedback node: extract actionable reviews, call agent_ssh.relay_feedback with structured feedback JSON in `src/coordinare/graph/nodes/relay_feedback.py`
- [ ] T050 [US2] Implement merge_pr node: call github.check_mergeability, call github.squash_merge, call github.move_card to Done in `src/coordinare/graph/nodes/merge_pr.py`
- [ ] T051 [US2] Wire US2 nodes (monitor_pr → relay_feedback/merge_pr) into graph builder with review routing (approved→merge, changes→relay, conflict→blocked) in `src/coordinare/graph/builder.py`

**Checkpoint**: Full card lifecycle works — ToDo → In Progress → In Review → Done with human review gating

---

## Phase 5: User Story 3 — Notification System (Priority: P3)

**Goal**: Every card column transition triggers both an email and a Slack notification with contextual information (status, task description, open questions, commit/PR summary).

**Independent Test**: Trigger a card column transition and verify both an email and a Slack message arrive with the correct contextual information.

### Tests for User Story 3

> **NOTE: Write these tests FIRST, ensure they FAIL before implementation**

- [ ] T052 [P] [US3] Unit tests for Notification model: content rules (questions for BLOCKED, commit summary for IN_REVIEW/DONE) in `tests/unit/models/test_notification.py`
- [ ] T053 [P] [US3] Unit tests for email service: SMTP connection, message formatting, send with retry, failure handling in `tests/unit/services/test_email.py`
- [ ] T054 [P] [US3] Unit tests for Slack service: webhook POST, message formatting with blocks, failure handling in `tests/unit/services/test_slack.py`
- [ ] T055 [P] [US3] Unit tests for notify node: builds Notification from state, calls both email and Slack services, handles partial failures in `tests/unit/graph/nodes/test_notify.py`
- [ ] T056 [US3] Integration test for notification flow: card transition triggers both email and Slack with correct content in `tests/integration/test_notification_flow.py`

### Implementation for User Story 3

- [ ] T057 [P] [US3] Implement Notification model with content rendering rules per data-model.md (status-dependent fields) in `src/coordinare/models/notification.py`
- [ ] T058 [US3] Implement email notification service: aiosmtplib connection, HTML/text message formatting, send with retry, failure logging (non-blocking) in `src/coordinare/services/email.py`
- [ ] T059 [US3] Implement Slack notification service: webhook POST via httpx/aiohttp, rich message formatting with blocks, failure logging (non-blocking) in `src/coordinare/services/slack.py`
- [ ] T060 [US3] Implement notify node: build Notification from CoordinareState transition, dispatch to email and Slack services concurrently in `src/coordinare/graph/nodes/notify.py`
- [ ] T061 [US3] Wire notify node into all transition edges in graph builder so every column change triggers notification in `src/coordinare/graph/builder.py`

**Checkpoint**: All card transitions produce email and Slack notifications — team has full visibility without watching the board

---

## Phase 6: User Story 4 — Blocked Card & Clarity Requests (Priority: P4)

**Goal**: When the agent cannot proceed or card details are insufficient, the coordinare moves the card to Blocked, comments with specific questions, and sends notifications. When the team answers, the coordinare detects the response and resumes work.

**Independent Test**: Dispatch a card with vague requirements, verify it moves to Blocked, a comment appears with questions, and notifications are sent. Add a response and verify the card resumes.

### Tests for User Story 4

> **NOTE: Write these tests FIRST, ensure they FAIL before implementation**

- [ ] T062 [P] [US4] Unit tests for handle_blocked node: posts questions as comment, moves card to Blocked, triggers notification in `tests/unit/graph/nodes/test_handle_blocked.py`
- [ ] T063 [P] [US4] Unit tests for monitor_agent node: detects working/pr_opened/blocked/error status, routes correctly in `tests/unit/graph/nodes/test_monitor_agent.py`
- [ ] T064 [P] [US4] Unit tests for GitHub add_comment method (M2) in `tests/unit/services/test_github.py`
- [ ] T065 [P] [US4] Unit tests for agent SSH check_status method (C2) in `tests/unit/services/test_agent_ssh.py`
- [ ] T066 [US4] Integration test for blocked card flow: dispatch → agent reports blocked → card moves to Blocked → team answers → card resumes in `tests/integration/test_graph_execution.py`

### Implementation for User Story 4

- [ ] T067 [P] [US4] Implement GitHub service add_comment method (M2: addComment mutation with questions as formatted markdown) in `src/coordinare/services/github.py`
- [ ] T068 [P] [US4] Implement agent SSH service check_status method (C2: status command, parse working/pr_opened/blocked/error responses) in `src/coordinare/services/agent_ssh.py`
- [ ] T069 [US4] Implement monitor_agent node: call agent_ssh.check_status, route to monitor_pr (if PR opened), handle_blocked (if blocked/error), or stay monitoring in `src/coordinare/graph/nodes/monitor_agent.py`
- [ ] T070 [US4] Implement handle_blocked node: call github.move_card to Blocked, call github.add_comment with questions, update state with open_questions in `src/coordinare/graph/nodes/handle_blocked.py`
- [ ] T071 [US4] Add unblock detection to check_board node: detect new comments on blocked cards from team members, move card back to In Progress, relay answers to agent in `src/coordinare/graph/nodes/check_board.py`
- [ ] T072 [US4] Implement blocked card reminder: check blocked_reminder_hours config, re-send notification if card has been blocked longer than threshold in `src/coordinare/graph/nodes/handle_blocked.py`
- [ ] T073 [US4] Wire US4 nodes (monitor_agent, handle_blocked) into graph builder with blocked/unblock routing in `src/coordinare/graph/builder.py`

**Checkpoint**: Exception path fully handled — blocked cards get questions, team gets notified, and work resumes when answers arrive

---

## Phase 7: Polish & Cross-Cutting Concerns

**Purpose**: Observability, deployment, and final integration

- [ ] T074 [P] Implement FastAPI health-check endpoint (/health, /ready) with service connectivity checks per health-api.md contract in `src/coordinare/health.py`
- [ ] T075 [P] Implement Prometheus metrics export (/metrics) with all metrics from health-api.md contract (cards_processed, cycle_time, notifications, dispatch_latency, errors) in `src/coordinare/metrics.py`
- [ ] T076 [P] Contract tests for health API response schemas (/health, /metrics, /ready) in `tests/contract/test_health_api.py`
- [ ] T077 Integration test for daemon lifecycle: startup validation, graceful shutdown on SIGTERM, restart recovery from checkpoint in `tests/integration/test_daemon_lifecycle.py`
- [ ] T078 [P] Create Dockerfile with multi-stage build (builder + runtime) for container deployment
- [ ] T079 [P] Create docker-compose.yaml with coordinare service, PostgreSQL for checkpointing, and volume-mounted config
- [ ] T080 Create config.example.yaml with all configuration options documented with comments
- [ ] T081 Validate quickstart.md: run through setup, configuration, and local execution steps end-to-end

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No dependencies — can start immediately
- **Foundational (Phase 2)**: Depends on Setup completion — BLOCKS all user stories
- **User Stories (Phase 3-6)**: All depend on Foundational phase completion
  - US1 (P1) should complete first as it establishes the core dispatch loop
  - US2 (P2) depends on US1 (needs card in In Progress/In Review to monitor PRs)
  - US3 (P3) can start after Foundational — notification is independent of dispatch
  - US4 (P4) depends on US1 (needs card dispatch to test blocked flow)
- **Polish (Phase 7)**: Depends on all user stories being complete

### User Story Dependencies

- **User Story 1 (P1)**: Can start after Foundational (Phase 2) — No dependencies on other stories
- **User Story 2 (P2)**: Depends on US1 (uses dispatch_card output, shares GitHub service methods, extends graph routing)
- **User Story 3 (P3)**: Can start after Foundational (Phase 2) — Notification is additive; integrates into existing transitions
- **User Story 4 (P4)**: Depends on US1 (extends check_board and dispatch flow with blocked path)

### Within Each User Story

- Tests MUST be written and FAIL before implementation
- Models before services
- Services before graph nodes
- Graph nodes before graph builder wiring
- Story complete before moving to next priority

### Parallel Opportunities

- All Setup tasks T003-T005 marked [P] can run in parallel
- Foundational tasks T007-T008, T011-T012, T016-T018 marked [P] can run in parallel
- All test tasks within a story marked [P] can run in parallel
- US1 and US3 can proceed in parallel after Foundational (US3 is independent)
- Within US1: T019-T026 (tests) in parallel, then T028-T030 (GitHub methods) in parallel
- Within US2: T037-T041 (tests) in parallel, then T043-T046 (models + GitHub methods) in parallel
- Within US3: T052-T055 (tests) in parallel, then T057 (model) while T058-T059 (services) in parallel
- Within US4: T062-T065 (tests) in parallel, then T067-T068 (services) in parallel
- Polish: T074-T076, T078-T079 all marked [P] can run in parallel

---

## Parallel Example: User Story 1

```bash
# Launch all US1 tests together (write first, verify they fail):
Task: "Contract tests for GitHub poll/move queries in tests/contract/test_github_queries.py"
Task: "Contract tests for agent SSH dispatch in tests/contract/test_agent_interface.py"
Task: "Unit tests for check_board node in tests/unit/graph/nodes/test_check_board.py"
Task: "Unit tests for assess_card node in tests/unit/graph/nodes/test_assess_card.py"
Task: "Unit tests for dispatch_card node in tests/unit/graph/nodes/test_dispatch_card.py"
Task: "Unit tests for GitHub service methods in tests/unit/services/test_github.py"
Task: "Unit tests for agent SSH service in tests/unit/services/test_agent_ssh.py"
Task: "Unit tests for Claude service in tests/unit/services/test_claude.py"

# Then launch parallelizable GitHub service methods:
Task: "Implement poll_board in src/coordinare/services/github.py"
Task: "Implement get_issue_details in src/coordinare/services/github.py"
Task: "Implement move_card in src/coordinare/services/github.py"
```

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Complete Phase 1: Setup
2. Complete Phase 2: Foundational (CRITICAL — blocks all stories)
3. Complete Phase 3: User Story 1 — Board Monitoring & Card Dispatch
4. **STOP and VALIDATE**: Test card pickup and agent dispatch independently
5. Deploy/demo if ready — coordinare can pick up cards and dispatch to an agent

### Incremental Delivery

1. Complete Setup + Foundational → Foundation ready
2. Add User Story 1 → Test independently → Deploy/Demo (**MVP!**)
3. Add User Story 2 → Test independently → Deploy/Demo (full lifecycle loop)
4. Add User Story 3 → Test independently → Deploy/Demo (team visibility)
5. Add User Story 4 → Test independently → Deploy/Demo (exception handling)
6. Polish → Observability, containerization, production readiness

### Parallel Team Strategy

With multiple developers after Foundational is complete:

- **Developer A**: User Story 1 (P1) → User Story 2 (P2) → User Story 4 (P4)
- **Developer B**: User Story 3 (P3) → Polish (Phase 7)

US3 (notifications) is fully independent and can proceed in parallel with US1.

---

## Notes

- [P] tasks = different files, no dependencies on incomplete tasks
- [Story] label maps task to specific user story for traceability
- Each user story should be independently completable and testable
- Tests MUST fail before implementation (Constitution Principle II)
- Commit after each task or logical group
- Stop at any checkpoint to validate story independently
- All external services mocked in tests — no network calls (Constitution Principle II)
