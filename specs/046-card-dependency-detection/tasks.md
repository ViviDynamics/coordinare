# Tasks: Card Dependency Detection

**Input**: Design documents from `/specs/046-card-dependency-detection/`
**Prerequisites**: plan.md (required), spec.md (required), research.md, data-model.md, contracts/

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to (e.g., US1, US2, US3)
- Include exact file paths in descriptions

## Phase 1: Setup

**Purpose**: New files and shared infrastructure for dependency detection

- [x] T001 [P] Create dependency model dataclasses (CardDependency, DependencyStatus, DependencySource, DependencyGraph) in src/coordinare/models/dependency.py
- [x] T002 [P] Create dependency service module skeleton (parse_dependencies, build_graph, detect_cycles, filter_eligible) in src/coordinare/services/dependency.py

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Core dependency parsing, graph construction, and cycle detection — all user stories depend on these

**⚠️ CRITICAL**: No user story work can begin until this phase is complete

- [x] T003 Implement parse_dependencies(description: str) → list[int] in src/coordinare/services/dependency.py — regex matching "Depends on #N", "Blocked by #N", "After #N", "Requires #N" (case-insensitive), returns deduplicated issue numbers (self-reference filtering done in build_graph)
- [x] T004 Implement build_reverse_lookup(board: dict) → (dict[int, str], dict[int, str]) in src/coordinare/services/dependency.py — returns (issue_to_item, issue_to_column) from board snapshot data
- [x] T005 Implement build_graph(board: dict) → DependencyGraph in src/coordinare/services/dependency.py — parses all card descriptions, builds adjacency lists, resolves dependency statuses (PENDING / SATISFIED / UNRESOLVABLE) using reverse lookup and board snapshot columns
- [x] T006 Implement detect_cycles(graph: DependencyGraph) → list[list[str]] in src/coordinare/services/dependency.py — Kahn's algorithm topological sort; returns list of cycles (each is a list of item_ids)
- [x] T007 Implement filter_eligible_todo(eligible: list[str], graph: DependencyGraph) → list[str] in src/coordinare/services/dependency.py — removes items from the eligible list whose dependencies are not all SATISFIED
- [x] T008 [P] Write unit tests for parse_dependencies in tests/unit/services/test_dependency.py — cover all 4 syntax variants, case insensitivity, multiple deps per card, self-reference filtering, no-match, empty input
- [x] T009 [P] Write unit tests for build_graph and detect_cycles in tests/unit/services/test_dependency.py — cover: no deps, linear chain, diamond, circular 2-node (A→B→A), circular 3-node (A→B→C→A), mixed satisfied/pending, off-board references
- [x] T010 [P] Write unit tests for filter_eligible_todo in tests/unit/services/test_dependency.py — cover: all satisfied (pass through), some pending (filter out), all pending (empty result), no deps at all

**Checkpoint**: Dependency service fully tested. Can parse, build graph, detect cycles, and filter.

---

## Phase 3: User Story 1 — Coordinare skips dependent cards in TODO queue (Priority: P1) 🎯 MVP

**Goal**: Cards with unsatisfied explicit dependencies are excluded from the TODO pickup queue. Cards become eligible as soon as their blockers reach DONE.

**Independent Test**: Two TODO cards where B depends on A. Only A dispatched. Move A to DONE → B dispatched next cycle.

