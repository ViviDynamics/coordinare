# Tasks: Post-PR Workflow Bug Fixes

**Input**: Design documents from `/specs/017-fix-post-pr-workflow/`
**Prerequisites**: plan.md ✓, spec.md ✓, research.md ✓, quickstart.md ✓

**Tests**: Included — constitution §II mandates unit tests for every changed public function.

**Organization**: Four bug fixes map to four user stories across two source files. US1/US2/US3 share `monitor_agent.py`; US4 is independent in `dispatch_card.py`.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to

---

## Phase 1: Setup

**Purpose**: Confirm the test baseline passes before making changes. No new dependencies or files required.

- [x] T001 Run existing unit tests for the two affected files and confirm all pass: `tests/unit/graph/nodes/test_monitor_agent.py` and `tests/unit/graph/nodes/test_dispatch_card.py`

**Checkpoint**: Baseline green — all existing tests pass before any code changes.

---

## Phase 2: Foundational

No foundational work required. Both source files exist. No new modules, no new dependencies. Proceed directly to user stories.

---

## Phase 3: US1 — PR Opened Moves Card to IN_REVIEW (Priority: P1) 🎯 MVP

**Goal**: When a performer reports `pr_opened`, the GitHub project board column is updated to IN_REVIEW and the coordinare transitions to `monitoring_pr` — with no re-dispatch on the next cycle.

**Independent Test**: Set up a `monitor_agent` call with a mock GitHub service and a mock agent that returns `status="pr_opened"` with valid `pr_url` and `pr_node_id`. Assert `github.move_card` was called with `("ITEM_X", "IN_REVIEW")` and that `result["phase"] == "monitoring_pr"`.

### Tests for User Story 1

> **Write these tests FIRST and confirm they FAIL before implementing Fix 1.**

- [x] T002 [US1] Add test `test_monitor_agent_calls_move_card_in_review_on_pr_opened` to `tests/unit/graph/nodes/test_monitor_agent.py` — assert `("ITEM_1", "IN_REVIEW")` appears in `gh.move_calls` and `result["phase"] == "monitoring_pr"`
- [x] T003 [US1] Add test `test_monitor_agent_pr_opened_missing_pr_url_does_not_move_card` to `tests/unit/graph/nodes/test_monitor_agent.py` — agent returns `pr_opened` with `pr_url=None`; assert `("ITEM_1", "IN_REVIEW")` NOT in `gh.move_calls` and card status unchanged
- [x] T004 [US1] Add test `test_monitor_agent_pr_opened_missing_pr_node_id_does_not_move_card` to `tests/unit/graph/nodes/test_monitor_agent.py` — agent returns `pr_opened` with `pr_node_id=None`; assert no IN_REVIEW move occurs
- [x] T005 [US1] Add test `test_monitor_agent_pr_opened_move_card_fails_gracefully` to `tests/unit/graph/nodes/test_monitor_agent.py` — `github.move_card` raises `RuntimeError`; assert phase is still `"monitoring_pr"` (move failure must not abort the transition)

### Implementation for User Story 1

- [x] T006 [US1] In `src/coordinare/graph/nodes/monitor_agent.py`, in the `if marker == "pr_opened":` block: add validation that `status.get("pr_url")` and `status.get("pr_node_id")` are both truthy before updating card state; if either is absent set `state["phase"] = "system_error"` with an appropriate `system_error_reason` and return early
- [x] T007 [US1] In `src/coordinare/graph/nodes/monitor_agent.py`, after the card state is updated in the `pr_opened` block, add `await github.move_card(card_id, "IN_REVIEW")` inside a `try/except Exception` guard with a `logger.warning` on failure — mirroring the defensive pattern already used in `session_expired` and `blocked` handlers; guard the whole block with `if github is not None:`

**Checkpoint**: US1 complete. `test_monitor_agent_calls_move_card_in_review_on_pr_opened` passes. The root cause of the double-dispatch loop is fixed.

