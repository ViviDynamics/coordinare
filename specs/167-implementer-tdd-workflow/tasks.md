# Tasks: Implementer Test-First Workflow with a Verified Hand-off

**Input**: Design documents from `/specs/167-implementer-tdd-workflow/`
**Prerequisites**: spec.md, plan.md, research.md, data-model.md, quickstart.md, contracts/

**Tests**: Required (Constitution II, TDD). Every gate rule (red_check, green_check, baseline_detection, vacuous_test_check, no_progress_check, scope_revert, commit_squash) gets a test written first with a mutation check shown to fail under a mutation of that rule. State-machine tests run against a real temporary git repository with a bare remote and a scripted fake agent_turn_runner (the 165 review showed fakes hid plumbing bugs). Two test trees run separately: `tests/` and `agent/performer/tests/`.

**Organization**: Tasks grouped by user story. Phases 1 and 2 are sequential prerequisites; US1, US2, US3 are P1; US4 is P2. Phase 2 blocks all; US1 blocks US2 and US3; US4 after US1; US2 and US3 independent.

## Format: `- [ ] T### [P] [US#] Description with exact file path`

## Phase 1: Setup

- [x] T001 Create the package skeleton `agent/performer/src/performer/workflows/implementer/` with `__init__.py`, `models.py`, `personas.py`, `intake.py`, `baseline.py`, `cycle.py`, `quality.py`, `ci.py`, `commits.py`, `gates.py`, `report.py`, and `tests/unit/workflows/implementer/__init__.py`
- [x] T002 [P] Register `"implementer"` in `SUPPORTED_WORKFLOWS` in `agent/performer/src/performer/workflows/__init__.py` and in `KNOWN_WORKFLOWS` in `src/coordinare/config.py`; extend `tests/unit/workflows/test_registry.py` so it accepts `"implementer"` and still rejects an unknown name
- [x] T003 Add budgets to `agent/performer/src/performer/workflows/budget.py`: `_STEP_BUDGETS["turn_milestone"] = 1200000` (20 min, FR-016), `_STEP_BUDGETS["quality"] = 600000` (10 min), `_STEP_BUDGETS["ci_wait"] = 1800000` (30 min, FR-014); write a test in `tests/unit/workflows/test_budget_floors.py` that each budget floor is enforced

## Phase 2: Foundational (blocks all user stories)