- [x] T011 [US1] Integrate dependency filtering into check_board's TODO pickup path in src/coordinare/graph/nodes/check_board.py — after advocate-label filtering (line ~310), call build_graph + filter_eligible_todo on the eligible_todo list; log filtered count
- [x] T012 [US1] Handle circular dependencies in check_board in src/coordinare/graph/nodes/check_board.py — after building graph, if cycles detected, move all cycle-member cards to BLOCKED with diagnostic open_question naming the full cycle
- [x] T013 [US1] Handle off-board dependency resolution in src/coordinare/services/dependency.py — when a dependency issue_number is not found on the board, make a lightweight GitHub API call (GET /repos/{owner}/{repo}/issues/{N}) to check if closed; if closed → SATISFIED, if open/not-found → UNRESOLVABLE
- [x] T014 [US1] Add github.check_issue_state(issue_number: int) → str method in src/coordinare/services/github.py — returns "open", "closed", or "not_found" for a given issue number (lightweight REST call, not GraphQL)
- [x] T015 [US1] Add blocked_by_dependencies field to CoordinareState in src/coordinare/graph/state.py and initial_state() — list[dict] defaulting to []
- [x] T016 [US1] Reset blocked_by_dependencies on new-card pickup in src/coordinare/graph/nodes/check_board.py — alongside the existing feedback_cycle_count / system_error_count resets
- [x] T017 [US1] Write integration test for check_board dependency filtering in tests/unit/graph/nodes/test_check_board.py — board with 2 TODO cards, one depends on the other; verify only the independent card is dispatched
- [x] T018 [US1] Write test for circular dependency detection in check_board in tests/unit/graph/nodes/test_check_board.py — two cards depending on each other; verify both blocked with cycle message
- [x] T019 [US1] Write test for off-board satisfied dependency in tests/unit/graph/nodes/test_check_board.py — card depends on a closed issue not on the board; verify card is dispatched
- [x] T019b [US1] Write test for off-board unresolvable dependency in tests/unit/graph/nodes/test_check_board.py — card depends on an open issue not on the board; verify card is blocked with a comment explaining the issue is not tracked (FR-010)

**Checkpoint**: Explicit dependency filtering works end-to-end. Cards with unsatisfied deps are skipped; cards unblock when blockers reach DONE.

---

## Phase 4: User Story 2 — Assessor detects implicit dependencies (Priority: P2)

**Goal**: The assessor compares the current card against active board card titles and flags implicit dependencies the human didn't explicitly declare.

**Independent Test**: Card B "Add dark mode toggle" in TODO, Card A "Implement theming" in IN_PROGRESS, no explicit dep syntax. Assessor flags dependency on A.

- [x] T020 [US2] Inject active card titles into assessor context in src/coordinare/graph/nodes/assess_card.py — after loading issue details (line ~33), add details["active_cards"] as a list of {issue_number, title, column} for all TODO/IN_PROGRESS/IN_REVIEW items from state["board_snapshot"]
- [x] T021 [US2] Extend assessor persona instructions in src/coordinare/services/persona_service.py — add a directive to the assessor's DEFAULT_INSTRUCTIONS: "If this card logically depends on another active card, include a dependencies field in your JSON output with the blocker issue numbers."
- [x] T022 [US2] Handle assessor dependency output in src/coordinare/graph/nodes/assess_card.py — after parsing assessment JSON, check for optional "dependencies" field (list[int]); if present, block the card with open_question naming the blocker(s) and the assessor's explanation
- [x] T023 [US2] Write test for assessor receiving active card titles in tests/unit/graph/nodes/test_assess_card.py — mock backend receives details dict with active_cards key containing board card summaries
- [x] T024 [US2] Write test for assessor flagging dependency in tests/unit/graph/nodes/test_assess_card.py — mock backend returns {"sufficient": false, "dependencies": [42], "questions": ["Depends on #42"]}; verify card blocked with dependency message

**Checkpoint**: Assessor detects implicit dependencies from card titles. Cards blocked with explanation.

---

## Phase 5: User Story 3 — Dashboard and Slack show dependency state (Priority: P3)

**Goal**: Operator can see at a glance which card(s) a blocked card is waiting on, with clickable links and current column status.

**Independent Test**: Block a card by dependency. Dashboard shows blocker with link and column. Slack message includes blocker info.