---

## Phase 4: US2 — Session Expiry With Existing PR Routes to Monitoring (Priority: P2)

**Goal**: When `session_expired` is received and the current card already has `pr_node_id` set, the coordinare routes to `monitoring_pr` instead of resetting the card to TODO.

**Independent Test**: Call `monitor_agent` with `status="session_expired"` and `card["pr_node_id"] = "PR_NODE_1"`. Assert `result["phase"] == "monitoring_pr"` and `("ITEM_1", "TODO")` NOT in `gh.move_calls`.

### Tests for User Story 2

> **Write these tests FIRST and confirm they FAIL before implementing Fix 2.**

- [x] T008 [US2] Add test `test_monitor_agent_session_expired_with_pr_routes_to_monitoring_pr` to `tests/unit/graph/nodes/test_monitor_agent.py` — `current_card` has `pr_node_id="PR_NODE_1"`; assert `result["phase"] == "monitoring_pr"` and no TODO move
- [x] T009 [US2] Add test `test_monitor_agent_session_expired_with_pr_clears_agent_dispatch` to `tests/unit/graph/nodes/test_monitor_agent.py` — verify `agent_dispatch` is cleared to `{}` and `agent_dispatch_at` is `None` when `pr_node_id` is set (dead session must be cleared so `relay_feedback` doesn't contact the expired performer and create a loop)
- [x] T010 [US2] Add test `test_monitor_agent_session_expired_without_pr_still_requeues_to_todo` to `tests/unit/graph/nodes/test_monitor_agent.py` — `current_card` has no `pr_node_id`; assert existing behaviour: `result["phase"] == "idle"` and `("ITEM_1", "TODO")` in `gh.move_calls`

### Implementation for User Story 2

- [x] T011 [US2] In `src/coordinare/graph/nodes/monitor_agent.py`, in the `elif marker == "session_expired":` block: at the top of the block, check `card.get("pr_node_id")`; if truthy, clear `agent_dispatch` to `{}` and `agent_dispatch_at` to `None` (dead session — keeping the expired `session_id` would let `relay_feedback` contact the performer and cause a loop), then set `state["phase"] = "monitoring_pr"` and return; only fall through to the existing TODO-reset logic when `pr_node_id` is absent

**Checkpoint**: US2 complete. Fallback safety net active: even if US1's `move_card("IN_REVIEW")` call fails transiently, the next session_expired will route to `monitoring_pr` instead of re-dispatching.

---

## Phase 5: US4 — Workspace Setup Failure Blocks Dispatch (Priority: P2)

**Goal**: Any workspace setup failure (exception or incomplete context) moves the card to BLOCKED and aborts dispatch. The performer is never started without complete workspace context.

**Independent Test**: Configure `dispatch_card` with a workspace manager whose `prepare()` raises `RuntimeError` (or returns `WorkspaceInfo` with empty `github_token`). Assert card is moved to BLOCKED, `result["phase"] == "blocked"`, and no performer dispatch call is made.

**Note**: This phase is INDEPENDENT of Phases 3–4 (different file). Can be worked in parallel with Phase 4.

### Tests for User Story 4

> **Write these tests FIRST and confirm they FAIL before implementing Fix 4.**

- [x] T012 [P] [US4] Add test `test_dispatch_card_workspace_exception_blocks_card` to `tests/unit/graph/nodes/test_dispatch_card.py` — workspace `prepare()` raises `RuntimeError`; assert `result["phase"] == "blocked"`, `("ITEM_1", "BLOCKED")` in `gh.move_calls`, and agent `dispatch_card` is NOT called
- [x] T013 [P] [US4] Add test `test_dispatch_card_workspace_empty_github_token_blocks_card` to `tests/unit/graph/nodes/test_dispatch_card.py` — workspace `prepare()` returns `WorkspaceInfo(path=Path("/tmp/ws"), branch="b", repo_url="https://github.com/o/r.git", github_token="")` ; assert card blocked with reason mentioning `github_token`
- [x] T014 [P] [US4] Add test `test_dispatch_card_workspace_empty_repo_url_blocks_card` to `tests/unit/graph/nodes/test_dispatch_card.py` — `WorkspaceInfo` has `repo_url=""`; assert card blocked
- [x] T015 [P] [US4] Add test `test_dispatch_card_workspace_complete_info_dispatches_normally` to `tests/unit/graph/nodes/test_dispatch_card.py` — all workspace fields populated; assert `result["phase"] == "monitoring_agent"` (existing happy path still works)

### Implementation for User Story 4

- [x] T016 [P] [US4] In `src/coordinare/graph/nodes/dispatch_card.py`, broaden the workspace exception catch from `except WorkspaceSetupError` to `except Exception` (re-using the same BLOCKED routing already in place); add a log line distinguishing unexpected workspace errors from `WorkspaceSetupError`
- [x] T017 [P] [US4] In `src/coordinare/graph/nodes/dispatch_card.py`, after the workspace setup block, add a completeness guard: if `workspace_manager is not None` and `workspace_info is not None`, check that `workspace_info.repo_url`, `workspace_info.branch`, and `workspace_info.github_token` are all truthy; if any is missing, move card to BLOCKED with a descriptive reason listing the missing fields and return

**Checkpoint**: US4 complete. Workspace failures now always surface as BLOCKED with a human-readable reason, never as silent no-context dispatches.

---

## Phase 6: US3 — Open Questions Preserved Across Session Expiry (Priority: P3)

**Goal**: When `session_expired` is received and `open_questions` is non-empty, the questions are saved to `card_clarifications` before being cleared, so they are available to the next performer dispatch.

**Independent Test**: Call `monitor_agent` with `status="session_expired"`, `open_questions=["What API?"]`, and no `pr_node_id`. Assert `card_clarifications` contains one entry with `questions=["What API?"]` and `answer=""`. Assert `open_questions` is `[]`.

**Note**: This fix is in the `session_expired` handler in `monitor_agent.py`. It MUST be implemented AFTER T011 (US2), since both modify the same `session_expired` block.

### Tests for User Story 3

> **Write these tests FIRST and confirm they FAIL before implementing Fix 3.**

- [x] T018 [US3] Add test `test_monitor_agent_session_expired_saves_open_questions_to_clarifications` to `tests/unit/graph/nodes/test_monitor_agent.py` — `open_questions=["What API?", "Which region?"]`, no `pr_node_id`; assert `result["card_clarifications"]` contains one entry with both questions and `answer=""`; assert `result["open_questions"] == []`
- [x] T019 [US3] Add test `test_monitor_agent_session_expired_appends_to_existing_clarifications` to `tests/unit/graph/nodes/test_monitor_agent.py` — `card_clarifications` already has one entry; `open_questions=["New Q"]`; assert `card_clarifications` now has two entries (existing preserved, new appended)
- [x] T020 [US3] Add test `test_monitor_agent_session_expired_no_questions_skips_clarification_save` to `tests/unit/graph/nodes/test_monitor_agent.py` — `open_questions=[]`; assert `card_clarifications` unchanged (no empty entry added)
- [x] T021 [US3] Add test `test_monitor_agent_session_expired_with_pr_and_open_questions_saves_before_routing` to `tests/unit/graph/nodes/test_monitor_agent.py` — `pr_node_id` set AND `open_questions` non-empty; assert questions ARE saved to `card_clarifications` even when routing to `monitoring_pr`

### Implementation for User Story 3

- [x] T022 [US3] In `src/coordinare/graph/nodes/monitor_agent.py`, in the `elif marker == "session_expired":` block, before the `state["open_questions"] = []` line: read `state.get("open_questions") or []`; if non-empty, build a clarification dict `{"questions": [str(q) for q in open_qs], "answer": ""}` and append it to `state["card_clarifications"]` (preserving any existing entries); this logic must run in BOTH branches (with `pr_node_id` and without)

**Checkpoint**: All four bugs fixed. US1+US2+US3 in `monitor_agent.py`, US4 in `dispatch_card.py`.

---

## Phase 7: Polish & Cross-Cutting Concerns

- [x] T023 [P] Add test `test_dispatch_card_workspace_manager_none_dispatches_without_workspace_context` to `tests/unit/graph/nodes/test_dispatch_card.py` — `state["workspace_manager"] = None`; assert dispatch proceeds normally and `result["phase"] == "monitoring_agent"` (this is the intended behaviour: no workspace_manager means transport handles its own setup, e.g. SSH)
- [x] T024 [P] Add test `test_state_store_preserves_card_clarifications_and_monitoring_pr_phase` to `tests/unit/test_state_store.py` — save a `WorkflowSnapshot` with `phase="monitoring_pr"` and a non-empty `card_clarifications` list; reload and assert both fields are intact (FR-007 coverage)
- [x] T025 [P] Run `ruff check src/coordinare/graph/nodes/monitor_agent.py src/coordinare/graph/nodes/dispatch_card.py` and fix any lint warnings
- [x] T026 [P] Run full test suite `.venv/bin/pytest --cov=coordinare --cov-report=term-missing` and confirm coverage does not decrease; fix any regressions

---

---

## Phase 8: US5 — PR CI Check Validation Before `pr_opened` (Priority: P2)

**Goal**: After opening a PR, the performer polls GitHub Check Runs and only reports `pr_opened` once all checks pass. If checks fail, the performer relays failure details to the backend for correction and retries. After `CHECK_MAX_ATTEMPTS` failures the card is blocked.

**Independent Test**: Run `handle_status` with a mock GitHub check-run API that returns a failing check. Confirm the performer does NOT return `pr_opened`. Confirm it calls `relay_feedback` with failure details and transitions back to `working`. When the mock then returns passing checks, confirm `pr_opened` is returned.

**Note**: This phase only touches performer code (`agent/performer/`). It is INDEPENDENT of all coordinare phases (1–7) and can run in parallel with Phase 7.

### Tests for User Story 5

> **Write these tests FIRST and confirm they FAIL before implementing Fix 5.**

- [x] T027 [P] [US5] In `agent/performer/tests/unit/test_github.py`, add `test_get_check_runs_returns_list` — mock httpx GET returning `{"check_runs": [...]}`, assert list is returned
- [x] T028 [P] [US5] In `agent/performer/tests/unit/test_github.py`, add `test_get_check_runs_raises_on_non_200` — mock returns 403, assert `GitHubAPIError` raised
- [x] T029 [P] [US5] In `agent/performer/tests/unit/test_github.py`, add `test_summarise_check_runs_all_pass_returns_pass` — all runs `completed/success`, assert `("pass", [])`
- [x] T030 [P] [US5] In `agent/performer/tests/unit/test_github.py`, add `test_summarise_check_runs_any_failure_returns_fail` — one run `completed/failure`, assert `("fail", [<that run>])`
- [x] T031 [P] [US5] In `agent/performer/tests/unit/test_github.py`, add `test_summarise_check_runs_pending_in_progress_returns_pending` — one run `in_progress`, no failures, assert `("pending", [])`
- [x] T032 [P] [US5] In `agent/performer/tests/unit/test_github.py`, add `test_summarise_check_runs_empty_list_returns_pass` — empty list, assert `("pass", [])`
- [x] T033 [P] [US5] In `agent/performer/tests/unit/test_github.py`, add `test_summarise_check_runs_action_required_treated_as_failure` — `conclusion="action_required"`, assert `"fail"` verdict
- [x] T034 [P] [US5] In `agent/performer/tests/unit/test_main.py`, add `test_handle_status_backend_done_transitions_to_waiting_for_checks` — backend returns `done`, mock `push_branch` + `create_pull_request` + `get_head_sha`; assert response is `working` (not `pr_opened`) and `perf.state == "waiting_for_checks"`
- [x] T035 [P] [US5] In `agent/performer/tests/unit/test_main.py`, add `test_handle_status_waiting_checks_all_pass_returns_pr_opened` — `perf.state == "waiting_for_checks"`, mock checks return pass; assert response `status == "pr_opened"`
- [x] T036 [P] [US5] In `agent/performer/tests/unit/test_main.py`, add `test_handle_status_waiting_checks_pending_returns_working` — mock checks return pending; assert response `status == "working"` with progress message
- [x] T037 [P] [US5] In `agent/performer/tests/unit/test_main.py`, add `test_handle_status_waiting_checks_failure_relays_to_backend` — mock checks return one failing run; assert `backend.relay_feedback` called with failure details and `perf.state == "working"` and `perf.check_attempt == 1`
- [x] T038 [P] [US5] In `agent/performer/tests/unit/test_main.py`, add `test_handle_status_waiting_checks_failure_at_max_attempts_returns_blocked` — `perf.check_attempt == CHECK_MAX_ATTEMPTS`; mock checks return failure; assert response `status == "blocked"` and `questions` contains failing check name
- [x] T039 [P] [US5] In `agent/performer/tests/unit/test_main.py`, add `test_handle_status_check_api_error_returns_working_does_not_increment_attempt` — `get_check_runs` raises `GitHubAPIError`; assert response `status == "working"` and `perf.check_attempt` unchanged (transient errors don't consume retry budget)

### Implementation for User Story 5

- [x] T040 [P] [US5] In `agent/performer/src/performer/github.py`, add `get_check_runs(owner, repo, ref, token) -> list[dict]` — GET `/repos/{owner}/{repo}/commits/{ref}/check-runs`, return `check_runs` array from response; raise `GitHubAPIError` on non-2xx
- [x] T041 [P] [US5] In `agent/performer/src/performer/github.py`, add `summarise_check_runs(check_runs) -> tuple[Literal["pass","fail","pending"], list[dict]]` — classify by `status`/`conclusion`; treat `failure`, `timed_out`, `cancelled`, `action_required` as failures; empty list → `("pass", [])`
- [x] T042 [P] [US5] In `agent/performer/src/performer/models.py`, add `pr_head_sha: str | None = None` and `check_attempt: int = 0` fields to `Performance` dataclass
- [x] T043 [P] [US5] In `agent/performer/src/performer/models.py`, add `"waiting_for_checks"` to the `PerformanceState` literal type
- [x] T044 [P] [US5] In `agent/performer/src/performer/workspace.py`, add `get_head_sha(stand)` function and `head_sha: str = ""` field to `Stand`
- [x] T045 [P] [US5] In `agent/performer/src/performer/config.py`, add `CHECK_MAX_ATTEMPTS: int = 3` to `Settings`
- [x] T046 [P] [US5] In `agent/performer/src/performer/main.py`, extend `handle_status()` with `waiting_for_checks` state and `_poll_check_runs()` helper; add early-return guard for `perf.state == "blocked"` to prevent repeated polls from falling through to `backend.get_status()` and re-executing the push/PR-open path; persist questions in `perf.open_questions` in the max-attempts blocked branch so the stable response is available across polls
- [x] T047 [P] [US5] In `agent/performer/src/performer/main.py`, add `_format_check_failures(failed_runs: list[dict]) -> str` helper
- [x] T047b [P] [US5] In `agent/performer/tests/unit/test_main.py`, add `test_blocked_state_stable_on_repeated_poll` — set `perf.state = "blocked"` with questions already on `perf.open_questions`; assert response is `blocked` with those questions and `backend.get_status` was NOT called
- [x] T047c [P] [US5] In `agent/performer/tests/unit/test_main.py`, add `test_blocked_state_questions_persisted_on_perf` — run through max-attempts blocked path; assert `perf.open_questions == resp.questions`
- [x] T048 [P] [US5] Run `ruff check agent/performer/src/performer/` and fix any lint warnings introduced by US5 changes
- [x] T049 [P] [US5] Run performer unit tests `cd agent/performer && uv run pytest` and confirm all pass including the new US5 tests

**Checkpoint**: US5 complete. Performer never reports `pr_opened` with failing CI checks. Failed checks trigger automatic correction attempts. After `CHECK_MAX_ATTEMPTS` the card is blocked.

---

## Dependencies & Execution Order

### Phase Dependencies

- **Phase 1 (Setup)**: No dependencies — start immediately
- **Phase 2 (Foundational)**: Skipped — nothing to do
- **Phase 3 (US1)**: Depends on Phase 1; BLOCKING — the root cause fix
- **Phase 4 (US2)**: Depends on Phase 3 (same file — sequential to avoid merge conflicts)
- **Phase 5 (US4)**: Independent of Phases 3–4 (different file) — CAN RUN IN PARALLEL with Phase 4
- **Phase 6 (US3)**: Depends on Phase 4 (modifies the same `session_expired` block as US2)
- **Phase 7 (Polish)**: Depends on all coordinare story phases complete
- **Phase 8 (US5)**: Independent of all coordinare phases — different package entirely; CAN RUN IN PARALLEL with any phase

### User Story Dependencies

- **US1 (P1)**: Independent — start after Phase 1
- **US2 (P2)**: Depends on US1 being committed (same file, adjacent code)
- **US4 (P2)**: Independent of US1/US2/US3 — different file, can run in parallel
- **US3 (P3)**: Depends on US2 (same handler, must apply after US2's changes)
- **US5 (P2)**: Independent of all coordinare stories — lives entirely in `agent/performer/`

### Within Each User Story

- Write failing tests FIRST — verify they fail before implementing
- Implement fix — verify tests now pass
- Commit before moving to next story

---

## Parallel Example: US4 alongside US2

```bash
# After Phase 3 (US1) is committed:
# Launch US2 (monitor_agent.py) and US4 (dispatch_card.py) concurrently:

Task A: "Add session_expired pr_node_id check in monitor_agent.py (T008–T011)"
Task B: "Add workspace validation in dispatch_card.py (T012–T017)"

# Both complete independently; merge order doesn't matter
```

---

## Implementation Strategy

### MVP First (US1 Only)

1. Complete Phase 1: Confirm baseline passes
2. Complete Phase 3: US1 — add the missing `move_card("IN_REVIEW")` call
3. **STOP and VALIDATE**: This alone fixes the double-dispatch loop (Bug 1)
4. Proceed to US2/US4/US3 for belt-and-suspenders coverage

### Incremental Delivery

1. Phase 1 → baseline confirmed
2. Phase 3 (US1) → root cause of double-dispatch fixed
3. Phase 4 (US2) + Phase 5 (US4) in parallel → fallback safety net + workspace validation
4. Phase 6 (US3) → context preservation hardened
5. Phase 7 → lint + coverage verified

---

## Notes

- All changes are in two files: `monitor_agent.py` (US1/US2/US3) and `dispatch_card.py` (US4)
- No new files, no new dependencies
- The `session_expired` handler changes (US2 + US3) must be applied sequentially in the same file — implement US2 first, then US3 builds on it
- T024 (StateStore round-trip test) satisfies FR-007: the StateStore serialises all CoordinareState fields automatically, so the test is a verification rather than a new capability
- T023 (`workspace_manager=None` test) confirms the intentional behaviour: if no workspace manager is configured, dispatch proceeds — this covers SSH and other transports where the performer manages its own workspace
- T027–T049 are all performer-side changes in `agent/performer/`; they have no dependencies on the coordinare tasks and can be worked independently (different package, different test runner — use `cd agent/performer && uv run pytest`)
