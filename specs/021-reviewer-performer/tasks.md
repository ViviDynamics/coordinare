# Tasks: Reviewer Performer

**Input**: Design documents from `/specs/021-reviewer-performer/`
**Prerequisites**: plan.md (required), spec.md (required for user stories)

**Organization**: Tasks are grouped by user story to enable independent implementation and testing of each story.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to (e.g., US1, US2, US3)
- Include exact file paths in descriptions

---

## Phase 1: Setup (Shared Infrastructure)

**Purpose**: Config extensions, model additions, and GitHub API helper that all user stories depend on

- [X] T001 Add `REVIEWER_MAX_CYCLES: int = 3` to `agent/performer/src/performer/config.py`
- [X] T002 Add `"approved"` and `"changes_requested"` to `PerformanceState` in `agent/performer/src/performer/models.py`; add `review_comments: list[dict] = field(default_factory=list)` and `review_cycle: int = 0` fields to the `Performance` dataclass
- [X] T003 Add `"approved"` and `"changes_requested"` to `PerformerStatusType` in `agent/performer/src/performer/protocol.py`; add `comments: list[dict] = Field(default_factory=list)` and `suggestions: list[str] = Field(default_factory=list)` fields to `PerformerResponse`
- [X] T004 Add `"approved"` and `"changes_requested"` to `StatusType` in `src/coordinare/protocol.py`; add `comments` and `suggestions` fields to `ProtocolResponse`
- [X] T005 Update contract test in `tests/contract/test_agent_protocol.py`: add `approved` and `changes_requested` to expected status values
- [X] T006 Write unit tests in `agent/performer/tests/unit/test_models.py` (extend): verify `approved` and `changes_requested` are valid `PerformanceState` values; verify `Performance.review_comments` defaults to empty list; verify `Performance.review_cycle` defaults to 0

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: GitHub Reviews API helper — MUST complete before user stories

**⚠️ CRITICAL**: No user story work can begin until this phase is complete

- [X] T007 Implement `post_pull_request_review(owner, repo, pr_number, event, body, comments, token)` async function in `agent/performer/src/performer/github.py`: POST to `/repos/{owner}/{repo}/pulls/{pr_number}/reviews` with event type (`APPROVE`, `REQUEST_CHANGES`, `COMMENT`), body text, and optional inline comments (each with `path`, `line`, `body`); use existing `httpx` client pattern; raise `GitHubAPIError` on non-2xx response
- [X] T008 Write unit tests for `post_pull_request_review` in `agent/performer/tests/unit/test_github.py` (extend): test APPROVE event sends correct payload; test REQUEST_CHANGES event includes comments; test non-2xx response raises `GitHubAPIError`; test empty comments list is valid

**Checkpoint**: GitHub Reviews API helper ready. New statuses available in models and protocol.

---

## Phase 3: User Story 1 — Approve Clean Implementation (Priority: P1) 🎯 MVP

**Goal**: Reviewer analyses the PR diff against the architecture plan and acceptance criteria. If all is well, posts an APPROVE review and returns `approved`.

**Independent Test**: Dispatch the reviewer with a clean PR, verify `approved` status and GitHub APPROVE review posted.

### Implementation for User Story 1

- [X] T009 [US1] Modify `agent/performer/src/performer/main.py`: in `handle_status` where `backend_status.state == "done"`, add a reviewer branch gated on `perf.role == "reviewing"`; parse `backend_status.output` as JSON containing `approved` (bool), `comments` (list), `suggestions` (list), `body` (str); if `approved == True`, call `post_pull_request_review` with event `APPROVE`, set `perf.state = "approved"`, return `PerformerResponse(status="approved", suggestions=suggestions)`
- [X] T010 [US1] Add `approved` to the terminal state guard in `agent/performer/src/performer/main.py` (alongside `pr_opened` and `plan_committed`) so subsequent status polls return the cached result
- [X] T011 [US1] Add `approved` to the message loop break condition in `agent/performer/src/performer/main.py` (alongside `pr_opened`, `plan_committed`, `error`)
- [X] T012 [US1] Verify `approved` is already in `TERMINAL_SUCCESS_STATES` in `src/coordinare/graph/nodes/monitor_performer.py` (added in 019 — confirm present)
- [X] T013 [P] [US1] Write unit tests in `agent/performer/tests/unit/test_main.py` (extend TestArchitectPerformer or new TestReviewerPerformer class): test reviewer role with approved backend output → returns `approved` status; test `post_pull_request_review` called with event `APPROVE`; test suggestions are included in response; test implementer role is unaffected (no regression)
- [X] T014 [US1] Run full test suite: `cd agent/performer && .venv/bin/pytest tests/unit/ -q` — confirm no regressions

**Checkpoint**: US1 complete. Reviewer can approve clean PRs. SC-002 verified.

---

## Phase 4: User Story 2 — Request Changes on Flawed Implementation (Priority: P1)

**Goal**: Reviewer posts REQUEST_CHANGES review with inline comments and returns `changes_requested`. Coordinare relays comments to the implementer. On re-dispatch after fixes, reviewer re-evaluates.

**Independent Test**: Dispatch the reviewer with a flawed PR, verify `changes_requested` with comments. Re-dispatch after fix, verify `approved`.

### Implementation for User Story 2