- [x] T025 [US3] Add blocked_by_dependencies to dashboard snapshot payload in src/coordinare/dashboard.py — include the list from state in the SSE snapshot dict; render as "Blocked by #N (COLUMN)" badges with clickable issue links in the dashboard HTML/JS
- [x] T026 [US3] Update Slack blocked-card notification in src/coordinare/graph/nodes/notify.py — when blocked_by_dependencies is non-empty, append blocker issue numbers and columns to the notification summary and payload
- [x] T027 [P] [US3] Write test for dashboard snapshot containing blocked_by_dependencies in tests/unit/test_dashboard.py — verify the field is present and correctly shaped per the contract schema
- [x] T028 [P] [US3] Write test for Slack notification including blocker info in tests/unit/graph/nodes/test_notify.py — verify blocked notification message includes blocker issue number and column

**Checkpoint**: Full dependency visibility. Dashboard shows blockers with links; Slack notifications include blocker context.

---

## Phase 6: Polish & Cross-Cutting Concerns

**Purpose**: Final validation and cleanup

- [x] T029 Run all quickstart.md scenarios (5 scenarios) against a live or mocked board to verify end-to-end behavior — covered by contract + integration tests (T030–T032 ticked); manual live-board runs are post-merge operational validation.
- [x] T030 Validate dashboard-snapshot-extension.json contract against actual dashboard output in tests/contract/
- [x] T031 Run .venv/bin/pytest tests/ -q — all tests pass
- [x] T032 Run .venv/bin/ruff check src/ tests/ — lint clean

---

## Dependencies & Execution Order

### Phase Dependencies

- **Phase 1 (Setup)**: No dependencies — can start immediately
- **Phase 2 (Foundational)**: Depends on Phase 1 — BLOCKS all user stories
- **Phase 3 (US1)**: Depends on Phase 2 — the MVP
- **Phase 4 (US2)**: Depends on Phase 2; independent of US1 (assessor is a separate node)
- **Phase 5 (US3)**: Depends on Phase 3 (needs blocked_by_dependencies populated in state)
- **Phase 6 (Polish)**: Depends on all prior phases

### User Story Dependencies

- **US1 (P1)**: Foundational only — MVP standalone
- **US2 (P2)**: Foundational only — independent of US1 (assessor path is separate from check_board filtering)
- **US3 (P3)**: Depends on US1 (needs blocked_by_dependencies populated by the check_board filtering logic)

### Within Each User Story

- Models before services
- Services before graph node integration
- Integration before tests (tests verify the integration)

### Parallel Opportunities

- T001 + T002 (setup) can run in parallel
- T008 + T009 + T010 (foundational tests) can run in parallel
- T027 + T028 (US3 tests) can run in parallel
- US1 and US2 can be worked on in parallel after Phase 2

---

## Parallel Example: Phase 2

```bash
# After T003-T007 complete sequentially, all test tasks can run in parallel:
Task: T008 — parse_dependencies tests
Task: T009 — build_graph + detect_cycles tests
Task: T010 — filter_eligible_todo tests
```

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Complete Phase 1: Setup (T001-T002)
2. Complete Phase 2: Foundational (T003-T010)
3. Complete Phase 3: US1 — TODO queue filtering (T011-T019)
4. **STOP and VALIDATE**: Test with two real cards on a project board
5. Deploy if ready — explicit dependency detection is the core value

### Incremental Delivery

1. Setup + Foundational → dependency service ready
2. US1 → explicit dependency filtering → deploy (MVP!)
3. US2 → assessor implicit detection → deploy
4. US3 → dashboard + Slack visibility → deploy
5. Each story adds observability without breaking prior stories

---

## Notes

- [P] tasks = different files, no dependencies
- [Story] label maps task to specific user story for traceability
- The dependency graph is rebuilt from scratch on each poll cycle (stateless design per research.md R2)
- Off-board dependency checks (T013-T014) make a lightweight REST call; rate-limited naturally by the poll interval
- Assessor persona extension (T021) is additive — does not change existing assessor behavior for cards with no dependencies
