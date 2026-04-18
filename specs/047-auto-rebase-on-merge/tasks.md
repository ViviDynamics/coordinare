# Tasks: Auto-Rebase Active Branches on Merge

**Input**: Design documents from `/specs/047-auto-rebase-on-merge/`
**Prerequisites**: plan.md (required), spec.md (required), research.md, data-model.md, contracts/

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can run in parallel (different files, no dependencies)
- **[Story]**: Which user story this task belongs to (e.g., US1, US2, US3)
- Include exact file paths in descriptions

## Phase 1: Setup

**Purpose**: New files and shared infrastructure for rebase operations

- [x] T001 [P] Create rebase model dataclasses (RebaseOutcome, RebaseJob, RebaseRound) in src/coordinare/models/rebase.py
- [x] T002 [P] Create rebase service module skeleton (detect_stale_branches, rebase_branch, force_push_with_lease, run_rebase_round) in src/coordinare/services/rebase.py

---

## Phase 2: Foundational (Blocking Prerequisites)

**Purpose**: Core rebase operations — clone, rebase, push, conflict detection. All user stories depend on these.

**⚠️ CRITICAL**: No user story work can begin until this phase is complete

- [x] T003 Add last_known_main_sha (str | None) and last_rebase_round (dict | None) fields to CoordinareState in src/coordinare/graph/state.py and initial_state()
- [x] T004 Implement _run_git(args, cwd, env, timeout) async helper in src/coordinare/services/rebase.py — async subprocess wrapper for git CLI commands with timeout, returns (exit_code, stdout, stderr)
- [x] T005 Implement fetch_main_sha(repo_url, token) in src/coordinare/services/rebase.py — runs git ls-remote refs/heads/main to get current main HEAD SHA without a full clone
- [x] T006 Implement detect_stale_branches(active_sessions, main_sha) in src/coordinare/services/rebase.py — returns list of (card_id, branch, pr_number, current_sha) for sessions whose branches are behind main; skips sessions in monitoring_performer phase (FR-006 active-session guard)
- [x] T007 Implement rebase_branch(repo_url, branch, main_sha, token, timeout) in src/coordinare/services/rebase.py — clones repo into temp dir, fetches main + branch, runs git rebase origin/main; returns RebaseJob with outcome CLEAN or conflict info (conflicted files, conflict preview); cleans up temp dir on completion
- [x] T008 Implement force_push_with_lease(repo_dir, branch, expected_sha, token) in src/coordinare/services/rebase.py — runs git push --force-with-lease=branch:expected_sha; returns success/failure; retries once on lease failure after re-fetching
- [x] T009 Implement run_rebase_round(active_sessions, main_sha, repo_url, token, github) in src/coordinare/services/rebase.py — orchestrates: detect_stale → rebase_branch for each → force_push → collect into RebaseRound; branches are processed independently (FR-007)
- [x] T010 [P] Write unit tests for fetch_main_sha in tests/unit/services/test_rebase.py — mock subprocess, verify git ls-remote parsing
- [x] T011 [P] Write unit tests for detect_stale_branches in tests/unit/services/test_rebase.py — cover: no sessions, all up-to-date, some stale, active performer skipped (FR-006)
- [x] T012 [P] Write unit tests for rebase_branch in tests/unit/services/test_rebase.py — cover: clean rebase, conflict detected (exit code + file list), already up-to-date, git failure
- [x] T013 [P] Write unit tests for force_push_with_lease in tests/unit/services/test_rebase.py — cover: success, lease rejection + retry, persistent failure

**Checkpoint**: Rebase service fully tested. Can detect stale branches, rebase, push, and collect results.

---

## Phase 3: User Story 1 — Clean rebase after sibling merge (Priority: P1) 🎯 MVP

**Goal**: After a PR merges to main, all other in-flight PR branches are automatically rebased and force-pushed. No conflicts in this story — clean rebases only.