- [X] T015 [US2] Modify reviewer branch in `agent/performer/src/performer/main.py`: if `approved == False` in backend output, call `post_pull_request_review` with event `REQUEST_CHANGES` and inline comments; increment `perf.review_cycle`; if `review_cycle >= REVIEWER_MAX_CYCLES`, set `perf.state = "blocked"` with unresolved summary; otherwise set `perf.state = "changes_requested"` and return `PerformerResponse(status="changes_requested", comments=comments)`
- [X] T016 [US2] Add `changes_requested` to the terminal state guard in `agent/performer/src/performer/main.py` so subsequent polls return the cached result with comments
- [X] T017 [US2] Add `changes_requested` to the message loop break condition
- [X] T018 [US2] Verify coordinare handles `changes_requested` in `src/coordinare/graph/nodes/monitor_performer.py`: `changes_requested` is NOT in `TERMINAL_SUCCESS_STATES` (it's a non-terminal outcome that should NOT advance the lifecycle); the coordinare should treat it like `blocked` or route the comments back to the implementer. Add `changes_requested` handling: extract comments from status, store as `relay_feedback` in state, reset `performer_stage` to `"implementing"`, set `phase = "dispatching"`
- [X] T019 [P] [US2] Write unit tests in `agent/performer/tests/unit/test_main.py` (extend): test reviewer with changes_requested → returns comments; test max cycle limit reached → returns blocked; test `post_pull_request_review` called with event `REQUEST_CHANGES` and comments array; test review_cycle increments
- [X] T020 [P] [US2] Write unit tests in `tests/unit/graph/nodes/test_monitor_performer.py` (extend): test `changes_requested` status routes comments to relay_feedback and resets performer_stage to implementing
- [X] T021 [US2] Run full test suite: `.venv/bin/pytest tests/unit/ -q` and `cd agent/performer && .venv/bin/pytest tests/unit/ -q` — confirm no regressions

**Checkpoint**: US2 complete. Reviewer can request changes and the coordinare relays them to the implementer. SC-001 and SC-003 verified.

---

## Phase 5: User Story 3 — Architecture Deviation Detection (Priority: P2)

**Goal**: Reviewer specifically flags architecture deviations by comparing the implementation against the plan file.

**Independent Test**: Dispatch the reviewer with a branch that deviates from the architecture plan, verify the review comment references the architecture plan.

### Implementation for User Story 3

- [X] T022 [US3] Verify that the reviewer's AI backend receives the architecture plan content via the dispatch payload's `architecture_plan_path` field (set by 020's dispatch_performer); the reviewer's prompt/context includes the plan for comparison — this is a backend prompting concern, not a performer code change
- [X] T023 [US3] Write unit test: dispatch the reviewer with a card that has `architecture_plan_path`, verify the payload passed to the backend includes the plan path reference
- [X] T024 [US3] Verify that when the architecture plan file is absent from the branch (architect was skipped), the reviewer still proceeds based on acceptance criteria alone — no crash or error (FR-010)

**Checkpoint**: US3 complete. Architecture deviation detection is a backend prompting concern; the performer correctly forwards the plan context.

---

## Phase 6: Polish & Cross-Cutting Concerns

- [X] T025 Run full test suite: `.venv/bin/pytest tests/unit/ -q` and `cd agent/performer && .venv/bin/pytest tests/unit/ -q` — confirm all pass
- [X] T026 Run linter: `.venv/bin/ruff check agent/performer/src/performer/main.py agent/performer/src/performer/models.py agent/performer/src/performer/github.py agent/performer/src/performer/config.py` — zero warnings
- [X] T027 Verify backward compatibility: implementer and architect roles still produce their expected terminal states (`pr_opened` and `plan_committed`)
- [X] T028 Verify `approved` is in `TERMINAL_SUCCESS_STATES` in `src/coordinare/graph/nodes/monitor_performer.py`

---

## Edge Cases (scoping decision)

**In-scope** (covered by tasks):
- **Architecture plan absent**: T024 — reviewer proceeds on acceptance criteria alone
- **Max review cycles reached**: T015 — returns `blocked` with unresolved summary
- **Re-dispatch after fix**: T008/T015 — each review is a new session; reviewer re-reads current diff

**Deferred**:
- **PR has no diff**: Backend prompting concern; performer returns whatever the backend produces
- **GitHub API failure posting review**: Existing `GitHubAPIError` handling propagates the error
- **Reviewer escalation to architect**: Future feature; currently reviewer blocks for human attention

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: No dependencies — start immediately
- **Foundational (Phase 2)**: Depends on Setup — BLOCKS user stories
- **US1 (Phase 3)**: Depends on Foundational — approve path (MVP)
- **US2 (Phase 4)**: Depends on US1 — changes_requested builds on the reviewer branch
- **US3 (Phase 5)**: Depends on Foundational — can parallel with US1/US2
- **Polish (Phase 6)**: Depends on all user stories

### Parallel Opportunities

- T013 and T019/T020 can run in parallel (different test files)
- US3 can proceed in parallel with US1/US2 after Foundational

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Phase 1: Setup (config, models, protocol)
2. Phase 2: Foundational (GitHub Reviews API helper)
3. Phase 3: US1 (approve path)
4. **STOP and VALIDATE**: Reviewer approves clean PRs

### Notes

- Changes span `agent/performer/` (primary) and `src/coordinare/` (protocol + monitor_performer)
- `approved` is already in 019's `TERMINAL_SUCCESS_STATES` — no lifecycle change needed
- `changes_requested` is a NEW non-terminal outcome requiring coordinare-side handling in `monitor_performer`
- Each review cycle is a fresh session — no in-memory state between dispatches
- The reviewer reads the architecture plan from the dispatch payload (set by 020)