- [x] T004 [P] Write failing tests in `tests/unit/workflows/implementer/test_models_bounds.py`: TurnBrief, TurnResult, Baseline, MilestonePlan, PerTurnAttempt, PerMilestoneRecord, QualityAttempt, CIAttempt, RunRecord per data-model.md; one test per bound (e.g. milestone_goal max 256 chars accepted, 257 rejected; forbidden_paths max 10 items, 11 rejected; per_milestone max milestones, etc.); `extra="forbid"` rejects unknown fields; empty milestone_goal rejected; all required fields present Include: Baseline accepts exactly one of `test_names` or `pass_count` (validator), `forbidden_paths` at most 10 items, TurnBrief goal/scope/done_when bounded at 256 characters each, and every persona template placeholder from data-model.md fills without KeyError.
- [x] T005 Implement `agent/performer/src/performer/workflows/implementer/models.py` (pydantic dataclasses or models with `extra="forbid"`, bounds enforced per data-model.md, validated by schema_guard); make T004 pass
- [x] T006 [P] Write failing tests in `tests/unit/workflows/implementer/test_toolkit_run_agent_turn.py`: (a) toolkit.run_agent_turn(brief, fake_runner) completes with TurnResult; (b) fake_runner receives the brief; (c) wall_time_ms is populated; (d) files_changed dict is populated from git diff; (e) exit_state is "success", "timeout", or "failed"; (f) timeout enforcement via asyncio.wait_for kills the turn at wall clock and returns exit_state="timeout"; (g) runner that raises returns exit_state="failed"
- [x] T007 [P] Write failing tests in `tests/unit/workflows/implementer/test_toolkit_events.py`: (a) during a running turn, toolkit forwards inner adapter events and token count to outer WorkflowAdapter.get_status().progress (mirroring 077 stall watchdog); (b) status changes between polls on a 20-minute implementation turn
- [x] T008 [P] Write failing tests in `tests/unit/workflows/implementer/test_commits_git_helpers.py`: (a) turn_start_sha(workspace) returns HEAD SHA at turn start; (b) changed_paths(since_sha, workspace) returns dict[path -> "added"|"modified"|"deleted"] via git status and git diff; (c) squash_turn_commits(to_sha, workspace) runs git reset --soft to_sha, returns old SHAs; (d) revert_paths(paths, workspace) reverts tracked via git checkout --, removes untracked; (e) commit_paths(paths, message, workspace) adds and commits in-scope only; tests with a real repository with two commits pre-squash, one commit post-squash, exact commit message present
- [x] T009 [P] Write failing tests in `tests/unit/workflows/implementer/test_baseline_detection.py`: (a) detect_baseline(toolkit, score) runs test_command once, parses per format (pytest, rspec, jest); (b) pytest --collect-only -q extracts test names and counts; (c) rspec --dry-run extracts counts; (d) jest --listTests extracts names; (e) make fallback (counts only) when per-test parsing fails; (f) broken test file is treated as runner error (workflow stops with env_blocked); (g) test_names or pass_count/fail_count one non-empty
- [x] T010 Implement `agent/performer/src/performer/workflows/implementer/commits.py` with pure-function git helpers: `turn_start_sha(workspace: Path) -> str`, `changed_paths(since_sha, workspace) -> dict[str, str]`, `squash_turn_commits(to_sha, workspace) -> list[str]` (git reset --soft, return old SHAs), `revert_paths(paths, workspace)`, `commit_paths(paths, message, workspace)`; make T008 pass
- [x] T011 Implement `agent/performer/src/performer/workflows/implementer/baseline.py`: `detect_baseline(toolkit, score) -> Baseline`; detect test command via ci_detection.detect(); run it once; parse via stack-specific parsers (lift from main.py lines 2817-2869 or create shared module); record test_names or pass_count/fail_count; make T009 pass
- [x] T012 [P] Write failing tests in `tests/unit/workflows/adapter/test_run_agent_turn_production.py`: (a) build_production_toolkit wires agent_turn_runner from backend adapter per settings.AGENT_BACKEND; (b) backend adapter starts once per turn with modified Score carrying turn persona; (c) per-turn score has persona_instructions changed, all other fields inherited; (d) adapter.get_status() polls until done or wall-clock; (e) files changed computed via git diff against turn-start SHA; (f) adapter.stop() called on completion or timeout; test with a fake backend registered under a fake name
- [x] T013 Implement `agent/performer/src/performer/workflows/adapter.py` added method: `build_production_toolkit(settings, stand, score) -> Toolkit` wiring agent_turn_runner; inject `AgentTurnRunner` callable that instantiates backend, starts with per-turn persona Score, polls until done or timeout, computes files changed, stops adapter; make T012 pass
- [x] T014 Implement `agent/performer/src/performer/workflows/toolkit.py` added method: `Toolkit.run_agent_turn(brief: TurnBrief, wall_clock_ms: int) -> Awaitable[TurnResult]` using injected agent_turn_runner; return TurnResult with exit_state, output_tail, files_changed, wall_time_ms, harness_commits; make T006, T007 pass
- [x] T015 Implement `agent/performer/src/performer/workflows/implementer/intake.py`: `build_milestones(brief: dict | None, score: Score) -> list[MilestonePlan]` per FR-003; brief milestones in order, or one milestone from card acceptance criteria; make parity tests pass
- [x] T016 Write parity tests in `tests/unit/workflows/implementer/test_intake_parity.py`: brief with two milestones returns two MilestonePlans in order; brief marked `implementer_single_turn: true` collapses to one; no brief returns one milestone with card criteria as done_when; empty brief returns one; milestone goal, scope, done_when exact from brief or card

## Phase 3: User Story 1 - A two-milestone card is built test-first, one milestone at a time (P1)

**Goal**: The workflow runs tests turn, red check, implementation turn, green check, commit per milestone, in order. Branch history reads: tests for milestone one, milestone one, tests for milestone two, milestone two.

**Independent Test**: `tests/eval/implementer_scenarios/test_fake_harness.py::test_two_milestones_sequence` with scripted fake harness in a temporary repository with a bare remote: four commits in that order, every turn brief names one milestone, red check saw failures, green check saw none, no turn touched the documentation tree.

### Tests first

