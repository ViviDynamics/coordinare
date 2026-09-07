# Tasks: Implementer Resume Rule for Milestones a Previous Run Already Did

**Input**: `specs/171-implementer-resume/spec.md`
**Tests**: required (Constitution II). Every rule predicate carries one named mutation applied in the real tree. The two test trees run in separate pytest invocations with `PYTHONPATH=src:agent/performer/src`.

## Phase 1: Foundational (models and the git read)

- [x] T001 `models.py`: `Baseline.test_names_failed: list[str] | None = None` (FR-016); `MilestonePlan.lane_source` gains `"resume"`; `PerMilestoneRecord.satisfied_by: Literal["this_run","prior_run","existing_tests"] = "this_run"`; `RunRecord.resumed_from_milestone: int | None = None`. Bounds test in `tests/unit/workflows/implementer/test_models_bounds.py`
- [x] T002 `baseline.py`: `capture_baseline` records `test_names_failed` from the parsed summary
- [x] T003 `commits.py`: `async branch_commit_entries(workspace, base_candidates) -> list[tuple[str, list[str]]]` reading `git log --format=%x00%s --name-only <ref>..HEAD` for the first ref that resolves via `git rev-parse --verify`, returning `[]` when none does (FR-001). Test in `tests/unit/workflows/implementer/test_commits.py` against a real repository: subjects with their paths, an unresolvable base, a merge-free linear history
- [x] T004 `cycle.py`: extract the test-path convention into `is_test_path(path) -> bool` over the union of the existing pattern sets, and use it from `changed_test_files` and from `scope_violations`' two inline pattern lists. No behaviour change other than the wider suffix set the union brings. Existing `test_test_paths.py` and `test_scope_violations.py` must pass unchanged

## Phase 2: The rules (US1, US2, US3, US4)

- [x] T005 [P] `resume.py`: `prior_run_paths(entries, issue_number) -> frozenset[str]` (FR-002). Tests `tests/unit/workflows/implementer/test_prior_run_paths.py` with the named mutation from the spec table
- [x] T006 [P] `resume.py`: `declared_test_paths(scope)` and `declared_source_paths(scope)` (FR-003), splitting a scope string the way `driver._scope_list` does and partitioning it with `cycle.is_test_path`. Tests `tests/unit/workflows/implementer/test_declared_paths.py` with both named mutations
- [x] T007 [P] `resume.py`: `tests_cover(test_paths, present, passed, failed) -> bool` (FR-004's result-set half), anchoring a reported test name to a file the way `red_check` does. Tests `tests/unit/workflows/implementer/test_tests_cover.py` with the named mutation
- [x] T008 `resume.py`: `resume_state(milestone, prior_paths, present, passed, failed) -> Literal["done","tests_only","open"]` (FR-004, FR-005, FR-006). Tests `tests/unit/workflows/implementer/test_resume_state.py` with the named mutation, plus the fails-closed cases: no declared test path, no declared source path, test present but not from a prior commit, source present but from main
- [x] T009 `resume.py`: `apply_resume(plans, states) -> tuple[list[MilestonePlan], list[MilestonePlan]]` (FR-007, FR-009), skipping a contiguous prefix of `done` and re-laning `tests_only` to `("tests", "resume")`. Tests `tests/unit/workflows/implementer/test_apply_resume.py` with the named mutation, plus the hole-in-the-middle case and the everything-done case

## Phase 3: Wiring (US1, US2, US3)

- [x] T010 `driver.py`: `RunContext.prior_paths: frozenset[str] = frozenset()`; `run_milestone` dispatches on `milestone.lane` falling back to `ctx.lane` (FR-010)
- [x] T011 `driver.py`: `_red_phase` returns a small `RedOutcome(files, summary, changed, satisfied_by)`; on a completed tests turn that changed no test file it judges the milestone with `resume_state` against the live summary and returns `satisfied_by="existing_tests"` on `done` (FR-011). `_feature` short-circuits on a satisfied outcome, growing the baseline and running no implementation turn. A changed test file keeps the red rule verbatim (FR-012)
- [x] T012 `__init__.py`: after the baseline, read the branch entries, compute `prior_paths`, judge every plan with `resume_state` against the baseline names, `apply_resume`, iterate the remaining list while keeping the full `plans` for the lane, `rerun_green` lookup and `milestones_planned`; append a `prior_run` record per skipped milestone (FR-007, FR-008, FR-013, FR-014); emit `implementer.resume` (FR-017)
- [x] T013 `report.py`: carry `resumed_from_milestone` into the assembled record (FR-014)

## Phase 4: Scenarios

- [x] T014 [US1][US2] `tests/unit/workflows/implementer/test_workflow_end_to_end.py`: the two-run real-repo scenario of SC-001. Run one with a harness whose milestone-zero implementation turn also writes `src/m1.py`, asserting `partial_progress` and the foreign test reverted. Run two on the same clone, asserting the first brief is milestone one, its persona is `TESTS`, no `IMPLEMENT` brief for it, the commit subject `test(#7): cover milestone 1`, `satisfied_by` `prior_run` on milestone zero, `resumed_from_milestone` one, and `pr_opened`
- [x] T015 [US1] Same file: every milestone already done, asserting no turn ran and the run still pushed, opened the pull request and reported `pr_opened`
- [x] T016 [US3] Same file: a tests turn that changes nothing over a prior-run test file that passes, asserting `satisfied_by` `existing_tests` and no implementation turn
- [x] T017 [US4] Same file: on a branch carrying this card's commits, a tests turn that writes a passing test for a milestone whose source is absent still fails after the reprompt
- [x] T018 [US1] Same file: a fresh branch whose repository already contains a file at a name the brief's scope uses, asserting nothing is skipped (FR-002 gate) and `resumed_from_milestone` is null

## Phase 4b: Contract and bounds

- [x] T021 `specs/171-implementer-resume/contracts/run-record-additions.md`, applied to `specs/167-implementer-tdd-workflow/contracts/run-record.schema.json`: optional `resumed_from_milestone` and per-milestone `satisfied_by`
- [x] T022 `present_paths` refuses an absolute or traversing scope segment (`_inside_repo`), with its own named mutation in `test_declared_paths.py`

## Phase 5: Verification

- [x] T019 Apply each named mutation in the real tree one at a time; confirm the paired test file fails; revert. 17 mutations across the six predicates plus `present_paths`; all killed
- [x] T020 `PYTHONPATH=src:agent/performer/src .venv/bin/pytest agent/performer/tests` and `PYTHONPATH=src:agent/performer/src .venv/bin/pytest tests` in separate invocations; `.venv/bin/ruff check` over the touched files

## Deviations from the plan above

- The rules live in a new `resume.py`, not in `plan.py`. `build_plan` has no
  baseline, and the `done` predicate needs one, so `build_plan` stays a pure
  function of the card and the brief and the resume decision is its own step
  after the baseline (a new `resume` entry in `phase_durations_ms` and `STATES`).
- T011's second FR-011 call site, inside the reprompt path, was written and then
  removed as unreachable: a reprompt turn can only write test files, so reaching
  it with no changed test file means the first turn's judgement already stood.
- `cycle.scope_violations` now uses the unified `is_test_path`, which widens its
  foreign-test rule to Go and Java test files. That was a latent gap in 167 (two
  copies of the convention had drifted), not a behaviour 171 needed.