**Independent Test**: Two PRs on separate files. Merge one. Verify the other's branch is rebased onto new main within one poll cycle.

- [x] T014 [US1] Trigger rebase round from merge_pr after successful squash merge in src/coordinare/graph/nodes/merge_pr.py — after github.squash_merge() succeeds, call run_rebase_round() with active_sessions and the new merge SHA; store result as last_rebase_round in state
- [x] T015 [US1] Detect external merges (non-coordinare PRs) in src/coordinare/graph/nodes/check_board.py — at the start of check_board, if last_known_main_sha is set, fetch current main SHA via fetch_main_sha; if changed, trigger run_rebase_round for all active sessions; update last_known_main_sha
- [x] T016 [US1] Initialize last_known_main_sha on first poll in src/coordinare/graph/nodes/check_board.py — on the first cycle (last_known_main_sha is None), set it from fetch_main_sha without triggering a rebase
- [x] T017 [US1] Write test for merge_pr triggering rebase round in tests/unit/graph/nodes/test_merge_pr.py — mock run_rebase_round, verify it's called after successful merge with correct args
- [x] T018 [US1] Write test for check_board detecting external merge in tests/unit/graph/nodes/test_check_board.py — mock fetch_main_sha returning a new SHA, verify rebase round triggered
- [x] T019 [US1] Write test for check_board first-cycle initialization in tests/unit/graph/nodes/test_check_board.py — verify last_known_main_sha is set without triggering rebase

**Checkpoint**: Clean rebases fire automatically after both coordinare and external merges. No conflict handling yet.

---

## Phase 4: User Story 2 — Performer resolves merge conflicts (Priority: P2)

**Goal**: When a rebase produces conflicts, dispatch a performer to resolve them. Only block if the performer fails.

**Independent Test**: Two PRs editing the same file. Merge one. Verify performer resolves the conflict on the other.

- [x] T020 [US2] Implement extract_conflict_info(repo_dir) in src/coordinare/services/rebase.py — after a failed rebase, reads git diff --name-only --diff-filter=U for conflicted files, reads conflict markers from each file (truncated to 500 chars per file), returns (conflicted_files, conflict_preview)
- [x] T021 [US2] Implement dispatch_conflict_resolver(card_id, branch, conflicted_files, conflict_preview, state) in src/coordinare/services/rebase.py — prepares relay_feedback with conflict details and instructions, sets performer_stage to implementing, sets phase to dispatching; the existing dispatch_performer → monitor_performer pipeline handles the rest
- [x] T022 [US2] Wire conflict path into rebase_branch in src/coordinare/services/rebase.py — when git rebase exits with conflicts, call extract_conflict_info; if conflict_preview is non-empty, set RebaseJob outcome to a new CONFLICT_PENDING status; run_rebase_round then calls dispatch_conflict_resolver for pending jobs
- [x] T023 [US2] Handle performer conflict resolution result in src/coordinare/services/rebase.py — after performer completes (returns pr_opened or approved), force-push the resolved branch; on performer error/blocked, set RebaseJob outcome to BLOCKED and post diagnostic comment via github.add_comment with conflicted files + preview + @-mention of human_reviewers
- [x] T024 [US2] Write test for extract_conflict_info in tests/unit/services/test_rebase.py — mock git working directory with conflict markers, verify file list and preview extraction
- [x] T025 [US2] Write test for dispatch_conflict_resolver in tests/unit/services/test_rebase.py — verify relay_feedback contains conflict details, performer_stage set to implementing
- [x] T026 [US2] Write test for blocked-on-unresolvable-conflict in tests/unit/services/test_rebase.py — performer returns error, verify card moved to BLOCKED with diagnostic comment containing file paths and conflict preview

**Checkpoint**: Performer-driven conflict resolution works. Cards only block on genuinely unresolvable conflicts.

---

## Phase 5: User Story 3 — Operator notified of rebase outcomes (Priority: P3)