- [x] T017 [P] [US1] Write `tests/unit/workflows/implementer/test_personas.py`: TESTS persona forbids source edits and docs; IMPLEMENT forbids tests and docs; REPAIR_TESTS, REPAIR_IMPLEMENT, REPAIR_QUALITY, REPAIR_CI all forbid docs; personas are string templates with <placeholders> filled at runtime; no markdown
- [x] T018 [P] [US1] Write `tests/unit/workflows/implementer/test_baseline_red_check.py` with mutation check: (a) red_check(changed_test_files, results, baseline) passes when at least one changed test fails and all baseline tests still pass; (b) all-pass fails red; (c) regression (baseline fail) fails red; (d) test file identification by convention (**/test_*, **/*_test.*, etc.); mutation-check by removing "at least one" or "all baseline still pass" condition
- [x] T019 [P] [US1] Write `tests/unit/workflows/implementer/test_baseline_green_check.py` with mutation check: (a) green_check(results, milestone_tests, baseline) passes when milestone tests pass and baseline still passes; (b) regression fails green; (c) no milestone tests passes (edge case); mutation-check by removing either "milestone pass" or "baseline still pass"
- [x] T020 [P] [US1] Write `tests/unit/workflows/implementer/test_vacuous_test_detection.py` with mutation check: (a) vacuous_test_check(baseline_all_pass_result) when all baseline still pass but tests should have failed, return true; (b) return false when tests failed as expected; mutation-check by inverting the condition
- [x] T021 [P] [US1] Write `tests/unit/workflows/implementer/test_scope_violations.py` with mutation check: (a) scope_violations(turn_kind="tests", changed_paths, stack) detects source edits and returns list of reverted paths; (b) turn_kind="implement" detects doc edits; (c) no violations returns empty list; mutation-check by removing the forbidden path check
- [x] T022 [P] [US1] Write `tests/unit/workflows/implementer/test_commit_squash_sequence.py`: git reset --soft to_sha -> checkout -- src/ -> rm -r docs/ (if present) -> rm -f untracked -> add in-scope -> commit; test with a real repo where harness made three commits, squash produces one with message "Tests for milestone X"; scope reverts recorded in RunRecord
- [x] T023 [P] [US1] Write `tests/unit/workflows/implementer/test_cycle_red_to_green.py`: (a) cycle runs per-milestone tests turn (red check passes), implementation turn (green check passes), commits both; (b) red check fails -> reprompt once; second red check failure -> fail milestone; (c) three implementation turn failures -> fail milestone with partial progress
- [x] T024 [P] [US1] Write `tests/unit/workflows/implementer/test_implementer_workflow_two_milestones.py` with fake harness: setup two_milestones fixture with bare remote and scripted agent_turn_runner that writes tests and implementation per turn kind; run ImplementerWorkflow.run(); assert four commits in order, red saw failures, green saw none, pr_opened status, run record shows two milestones completed

### Implementation

- [x] T025 [US1] Implement personas in `agent/performer/src/performer/workflows/implementer/personas.py`: TESTS, IMPLEMENT, REPAIR_TESTS, REPAIR_IMPLEMENT, REPAIR_QUALITY, REPAIR_CI as string templates per research R-a; templates forbid documentation and commits; make T017 pass
- [x] T026 [US1] Implement pure gate functions in `agent/performer/src/performer/workflows/implementer/gates.py`: (a) `red_check(changed_test_files, results, baseline) -> bool` (FR-005); (b) `green_check(results, milestone_tests, baseline) -> bool` (FR-006); (c) `vacuous_test_check(baseline_result) -> bool` (FR-005); (d) `scope_violations(turn_kind, changed_paths, stack) -> list[dict]` recording reverted paths (FR-008); make T018, T019, T020, T021 pass
- [x] T027 [US1] Implement `agent/performer/src/performer/workflows/implementer/cycle.py`: `run_milestone(toolkit, score, milestone, baseline, attempt_counter, turn_persona_map) -> tuple[bool, PerMilestoneRecord]` pure-ish orchestration of tests turn, red check, implement turn, green check, commit with cap enforcement and reprompt rule; tests turn fails if all-pass (FR-005 reprompt); implementation capped at 3 attempts; make T023 pass
- [x] T028 [US1] Implement `agent/performer/src/performer/workflows/implementer/__init__.py`: `ImplementerWorkflow.run(performer_response, toolkit, score) -> dict` state machine: intake -> baseline -> cycle per milestone -> quality -> local_gate -> push_and_pr -> ci_wait -> report; log phase durations; return dict with run_record and status; make T024 pass
- [x] T029 [P] [US1] Write `tests/unit/workflows/implementer/test_report_assembly.py`: RunRecord assembles per-milestone history, quality/CI attempts, scope reverts, phase durations, turn counts; structure matches run-record.schema.json; every milestone has goal, done_when, tests_attempt, implement_attempts, implementation_successful, failure_reason
- [x] T030 [US1] Implement `agent/performer/src/performer/workflows/implementer/report.py`: `assemble_run_record(workflow_state) -> RunRecord` carrying milestones, quality attempts, CI attempts, scope reverts, phase durations, terminal status and reason; make T029 pass
- [x] T031 [P] [US1] Write `agent/performer/tests/unit/test_implementer_report_path.py` (mirrors 165 architect path test): when backend output is dict with "implementer_run" key, main.py implementing post-processing skips prose and returns PerformerResponse with status mapping per data-model.md terminal table: `pr_opened` -> state="done"; `partial_progress` -> state="changes_requested"; `env_blocked` -> state="env_blocked"; report carries run record; prose path unchanged when no "implementer_run" key
- [x] T032 [US1] In `agent/performer/src/performer/main.py` implementing stage post-processing: add branch for `workflow_name == "implementer"` that detects "implementer_run" key in report; map terminal outcomes per data-model.md and FR-017; apply existing prose path when workflow flag off (byte-for-byte); make T031 pass
- [x] T033 [P] [US1] Write `tests/unit/workflows/test_dispatch_branch.py` (or extend existing 164/165 parity tests): with no `workflow: implementer` the implementer path is byte-for-byte the prose path (contracts test of dispatch payload; FR-017)
- [x] T034 [US1] Write `tests/eval/implementer_scenarios/fixtures.py` (or conftest.py): set up temporary git repositories with bare remotes, three test fixtures: `single` (one milestone, simple tests+impl, quality+CI pass first time), `two_milestones` (two milestones, same), `vacuous` (tests all-pass without impl); scripted fake agent_turn_runner per fixture (edits files, makes commits, per turn.kind)
- [x] T035 [P] [US1] Write `tests/eval/implementer_scenarios/test_fake_harness.py::test_single_happy_path`: single fixture, full happy path (tests, impl, quality, local gate, PR, CI), expects status="pr_opened", 2 commits (tests + impl), run record shows one milestone completed
- [x] T036 [P] [US1] Write `tests/eval/implementer_scenarios/test_fake_harness.py::test_two_milestones_sequence`: two_milestones fixture, expects status="pr_opened", 4 commits in order (tests M1, impl M1, tests M2, impl M2), run record shows two milestones completed, baseline carry-forward from M1 to M2

## Phase 4: User Story 2 - A vacuous or stuck milestone is bounded, not abandoned (P1)

**Goal**: Reprompt once on vacuous tests; cap implementation at 3 attempts; on milestone failure, revert red tests commit and report partial progress naming the failing milestone.

**Independent Test**: `tests/eval/implementer_scenarios/test_fake_harness.py::test_vacuous_tests_reprompt` and `test_stuck_impl_attempts`: vacuous fixture has one reprompt turn then fails; stuck fixture has 3 impl attempts then fails; both report partial progress with milestone named; branch at last green commit, nothing pushed.

### Tests first

- [x] T037 [P] [US2] Write `tests/unit/workflows/implementer/test_cap_attempt_counters.py` with mutation checks: (a) tests turn + reprompt + 3 implementation turns per milestone (FR-006); (b) second all-pass -> fail milestone (FR-005); (c) third implementation failure -> fail milestone (FR-006); mutation-check by changing the counters or thresholds
- [x] T038 [P] [US2] Write `tests/unit/workflows/implementer/test_milestone_failure_revert.py` with mutation check: when a milestone fails (red never turns green or stuck on attempts), the red tests commit is reverted (git reset --soft to pre-tests SHA), branch is at last green commit, nothing is pushed (FR-009); mutation-check by not reverting or pushing anyway
- [x] T039 [P] [US2] Write `tests/unit/workflows/implementer/test_partial_progress_outcome.py`: when milestone fails, run_record.status="partial_progress", reason names the failing milestone, next_focus_milestone set, milestones_completed < milestones_planned
- [x] T040 [P] [US2] Write `tests/eval/implementer_scenarios/test_fake_harness.py::test_vacuous_tests_reprompt`: vacuous fixture where all tests pass without implementation, expects one reprompt turn then failure; status="partial_progress", run_record shows tests_attempt and tests_reprompt both in PerMilestoneRecord, implementation_successful=false
- [x] T041 [P] [US2] Write `tests/eval/implementer_scenarios/test_fake_harness.py::test_stuck_impl_attempts`: stuck fixture where implementation never makes tests pass (three attempts), expects status="partial_progress", run_record shows 3 implement_attempts all failed, implementation_successful=false, branch at last green commit

### Implementation

- [x] T042 [US2] Update `agent/performer/src/performer/workflows/implementer/cycle.py` with reprompt logic: when red_check sees all-pass, run reprompt turn (REPAIR_TESTS persona); if second red check still all-pass, fail milestone with failure_reason="tests all pass without implementation"; make T037, T040 pass
- [x] T043 [US2] Update `agent/performer/src/performer/workflows/implementer/cycle.py` with attempt caps and milestone failure: on third implementation attempt failure, end cycle with milestone failed, failure_reason naming the milestone; on milestone failure, run_record carries partial_progress with milestone named; make T037, T038, T039, T041 pass
- [x] T044 [US2] In `agent/performer/src/performer/workflows/implementer/__init__.py` state machine: when cycle returns failed milestone, end workflow as partial_progress (do not continue to next milestone or quality); revert red tests commit (git reset --soft pre-tests SHA) before exiting; ensure nothing is pushed (FR-009)

## Phase 5: User Story 3 - Quality, local gate, CI are gates with repair loops, and the hand-off waits for green (P1)

**Goal**: Quality commands run with repair loop (at most 2 repairs); local gate runs unchanged; PR opens after quality; CI polling with repair loop (at most 3 repairs) and no-progress check; only pr_opened when CI green.

**Independent Test**: `tests/eval/implementer_scenarios/test_fake_harness.py::test_single_fixture_with_quality_ci_repairs`: single fixture with fake quality command failing once and fake CI failing once; expects two repair turns (one quality, one CI), final status="pr_opened", run_record shows one quality attempt and one CI attempt.

### Tests first

- [x] T045 [P] [US3] Write `tests/unit/workflows/implementer/test_quality_command_detection.py`: detected lint command from ci_detection.lint_command; QUALITY_COMMANDS from score.workflow_env (newline-separated); first failure triggers repair; quality stops on first failure (FR-010); sequence is lint first, then declared commands in order
- [x] T046 [P] [US3] Write `tests/unit/workflows/implementer/test_quality_repair_loop.py` with mutation check: quality failure -> repair turn with command output in brief.failing_output; re-run milestone tests then full quality set; at most 2 repairs; after 2 repairs fail, end run with status="partial_progress" naming the command (FR-010); mutation-check by changing cap or removing re-test logic
- [x] T047 [P] [US3] Write `tests/unit/workflows/implementer/test_no_progress_detection.py` with mutation check: same set of failing checks on two consecutive CI polls = no progress; end run with partial_progress (FR-013); mutation-check by removing the comparison or changing threshold
- [x] T048 [P] [US3] Write `tests/unit/workflows/implementer/test_ci_repair_loop.py` with mutation check: failing check -> poll get_check_run_logs() for log excerpt -> repair turn with log_excerpt in brief.failing_output; re-run milestone tests, quality, push, poll again; at most 3 repairs; after 3 repairs fail or no-progress detected, end run naming the check (FR-013); mutation-check by changing cap
- [x] T049 [P] [US3] Write `tests/unit/workflows/implementer/test_ci_pending_hold.py`: checks pending past CI_WAIT_MS budget (30 min default, FR-014); workflow ends with status="env_blocked", reason names pending checks, no push, no pr_opened; mutation-check by removing timeout or removing the hold
- [x] T050 [P] [US3] Write `tests/unit/workflows/implementer/test_pr_open_or_update.py`: PR opens without reporting pr_opened status before CI phase (FR-012); PR updates if it already exists (re-dispatch after bounce)
- [x] T051 [P] [US3] Write `tests/eval/implementer_scenarios/test_fake_harness.py::test_single_fixture_with_repairs`: single fixture with quality_command failing once, CI check failing once; expects two repair turns, final status="pr_opened", quality_attempts and ci_attempts in run_record show repair_needed=true then passed=true
- [x] T052 [P] [US3] Write `tests/eval/implementer_scenarios/test_fake_harness.py::test_ci_pending_hold`: ci_pending fixture where CI checks stay pending past budget; expects status="env_blocked", no push, no pr_opened

### Implementation

- [x] T053 [US3] Implement `agent/performer/src/performer/workflows/implementer/quality.py`: `run_quality_phase(toolkit, score, milestone) -> tuple[bool, list[QualityAttempt]]` detecting lint + declared commands, running in order, stopping on first failure, repair loop with at most 2 repairs, re-test milestone + full quality after repair; make T045, T046 pass
- [x] T054 [US3] Implement `agent/performer/src/performer/workflows/implementer/ci.py`: `run_ci_phase(toolkit, score, github, pr_url, milestone) -> tuple[bool, list[CIAttempt]]` polling check_runs, no-progress detection (same failing checks twice), repair loop with at most 3 repairs (re-test milestone, quality, push, poll), CI_WAIT_MS timeout -> env_blocked hold; make T047, T048, T049 pass
- [x] T055 [US3] In `agent/performer/src/performer/workflows/implementer/__init__.py` state machine: after green milestones, run quality phase; on quality failure (2 repairs) end as partial_progress; on quality pass, run local_gate (unchanged, spec-089); on local_gate pass, push via spec-165 path and open PR (without reporting pr_opened); then run CI phase; on CI green report pr_opened (FR-013, FR-014); make T050, T051, T052 pass
- [x] T056 [US3] In `agent/performer/src/performer/workflows/implementer/gates.py`: add `no_progress_check(prior_failing_checks: list[str], current_failing_checks: list[str]) -> bool` pure function (FR-013); make T047 pass
- [x] T057 [US3] Implement repair turns for quality and CI in `agent/performer/src/performer/workflows/implementer/cycle.py`: after any repair (quality or CI), re-run milestone tests first; if regression detected (baseline fails), route back to green check for that milestone (FR-015); make run_milestone re-runnable with regression detection Also write `tests/unit/workflows/implementer/test_repair_regression.py` (FR-015): a quality repair and a CI repair that make the tool pass but break a baseline test route the run back to the green check for that milestone (the next brief names the regressed test) instead of forward; with a mutation check (skip the milestone test re-run and the test fails).

## Phase 6: User Story 4 - A small card gets one cycle and no ceremony (P2)

**Goal**: No brief or brief marked single-turn -> one milestone with card acceptance criteria; at most 5 harness turns plus repairs.

**Independent Test**: `tests/eval/implementer_scenarios/test_fake_harness.py::test_single_no_brief`: single fixture with no brief; expects one milestone, two commits (tests + impl), same gates, pr_opened, turn count under 5.

### Tests first

- [x] T058 [P] [US4] Write `tests/unit/workflows/implementer/test_plan_collapse.py`: (a) no brief -> one milestone with card.acceptance_criteria as done_when; (b) brief with `implementer_single_turn: true` -> collapse multiple milestones into one; (c) brief with `implementer_single_turn: false` -> keep milestones
- [x] T059 [P] [US4] Write `tests/eval/implementer_scenarios/test_fake_harness.py::test_single_no_brief`: single fixture with no brief, expects one milestone, pr_opened status

### Implementation

- [x] T060 [US4] Update `agent/performer/src/performer/workflows/implementer/intake.py`: `build_milestones(brief, score)` collapses to one milestone when no brief or `implementer_single_turn: true` (FR-003); make T058 pass

## Phase 7: Polish & Cross-Cutting Concerns

- [x] T061 [P] Write `tests/unit/workflows/implementer/test_toolkit_metrics.py`: WorkflowMetrics gains `agent_turns` count and `per_phase_durations_ms` dict; baseline, cycle, quality, local_gate, ci phases each record their duration
- [x] T062 [P] Write `tests/unit/workflows/implementer/test_stall_watchdog_forwarding.py` (R-e): run_agent_turn forwards inner harness adapter events and token growth to outer WorkflowAdapter.get_status().progress; a 20-minute implementation turn does not trigger stall timeout when forwarding is present; documentation note: turn wall clock must not exceed stall_timeout_seconds (default 900) unless forwarding is in place (quickstart.md)
- [x] T063 Write `tests/unit/workflows/implementer/test_turn_persona_assembly.py`: per-turn Score carries modified persona_instructions from personas.py per turn.kind, all other fields inherited from caller; persona_instructions has <placeholders> filled at runtime (goal, scope, done_when, failing_output for repairs)
- [x] T064 Add `# workflow: implementer` example to `config.example.yaml` with workflow_env keys (QUALITY_COMMANDS, TURN_WALL_CLOCK_MS, etc.) and defaults from budget.py
- [x] T065 Write "Role workflows: implementer" subsection in `docs/onboarding/04-harnesses-and-shims.md` describing: intake (milestones from brief or card), tests turn (red check must pass), implementation turn (green check must pass, up to 3 attempts), quality gate (detected lint + declared commands, repair loop), local gate (unchanged spec-089), push and PR (spec-165 path, no pr_opened until CI green), CI polling (repair loop, no-progress detection, pending hold), hand-off only on CI green; include stall-timeout note from R-e
- [x] T066 [P] Write `tests/eval/implementer_scenarios/test_log_format.py`: every phase logs its duration and outcome in `run_record.phase_durations_ms` (planning, baseline, per_milestone, quality, local_gate, ci); every turn logs wall_time_ms and files_changed; run_record.turn_count, model_calls, github_api_calls populated
- [x] T067 Implement `agent/performer/src/performer/workflows/implementer/intake.py` logging: log `implementer.intake` event with phase duration, milestones_planned count
- [x] T068 Implement phase duration logging in `agent/performer/src/performer/workflows/implementer/__init__.py`: every phase (intake, baseline, cycle, quality, local_gate, push_and_pr, ci_wait) logs start/end with duration_ms; run_record.phase_durations_ms carries all; log `implementer.run_complete` with terminal status and duration
- [x] T069 Write `tests/eval/implementer_scenarios/README.md` documenting: fixture setup (bare remote, fake harness, scripted commits), CI mode (deterministic, fast), live mode (--live flag, real model, gateway), properties verified (no red push, bounded turns, green CI at hand-off), performance expectations per quickstart.md SC-005
- [x] T070 Implement `src/coordinare/eval/implementer_scenarios.py`: stub runner for CI, --live flag swaps for real adapter; run five eval fixtures (single, two_milestones, vacuous, stuck, ci_pending); exit 0 on all pass, 1 on any fail
- [ ] T071 [P] Run both test trees in worktree with `PYTHONPATH=src:agent/performer/src` (`tests/unit/workflows/implementer/`, `agent/performer/tests/unit/test_implementer_*.py`), `ruff check src tests agent/performer/src`; record mutation checks performed (one per gate rule: red_check, green_check, vacuous_test_check, scope_violations, commit_squash, no_progress_check, quality repair cap, CI repair cap); all US1, US2, US3, US4, Polish tests pass Mutation protocol (Constitution II): for each rule test, apply the named one-line mutation to the real source, run that test file, observe the named test fail, restore, verify with `git diff --stat` that only intended changes remain, and record the table (rule, mutation, failing test) in the PR description.
- [ ] T072 Adversarial review (Workflow: diverse-lens finders plus refute-by-execution over full diff) before opening the PR; disposition every finding (standing rule)
- [ ] T073 Rebuild `coordinare-performer:{base,full,extra}` images from the branch
- [ ] T074 Run one live `single` and one live `two_milestones` fixture with real model via gateway in a performer container (per quickstart.md live eval); record turn durations (tests, impl, quality, CI) against provisioned budgets (FR-016 table); confirm no red push (SC-002), bounded turns (SC-004), green CI at hand-off (SC-001)
- [x] T075 Verify FR-017 (byte-for-byte unchanged prose path): existing implementer tests (without `workflow: implementer`) still pass; implemented as T033 parity test extension

## Dependencies

- **Phase 1 (Setup)**: No dependencies - can start immediately
- **Phase 2 (Foundational)**: Depends on Setup (Phase 1) - BLOCKS all user stories
  - T004/T005 (models) parallel; T006/T007 (toolkit turn) parallel; T008/T009/T010 (commits, baseline) parallel; T012/T013 (adapter wiring) parallel; T014/T015/T016 (intake parity) sequential once T005, T013 done
- **Phase 3 (US1, P1)**: Depends on Foundational (Phase 2) complete
  - T017/T018/T019/T020/T021/T022 (test-first gate rules) parallel; T023 (cycle) depends on T018/T019/T020/T022; T024 (full workflow e2e fake harness) depends on T023, T025 (personas); T025 implementation once T017 tests pass; T026 (gates) once T018/T019/T020/T021 pass; T027 (cycle) once T023 tests pass; T028 (workflow) once T025/T026/T027 done; T029/T030 (report) can start once T005 models; T031/T032/T033/T034 (main.py integration, parity, eval fixtures) depend on T028/T030; T035/T036 (eval fixtures) depend on T034
- **Phase 4 (US2, P1)**: Depends on Phase 3 (US1) complete
  - T037/T038/T039 (attempt caps, failure logic) parallel; T040/T041 (eval fixtures vacuous/stuck) depend on T037/T038/T039; T042/T043/T044 (implementation) once tests pass
- **Phase 5 (US3, P1)**: Depends on Phase 3 (US1) and independent from Phase 4
  - T045/T046/T047/T048/T049/T050 (test-first quality, CI, no-progress, pending) parallel; T051/T052 (eval fixtures) depend on T045/T046/T047/T048/T049; T053/T054/T055/T056/T057 (implementation) once tests pass
- **Phase 6 (US4, P2)**: Depends on Phase 3 (US1) complete
  - T058/T059 (plan collapse tests) parallel; T060 (implementation) once T058 passes
- **Phase 7 (Polish)**: Depends on all user stories (Phases 3, 4, 5, 6) complete
  - T061/T062/T063/T064/T065/T066 parallel; T067/T068 implementation once T061/T066 tests pass; T069 (eval README) once fixtures exist; T070 (eval runner) depends on T034/T035/T036/T040/T041/T051/T052; T071 runs both test trees once all tests written; T072 adversarial review; T073 rebuild images; T074/T075 live validation

## Parallel Examples

- **Phase 2 model/toolkit/commits/baseline/adapter foundation**: T006, T007, T008, T009, T012, T013 all parallel (disjoint files: toolkit.py, commits.py, baseline.py, adapter.py tests)
- **Phase 3 US1 test-first gates**: T017, T018, T019, T020, T021, T022 all parallel (separate test files: personas.py, gates.py, cycle.py, report.py tests)
- **Phase 3 US1 implementation**: T025 personas, T026 gates, T027 cycle all parallel once their tests pass; T028 workflow orchestrates once all three done
- **Phase 4 US2 attempt caps and failure**: T037, T038, T039 all parallel (separate test files); T040, T041 eval fixtures depend on implementation
- **Phase 5 US3 quality and CI gates**: T045, T046, T047, T048, T049, T050 all parallel (separate test files: quality.py, ci.py, no-progress, repair loop, pending, PR open); T051, T052 eval fixtures depend on implementation
- **Phase 7 Polish metrics and logging**: T061, T062, T063, T064, T065, T066 all parallel (separate concerns: metrics, forwarding, persona, config, docs, logs); T067, T068 implementation once tests pass; T070 eval runner once all fixtures done; T071 test trees; T072 review; T073 rebuild; T074 live; T075 parity

## Implementation Strategy

**MVP (Phases 1 to 3 plus Phase 5 hand-off gate)**: Setup + Foundational + US1 (happy path) + US3 (quality and CI gates ensure hand-off is green). This delivers the core loop (test-first per milestone, quality, CI green before hand-off) and validates the turn primitive, adapter wiring, and repair loops. Rollout once US1, US3 fixtures pass live with measured phase durations and no red pushes.

**Phase 4 and 6 added before general release**: US2 (bounded vacuous/stuck) and US4 (single-turn card) handle edge cases and small cards. Both depend on US1 code; can be shipped in the same PR as US1 or deferred to a follow-up.

**Rollout** (after all tests pass in CI and live rounds confirm budgets):
1. Rebuild images (T073)
2. Enable `workflow: implementer` on a non-production symphony (preferably one that already runs `workflow: architect`)
3. Monitor phase durations and turn counts against SC-001, SC-004, SC-005
4. Expand to full fleet

**Notes**:
- Phases 3, 4, 5 can be developed in parallel by separate team members once Phase 2 is done
- Phase 6 (US4) is independently testable and can be done in parallel with Phases 4, 5 or deferred
- Phase 7 Polish runs after all features are implemented
- Every gate rule must have a dedicated mutation test (standing rule, Constitution II); mutation recorded in PR description
- Live runs on `single` and `two_milestones` fixtures are mandatory before enabling (quickstart.md rollout step)