**Goal**: Single Slack summary per rebase round + dashboard shows per-card rebase status.

**Independent Test**: Merge a PR with two in-flight branches. Verify one Slack message and dashboard tiles.

- [x] T027 [US3] Add rebase_round_complete notification event type in src/coordinare/models/notification.py — new EventType enum value
- [x] T028 [US3] Post rebase summary Slack notification in src/coordinare/services/rebase.py — after run_rebase_round completes, build a summary message listing each branch + outcome; dispatch via notification_service with event_type rebase_round_complete
- [x] T029 [US3] Add last_rebase_round to dashboard snapshot in src/coordinare/dashboard.py — include serialized RebaseRound in the SSE payload; render JS showing per-card rebase status (outcome badge + SHA + timestamp) near the active-performers section
- [x] T030 [US3] Add rebase_round_complete to notification routing in config — ensure the new event type is routed to slack-ops channel by default
- [x] T031 [P] [US3] Write test for Slack rebase summary in tests/unit/services/test_rebase.py — verify single notification dispatched with correct summary for a round with mixed outcomes
- [x] T032 [P] [US3] Write test for dashboard snapshot containing last_rebase_round in tests/unit/test_dashboard.py — verify field present and correctly shaped per contract schema

**Checkpoint**: Full rebase visibility — Slack summary per merge, dashboard per-card status.

---

## Phase 6: Polish & Cross-Cutting Concerns

- [x] T033 Run all quickstart.md scenarios (5 scenarios) against a live or mocked environment
- [x] T034 Validate dashboard-rebase-status.json contract against actual dashboard output in tests/contract/
- [x] T035 Run .venv/bin/pytest tests/ -q — all tests pass
- [x] T036 Run .venv/bin/ruff check src/ tests/ — lint clean

---

## Dependencies & Execution Order

### Phase Dependencies

- **Phase 1 (Setup)**: No dependencies — can start immediately
- **Phase 2 (Foundational)**: Depends on Phase 1 — BLOCKS all user stories
- **Phase 3 (US1)**: Depends on Phase 2 — the MVP (clean rebases)
- **Phase 4 (US2)**: Depends on Phase 2 + Phase 3 (needs the rebase trigger wired up)
- **Phase 5 (US3)**: Depends on Phase 3 (needs rebase rounds firing to have something to notify about)
- **Phase 6 (Polish)**: Depends on all prior phases

### User Story Dependencies

- **US1 (P1)**: Foundational only — MVP standalone
- **US2 (P2)**: Depends on US1 (needs rebase trigger + conflict detection from Phase 2/3)
- **US3 (P3)**: Depends on US1 (needs RebaseRound results to display)

### Parallel Opportunities

- T001 + T002 (setup) can run in parallel
- T010 + T011 + T012 + T013 (foundational tests) can run in parallel
- T031 + T032 (US3 tests) can run in parallel

---

## Implementation Strategy

### MVP First (User Story 1 Only)

1. Complete Phase 1: Setup (T001-T002)
2. Complete Phase 2: Foundational (T003-T013)
3. Complete Phase 3: US1 — Clean rebase trigger (T014-T019)
4. **STOP and VALIDATE**: Test with two real cards on a project board
5. Deploy if ready — clean rebase is the core value

### Incremental Delivery

1. Setup + Foundational → rebase service ready
2. US1 → clean rebases on merge → deploy (MVP!)
3. US2 → performer conflict resolution → deploy
4. US3 → Slack + dashboard visibility → deploy

---

## Notes

- [P] tasks = different files, no dependencies
- [Story] label maps task to specific user story
- Rebase operations use ephemeral temp clones (same pattern as WorkspaceManager)
- Active-session guard (FR-006) is in detect_stale_branches — branches with monitoring_performer phase are skipped
- Force-push uses --force-with-lease for safety (never bare --force)
- Conflict resolution reuses existing dispatch_performer → monitor_performer pipeline
