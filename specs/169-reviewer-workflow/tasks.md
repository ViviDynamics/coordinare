# Tasks: Reviewer Workflow with Verified Findings and a Structured Hand-off

**Input**: Design documents from `/specs/169-reviewer-workflow/`
**Prerequisites**: spec.md, plan.md, research.md, data-model.md, quickstart.md, contracts/

**Tests**: Required (Constitution II). Every gate rule (anchor_ok, drop_unanchored, add_unaddressed_feedback, add_documentation_by_implementer, coverage, verdict) gets a test written first with a mutation check shown to fail under a mutation of that rule. State-machine tests run against a real temporary git repository with a branch diff, a scripted stub model, and a fake command runner that serves file contents for surveyed paths. Two test trees run separately: `tests/` (coordinare) and `agent/performer/tests/` (performer).

**Organization**: Tasks grouped by user story and phase. Phase 1 and 2 are sequential prerequisites; US1, US2, US3 are P1; US4 is P2. Phase 2 blocks all; US1 blocks US2/US3; Phase 7 (repair lane) depends on US1; Phase 8 (Polish) runs last.

## Format: `- [ ] T### [P] [US#] Description with exact file path`

## Phase 1: Setup

- [x] T001 Create the package skeleton `agent/performer/src/performer/workflows/reviewer/` with `__init__.py`, `models.py`, `personas.py`, `intake.py`, `diffparse.py`, `findings.py`, `gate.py`, `post.py`, and `report.py`; add test init file `tests/unit/workflows/reviewer/__init__.py`
- [x] T002 [P] Register `"reviewer"` in `SUPPORTED_WORKFLOWS` in `agent/performer/src/performer/workflows/__init__.py` and in `KNOWN_WORKFLOWS` in `src/coordinare/config.py`; extend `tests/unit/workflows/test_registry.py` so it accepts `"reviewer"` and still rejects unknown names
- [x] T003 Add budget to `agent/performer/src/performer/workflows/budget.py`: `_STEP_BUDGETS["findings"] = 8000` (completion tokens per findings call, FR-005); write a test in `tests/unit/workflows/test_budget_floors.py` that the findings budget floor is enforced

## Phase 2: Foundational (blocks all user stories)

- [x] T004 [P] Write failing tests in `tests/unit/workflows/reviewer/test_models_bounds.py`: ChangedFile, Hunk, Finding (with fixed categories: logic_error, test_missing, style, performance, security, documentation_by_implementer, unaddressed_feedback), Disposition, ReviewRecord per data-model.md; one test per bound (e.g. path is non-empty string, line >= 0, problem/why_blocking/evidence max 500 chars, category in fixed set, findings list max 30 items, verdict in {approved, changes_requested, env_blocked}); `extra="forbid"` rejects unknown fields; every persona template placeholder fills without KeyError
- [x] T005 Implement `agent/performer/src/performer/workflows/reviewer/models.py` (pydantic dataclasses or models with `extra="forbid"`, bounds enforced per data-model.md, validated by schema_guard); make T004 pass
- [x] T006 [P] Write failing tests in `tests/unit/workflows/reviewer/test_diffparse_unified.py`: parse unified diff into ChangedFile(path, hunks with new-side line ranges, fully_in_diff boolean); cases: single hunk, multiple hunks, file deletion, file rename, binary skipped, truncation marker detection ("coordinare: omitted X tooling and Y binary files; diff truncated to 60000 chars"); sample diffs with real structure; fully_in_diff is False if diff was truncated; mutation-check by removing truncation detection
- [x] T007 Implement `agent/performer/src/performer/workflows/reviewer/diffparse.py`: unified diff parser with `parse_diff(diff_text: str) -> tuple[list[ChangedFile], bool]` (list of files, truncation flag); detect truncation note from sanitizer; extract file paths and hunks with new-side line ranges; make T006 pass
- [x] T008 [P] Write failing tests in `tests/unit/workflows/reviewer/test_state_store_v19.py`: schema v19 adds `PersistedSession.review_findings: dict[str, Any] | None` with default None; test that v18 snapshots load with review_findings=None; test that review_findings accepts a complete ReviewRecord dict and rejects malformed dicts (corrupt-drop validator like assessment); test round-trip serialization; write a test in `src/coordinare/state_store.py` that CURRENT_SCHEMA_VERSION is bumped from 18 to 19
- [x] T009 [P] Write failing tests in `tests/unit/workflows/reviewer/test_score_review_fields.py`: Score class in `agent/performer/src/performer/models.py` gains two new fields: `review_findings: dict[str, Any] | None = None` (for implementing stage only) and `review_findings: list[str] | None = None` (for reviewing stage); test parity contract in `tests/contract/test_dispatch_payload.py` that the fields appear and have correct types and bounds per dispatch-payload-additions.md
- [x] T010 Implement state_store v19 in `src/coordinare/state_store.py`: bump CURRENT_SCHEMA_VERSION to 19, add `review_findings` field to PersistedSession with default None, add corrupt-drop validator; make T008 pass
- [x] T011 Implement Score fields in `agent/performer/src/performer/models.py`: add `review_findings` fields; make T009 pass
- [x] T012 [P] Write failing tests in `tests/unit/workflows/reviewer/test_adapter_toolkit.py`: reviewer toolkit has `command_runner` but `screenshot_capture=None` and `dom_reader=None` (read-only); test that refusing a command is recorded with reason; test that surveyed files are tracked by command name; budget is enforced per spec-164 patterns
- [x] T013 Implement reviewer toolkit in `agent/performer/src/performer/workflows/adapter.py` (or extend existing): `build_reviewer_toolkit(settings, stand, score) -> Toolkit` with command_runner, no screenshot/dom; make T012 pass

## Phase 3: User Story 1 - A PR with real problems gets anchored, actionable findings (P1)

**Goal**: The workflow parses the diff, lets the model survey code under budget, asks for findings, validates anchors against diff and surveyed files, drops unanchored findings and re-prompts once, posts one GitHub review with inline comments per finding, and hands findings to the implementer as a repair brief.

**Independent Test**: `tests/eval/reviewer_scenarios/test_eval.py::test_findings` with a stubbed model: the posted review has one inline comment per finding on its line; the report's findings all anchor inside the diff hunks or surveyed files; the performer status is changes_requested; a lifted copy reaches an implementer dispatch and selects the repair lane.

### Tests first

- [x] T014 [P] [US1] Write `tests/unit/workflows/reviewer/test_personas.py`: REVIEW persona instructs model to read diff, survey code, identify issues with fixed categories; SURVEY_COVERAGE persona names unread files and asks for one more survey turn; personas are string templates with placeholders (diff, prior_comments, survey_commands, changed_files, etc.) filled at runtime; no markdown in raw persona text
- [x] T015 [P] [US1] Write `tests/unit/workflows/reviewer/test_intake_parse_diff.py`: (a) parse_diff into ChangedFile structures; (b) normalize relay_feedback to list of {id, path, line, body}; (c) handle comments without path/line as {path: "", line: 0}; (d) record truncation marker; (e) carry implementation_brief when present; test real diff samples from spec-165 review payloads
- [x] T016 [P] [US1] Write `tests/unit/workflows/reviewer/test_intake_integration.py`: intake step assembles changed_files, open_comments, truncation flag, implementation_brief, ci_state from Score fields; outputs structure matches models for survey and findings inputs
- [x] T017 [P] [US1] Write `tests/unit/workflows/reviewer/test_survey_coverage_tracking.py`: (a) survey runs only commands the spec-165 allow-list accepts; (b) coverage tracks which changed_files were opened by command names; (c) survey has command budget (12 commands, 4000 chars each per FR-003); (d) refused commands recorded with reason; (e) coverage pass runs survey again naming unread files; test inherit from architect survey logic
- [x] T018 [P] [US1] Write `tests/unit/workflows/reviewer/test_findings_schema_guard.py`: (a) findings call is schema-guarded per spec-164 patterns; (b) max 30 findings enforced; (c) schema rejects verdict field; (d) one reprompt on schema violation; (e) second violation raises malformed_output; test with sample model output including malformed JSON
- [x] T019 [P] [US1] Write `tests/unit/workflows/reviewer/test_gate_anchor_ok.py` with mutation check: anchor_ok(finding, changed_files, hunks_by_path, surveyed_files) returns True if (finding.path in changed_files AND finding.line in any hunk's range) OR finding.path in surveyed_files; mutation by removing either condition
- [x] T020 [P] [US1] Write `tests/unit/workflows/reviewer/test_gate_drop_unanchored.py`: unanchored findings are recorded and reprompted once; second miss is dropped and recorded; dropped findings carry evidence snippet for human review
- [x] T021 [P] [US1] Write `tests/unit/workflows/reviewer/test_gate_unaddressed_feedback.py` with mutation check: for every open comment without a disposition, add an `unaddressed_feedback` finding anchored to comment's path/line; mutation by skipping the check
- [x] T022 [P] [US1] Write `tests/unit/workflows/reviewer/test_gate_documentation_check.py` with mutation check: when implementation_brief is present and diff touches docs/, add a `documentation_by_implementer` finding; mutation by removing the brief check or path check
- [x] T023 [P] [US1] Write `tests/unit/workflows/reviewer/test_gate_coverage.py` with mutation check: (a) coverage_ok(changed_files, covered_files, diff_truncated) returns True only if all changed_files in covered_files AND (NOT diff_truncated OR at least one coverage_pass_ran); (b) approval requires full coverage; mutation by removing any condition
- [x] T024 [P] [US1] Write `tests/unit/workflows/reviewer/test_gate_verdict.py` with mutation check: (a) verdict is changes_requested if any Finding survives; (b) verdict is approved if no findings AND full coverage; (c) verdict is env_blocked if no findings but incomplete coverage; mutation by changing logic
- [x] T025 [P] [US1] Write `tests/unit/workflows/reviewer/test_post_github_review.py`: (a) REQUEST_CHANGES event when findings exist; (b) COMMENT event when no findings and approved; (c) one inline comment per finding at finding.path and finding.line; (d) findings with path="" posted as path="/" line=1; (e) body names fixed dispositions; (f) no threads resolved; test with mock github.post_pull_request_review
- [x] T026 [P] [US1] Write `tests/unit/workflows/reviewer/test_report_assembly.py`: ReviewRecord assembles all workflow state: changed_files, diff_truncated, survey commands/refusals, findings_before_gate, findings_dropped, findings_after_recheck, dispositions, coverage_pass_ran, verdict, covered_files, post_error, posted_review_url; structure matches review-record.schema.json
- [x] T027 [P] [US1] Write `tests/eval/reviewer_scenarios/test_eval.py::test_findings` with stubbed model: (a) PR with three findings, one unanchored; (b) gate drops unanchored, reprompts; (c) model returns re-anchored findings; (d) posted review has two inline comments; (e) report verdict is changes_requested; (f) lifted findings reach implementer dispatch
- [x] T028 [P] [US1] Write `tests/eval/reviewer_scenarios/test_eval.py::test_hallucinated_anchor` with stubbed model: finding points to line outside diff and not opened by survey; gate drops it; reprompt; model re-anchors; final findings all valid

### Implementation

- [x] T029 [US1] Implement personas in `agent/performer/src/performer/workflows/reviewer/personas.py`: REVIEW and SURVEY_COVERAGE as string templates per research R-a; fill placeholders at runtime; make T014 pass
- [x] T030 [US1] Implement `agent/performer/src/performer/workflows/reviewer/intake.py`: parse diff, normalize relay_feedback, extract truncation marker, carry implementation_brief; make T015, T016 pass
- [x] T031 [US1] Implement `agent/performer/src/performer/workflows/reviewer/survey.py` (or extend existing spec-164 logic): run_survey_step (inherit from architect); track covered_files; handle command budget and refusals; record commands; make T017 pass
- [x] T032 [US1] Implement `agent/performer/src/performer/workflows/reviewer/findings.py`: one schema-guarded findings call, max 30 findings, rejects verdict field, one reprompt on violation; make T018 pass
- [x] T033 [US1] Implement pure gate functions in `agent/performer/src/performer/workflows/reviewer/gate.py`: (a) `anchor_ok(finding, changed_files, hunks_by_path, surveyed_files) -> bool` (FR-006); (b) `drop_unanchored(findings) -> tuple[list, list]` with reprompt logic; (c) `add_unaddressed_feedback(dispositions, open_comments) -> list[Finding]` (FR-007); (d) `add_documentation_check(diff, implementation_brief) -> Finding | None` (FR-008); (e) `coverage(changed_files, covered_files, diff_truncated) -> bool` (FR-004, FR-009); (f) `verdict(findings, coverage_ok, diff_available) -> str` (FR-009); make T019 through T024 pass
- [x] T034 [US1] Implement `agent/performer/src/performer/workflows/reviewer/post.py`: post_pull_request_review via github API; map verdict to event (REQUEST_CHANGES or COMMENT); inline comments per finding; handle path="" as fallback; name fixed dispositions in body; make T025 pass
- [x] T035 [US1] Implement `agent/performer/src/performer/workflows/reviewer/report.py`: assemble_review_record combining all state; make T026 pass
- [x] T036 [US1] Implement `agent/performer/src/performer/workflows/reviewer/__init__.py`: ReviewerWorkflow.run state machine: intake -> survey -> findings -> gate -> post -> report, all states advanced by code (not model); return dict with review_record and status; log durations per FR-016; make T027, T028 pass
- [x] T037 [US1] Implement post-processing in `agent/performer/src/performer/main.py` reviewing stage: when `workflow_name == "reviewer"`, detect "review_findings" key in report; map verdict to status: changes_requested, approved, or env_blocked (FR-011); return PerformerResponse with lifted findings; byte-for-byte prose path when workflow flag off (FR-015); write test `agent/performer/tests/unit/test_reviewer_report_path.py` mirroring spec-165 architect test
- [x] T038 [US1] In `src/coordinare/graph/nodes/monitor_performer.py`: lift report["review_findings"] into state["review_findings"] when reviewer reports changes_requested; write test in `tests/unit/graph/nodes/test_monitor_performer_lift_findings.py`
- [x] T039 [US1] In `src/coordinare/graph/nodes/dispatch_performer.py`: implement `reset_review_findings_for_reviewer` function; call on reviewer dispatch to clear prior findings (FR-012); implement `inject_review_findings` function to inject review_findings into implementing-stage Score only (FR-012); write tests in `tests/unit/graph/nodes/test_dispatch_performer_reviewer_functions.py`
- [x] T040 [US1] Write parity test in `tests/contract/test_dispatch_payload.py`: without `workflow: reviewer` the dispatch payload is byte-for-byte unchanged (FR-015)

## Phase 4: User Story 2 - A clean PR is approved only after the reviewer has read it all (P1)

**Goal**: When diff is truncated or files remain unread after first survey, run exactly one more coverage pass. Approval requires full coverage. Missing coverage is an environment hold naming unread files. A large PR with coverage but no findings is approved.

**Independent Test**: `tests/eval/reviewer_scenarios/test_eval.py::test_clean` (no findings, full coverage, approved). `tests/eval/reviewer_scenarios/test_eval.py::test_truncated` with coverage pass that opens cut files (approval or changes_requested based on findings).

### Tests first

- [x] T041 [P] [US2] Write `tests/unit/workflows/reviewer/test_coverage_pass_trigger.py`: (a) coverage_pass_needed when diff_truncated OR any changed_file not in covered_files; (b) when needed, run exactly one more survey turn with SURVEY_COVERAGE persona (FR-004); (c) track covered_files from second survey; test the trigger logic as pure function per Constitution II
- [x] T042 [P] [US2] Write `tests/unit/workflows/reviewer/test_verdict_with_coverage.py` with mutation checks: (a) approval requires no findings AND full coverage; (b) changes_requested when findings exist (regardless of coverage); (c) env_blocked when no findings but incomplete coverage (FR-009); mutations: remove coverage check, remove findings check, invert coverage requirement
- [x] T043 [P] [US2] Write `tests/eval/reviewer_scenarios/test_eval.py::test_clean`: small PR, diff complete, no findings, full coverage; verdict approved; COMMENT review posted; no inline comments
- [x] T044 [P] [US2] Write `tests/eval/reviewer_scenarios/test_eval.py::test_truncated`: large PR, injected diff truncated, first survey reads only partial files, coverage pass asks for remaining files, model opens them and approves; or with findings, changes_requested instead

### Implementation

- [x] T045 [US2] Update `agent/performer/src/performer/workflows/reviewer/__init__.py` state machine: after survey, check if coverage_pass_needed; if yes, run survey again with SURVEY_COVERAGE persona, merge covered_files; make T041, T042, T043, T044 pass
- [x] T046 [US2] Update `agent/performer/src/performer/workflows/reviewer/gate.py`: coverage function checks both changed_files presence and truncation status per FR-009; write mutation test in `tests/unit/workflows/reviewer/test_gate_coverage_mutation.py` that shows the check fails when either condition is removed

## Phase 5: User Story 3 - Earlier feedback is never lost (P1)

**Goal**: Model must disposition every open comment from earlier rounds. Any missing disposition becomes an unaddressed_feedback finding by rule. Fixed dispositions are named in review body for human thread resolution.

**Independent Test**: `tests/eval/reviewer_scenarios/test_eval.py::test_prior_feedback` with two open comments, model dispositions one, other becomes finding by code.

### Tests first

- [x] T047 [P] [US3] Write `tests/unit/workflows/reviewer/test_intake_prior_comments.py`: open comments from relay_feedback normalized to {id, path, line, body} with empty path/line for comments without them; test real relay_feedback payloads
- [x] T048 [P] [US3] Write `tests/unit/workflows/reviewer/test_findings_dispositions.py`: findings call returns Disposition list pairing prior comment ids with status (fixed or not_fixed) and optional Finding when not_fixed; schema enforces one disposition per comment id or error
- [x] T049 [P] [US3] Write `tests/unit/workflows/reviewer/test_gate_missing_disposition.py` with mutation check: every prior comment without a disposition in the model's response becomes an unaddressed_feedback finding anchored to comment path/line; mutation: skip the missing disposition check
- [x] T050 [P] [US3] Write `tests/unit/workflows/reviewer/test_post_disposition_names.py`: review body text names all fixed dispositions ("Fixed feedback on [comment IDs]") for human resolution; test that body is present in posted review
- [x] T051 [P] [US3] Write `tests/eval/reviewer_scenarios/test_eval.py::test_prior_feedback`: two open prior comments, model returns disposition for one (fixed), omits other; gate adds missing one as unaddressed_feedback finding; posted review names fixed comment; verdict changes_requested due to finding

### Implementation

- [x] T052 [US3] Update `agent/performer/src/performer/workflows/reviewer/intake.py`: carry open_comments list with normalized structure; pass to findings call
- [x] T053 [US3] Update `agent/performer/src/performer/workflows/reviewer/findings.py`: findings call schema includes Disposition list; parse model dispositions; make T048 pass
- [x] T054 [US3] Update `agent/performer/src/performer/workflows/reviewer/gate.py`: call `add_unaddressed_feedback(dispositions, open_comments)` to add missing dispositions as findings; make T049 pass
- [x] T055 [US3] Update `agent/performer/src/performer/workflows/reviewer/post.py`: body text names fixed dispositions for thread resolution; make T050, T051 pass

## Phase 6: User Story 4 - What the implementer was told not to do is checked by code (P2)

**Goal**: When dispatch carries a spec-165 implementation brief, the brief tells implementer to write code and tests only. If diff touches documentation tree, add a documentation_by_implementer finding without asking model. This is a pure-function gate rule.

**Independent Test**: `tests/eval/reviewer_scenarios/test_eval.py::test_findings` with brief and diff touching `docs/`: finding is present even when model returned none.

### Tests first

- [x] T056 [P] [US4] Write `tests/unit/workflows/reviewer/test_gate_documentation_by_implementer.py` with mutation check: when implementation_brief present and diff has any path under `docs/`, add Finding with category=documentation_by_implementer anchored to first docs/ file; mutation: remove either condition

### Implementation

- [x] T057 [US4] Update `agent/performer/src/performer/workflows/reviewer/gate.py`: call `add_documentation_check(diff_paths, implementation_brief)` to add documentation_by_implementer finding when both conditions met; make T056 pass

## Phase 7: Repair Lane in Spec-167 Implementer Workflow

**Goal**: When implementer dispatch carries review_findings, select a repair lane that groups findings by file and runs one implementation turn per file group. Milestones carry the findings verbatim. After repairs, quality and CI gates run as normal.

### Tests first

- [x] T058 [P] [P7] Write `tests/unit/workflows/implementer/test_plan_select_lane_repair.py` with mutation check: (a) when review_findings present, select_lane returns "repair"; (b) milestones are built grouping findings by path; (c) one MilestonePlan per unique path; (d) milestone goal is "address review findings in <path>"; (e) milestone.brief carries all findings for that path verbatim; mutation: remove review_findings check or skip grouping
- [x] T059 [P] [P7] Write `tests/unit/workflows/implementer/test_repair_personas.py`: REPAIR_REVIEW persona lists findings for a file and asks implementer to address them; template has <findings_for_file>, <file_path> placeholders filled at runtime
- [x] T060 [P] [P7] Write `tests/eval/implementer_scenarios/test_eval.py::test_repair_lane` (or extend existing fixture): review_findings injected with two findings in one file and one in another; implementer workflow selects repair lane; two milestones created (one per unique path); each milestone runs one CHANGE-style turn with findings in brief; status pr_opened after quality and CI pass

### Implementation

- [x] T061 [P7] Update `agent/performer/src/performer/workflows/implementer/models.py`: Lane enum or set gains "repair" value
- [x] T062 [P7] Implement REPAIR_REVIEW persona in `agent/performer/src/performer/workflows/implementer/personas.py`: template with findings list and file path
- [x] T063 [P7] Update `agent/performer/src/performer/workflows/implementer/plan.py`: `select_lane()` function checks for review_findings; if present, return "repair" and build milestones by file path; one MilestonePlan per unique path with goal and brief carrying findings; make T058, T059, T060 pass
- [x] T064 [P7] Write `tests/eval/implementer_scenarios/test_repair_lane_end_to_end.py`: real temp repo, review findings injected, implementer workflow runs repair lane, milestones grouped by file, all quality and CI gates run, pr_opened on green

## Phase 8: Polish & Cross-Cutting Concerns

- [x] T065 [P] Write `tests/unit/workflows/reviewer/test_review_record_schema.py`: ReviewRecord matches review-record.schema.json; write a schema parity test like spec-165 does for blueprints
- [x] T066 [P] Write `tests/unit/workflows/reviewer/test_metrics_logging.py`: every phase (intake, survey, findings, gate, post, report) logs start/end with duration_ms; findings call logs completion tokens; survey logs commands executed
- [x] T067 Write `tests/unit/workflows/reviewer/test_turn_persona_assembly.py`: per-call Score carries modified persona_instructions from personas.py, all other fields inherited; placeholders (diff, prior_comments, changed_files, coverage_status, etc.) filled at runtime
- [x] T068 Add `# workflow: reviewer` example to `config.example.yaml` with role configuration and defaults; document that findings are lifted and injected into implementer only
- [x] T069 Write "Role workflows: reviewer" subsection in `docs/onboarding/04-harnesses-and-shims.md` describing: intake (diff parsing, prior comments), survey (read-only, budget-bounded), findings (schema-guarded, 30-item limit), gate (anchor validation, coverage, dispositions, verdict by code), post (GitHub review with inline comments), report (findings lifted for repair); include SC-001 through SC-006 success criteria; note that without workflow flag behavior is byte-for-byte unchanged
- [x] T070 [P] Write `tests/eval/reviewer_scenarios/fixtures.py` (or conftest.py): set up temporary git repositories with branch checkouts, diffs, prior comments. Five fixtures: `clean` (no issues), `findings` (real issues), `hallucinated_anchor` (bad anchor), `prior_feedback` (unaddressed comment), `truncated` (large diff cut off); scripted stub model per fixture returns json findings, dispositions, per spec contracts
- [x] T071 [P] Write `tests/eval/reviewer_scenarios/fakes.py`: FakeLog test per spec 165 style; stub reviewer backend that returns deterministic findings per fixture; allow injection of findings for mutation testing
- [x] T072 [P] Write `tests/eval/reviewer_scenarios/test_eval.py`: test functions for all five fixtures (test_clean, test_findings, test_hallucinated_anchor, test_prior_feedback, test_truncated); pass mode (CI with stubbed model) and live mode (--live flag, real gateway); assertions on findings, verdict, posted review event, inline comments, lifted state
- [x] T073 Implement `src/coordinare/eval/reviewer_scenarios.py`: stub runner for CI, --live flag swaps for real adapter; run five eval fixtures; exit 0 on all pass, 1 on any fail; emit JSON report with execution times, coverage, findings per fixture
- [x] T074 Write `tests/eval/reviewer_scenarios/README.md` documenting: fixture setup (temp repo, branch, diff, prior comments), CI mode (deterministic, stubbed model, fast), live mode (--live flag, real gateway), properties verified per spec (all findings anchored, no approval without coverage, dispositions honored, post result recorded), performance expectations per quickstart.md SC-004
- [x] T075 Write `tests/unit/graph/nodes/test_reset_review_findings.py`: reset_review_findings_for_reviewer clears prior findings on reviewer dispatch; review_findings field set to None; test with a session snapshot that has findings
- [x] T076 [P] Run both test trees in worktree with `PYTHONPATH=src:agent/performer/src` (`tests/unit/workflows/reviewer/`, `agent/performer/tests/unit/test_reviewer_*.py`, `tests/unit/graph/nodes/test_*.py`); record mutation checks performed (one per gate rule: anchor_ok, drop_unanchored, add_unaddressed_feedback, add_documentation_check, coverage, verdict); all US1, US2, US3, US4, P7, Polish tests pass. Mutation protocol (Constitution II): for each rule test, apply the named one-line mutation to the real source, run that test file, observe the named test fail, restore, verify with `git diff --stat` that only intended changes remain, and record the table (rule, mutation, failing test) in PR description
- [x] T077 Adversarial review (Workflow: diverse-lens finders plus refute-by-execution over full diff) before opening PR; disposition every finding (standing rule)
- [x] T078 Rebuild `coordinare-performer:{base,full,extra}` images from branch
- [x] T079 Run live eval: `test_clean` and `test_findings` fixtures with real gateway model via performer container (per quickstart.md live eval); record round durations (intake, survey, findings, gate, post, report) against provisional budgets (FR-016 table); confirm SC-001 through SC-006 met on live rounds
- [x] T080 [P] Run test for byte-for-byte unchanged path: `tests/contract/test_dispatch_payload.py` (with no `workflow: reviewer` flag); existing reviewer tests still pass unchanged
- [x] T081 Verify FR-001 through FR-018: every requirement tested and demonstrated in at least one fixture or unit test; doc compliance table in research.md or quickstart.md

---

- [x] T082 [P] Write `tests/unit/workflows/reviewer/test_report_write_free.py` and implement the structural write-free record in `agent/performer/src/performer/workflows/reviewer/report.py`: the executed `git status --porcelain` result and the allow-list refusals, as spec 165's architect report does (FR-014), with a mutation check (drop the executed check and the test fails)
- [x] T083 [P] Write `tests/contract/test_review_findings_payload.py`: `review_findings` is declared on Score, present in an implementing dispatch that follows a changes-requested review, ABSENT from a reviewing dispatch and every other stage, and a lifted `review` report validates against `specs/169-reviewer-workflow/contracts/review-record.schema.json` (FR-012, G2 of the analysis)

## Dependencies

- **Phase 1 (Setup)**: No dependencies; can start immediately
- **Phase 2 (Foundational)**: Depends on Setup (Phase 1) complete - BLOCKS all user stories
  - T004/T005 (models) and T006/T007 (diffparse) parallel; T008/T009/T010/T011 (state_store, Score) parallel; T012/T013 (adapter) parallel once T005/T011 done
- **Phase 3 (US1, P1)**: Depends on Foundational (Phase 2) complete
  - T014/T015/T016 (intake tests) parallel; T017/T018 (survey, findings tests) parallel; T019/T020/T021/T022/T023/T024 (gate rule tests) parallel; T025/T026/T027/T028 (implementation) depend on tests for that component; T029/T030/T031/T032/T033/T034/T035/T036 (impl main) depend on tests; T037/T038/T039/T040 (coordinare integration) depend on T036 implementation
- **Phase 4 (US2, P1)**: Depends on Phase 3 (US1) complete
  - T041/T042 (coverage tests) parallel; T043/T044 (eval fixtures) depend on T041/T042; T045/T046 (implementation) once tests pass
- **Phase 5 (US3, P1)**: Depends on Phase 3 (US1) complete; independent from Phase 4
  - T047/T048/T049/T050 (prior feedback tests) parallel; T051 (eval fixture) depends on T047/T048/T049; T052/T053/T054/T055 (implementation) once tests pass
- **Phase 6 (US4, P2)**: Depends on Phase 3 (US1) complete; independent from Phases 4/5
  - T056 (test) parallel; T057 (implementation) once T056 passes
- **Phase 7 (Repair lane, P7)**: Depends on Phase 3 (US1) complete and spec-167 implementer workflow exists
  - T058/T059/T060 (tests) parallel; T061/T062/T063/T064 (implementation in 167) once tests pass
- **Phase 8 (Polish)**: Depends on all user stories (Phases 3, 4, 5, 6, 7) complete
  - T065/T066/T067/T068/T069/T070/T071 parallel; T072/T073/T074 depend on T070/T071; T075 depends on T038/T039; T076 runs all tests; T077 review; T078 rebuild; T079 live; T080/T081 validation

## Parallel Examples

- **Phase 2 foundational foundation**: T004, T005 (models); T006, T007 (diffparse); T008, T009, T010, T011 (state_store, Score); T012, T013 (adapter) all disjoint files
- **Phase 3 US1 test-first gates**: T014, T015, T016 (intake); T017, T018 (survey, findings); T019, T020, T021, T022, T023, T024 (gate rules, findings scenarios) all separate test files
- **Phase 3 US1 implementation**: T029 (personas), T030 (intake), T031 (survey), T032 (findings), T033 (gates), T034 (post), T035 (report) all parallel once their tests pass; T036 (workflow orchestration) once all done
- **Phase 4 US2 coverage**: T041, T042 (tests) parallel; T043, T044 (fixtures) depend on tests
- **Phase 5 US3 prior feedback**: T047, T048, T049, T050 (tests) parallel; T051 (fixture) once tests pass; T052, T053, T054, T055 (implementation) parallel
- **Phase 7 repair lane**: T058, T059, T060 (tests) parallel; T061, T062, T063, T064 (implementer changes) once tests pass
- **Phase 8 Polish logging, config, eval**: T065, T066, T067, T068, T069, T070, T071 all parallel (separate concerns: schema, logging, personas, config, docs, eval fixtures); T072, T073, T074, T075, T076 depend on T070/T071

---

## Implementation Strategy

**MVP (Phases 1 to 3 plus US2 coverage)**: Setup + Foundational + US1 (findings anchored and actionable) + US2 (approval requires coverage). This delivers the core loop (parse diff, survey, findings schema-guarded, gate validates anchors and coverage, post review with inline comments) and validates the toolkit, state lift, and repair lane integration point. Rollout once US1 and US2 fixtures pass live with measured round durations under SC-004 (10m p90) and no findings fail anchor checks (SC-001).

**US3 and US4 added before general release**: US3 (prior feedback dispositions) and US4 (documentation check) handle round-trip reviews and implementer brief constraints. Both depend on US1 code; can be shipped in the same PR as US1 or deferred to a follow-up.

**Phase 7 repair lane added once implementer (spec-167) stabilizes**: Integrates review findings into the implementer workflow. Can be delivered as a separate spec-167 follow-up once US1+US2 are live and the implementer workflow is proven.

**Rollout** (after all tests pass in CI and live rounds confirm budgets):
1. Rebuild images (T078)
2. Enable `workflow: reviewer` on a non-production symphony (preferably one already running spec-164/165/166 workflows)
3. Monitor round durations and anchor validity against SC-001, SC-004
4. Expand to full fleet

**Notes**:
- Phases 3, 4, 5, 6 can be developed in parallel by separate team members once Phase 2 is done
- Phase 7 (repair lane) is independently testable and can run in parallel with other phases or deferred
- Phase 8 Polish runs after all features are implemented
- Every gate rule must have a dedicated mutation test (standing rule, Constitution II); mutation recorded in PR description
- Live runs on `clean` and `findings` fixtures are mandatory before enabling (quickstart.md rollout step)

## Implementation notes (what landed where)

The tests were consolidated into fewer files than the task list names; every task's substance is present:

- Models, diff parser: `tests/unit/workflows/reviewer/test_models_bounds.py`, `test_diffparse_unified.py` (T004 to T007). `Finding.category` is a bounded string; the model-facing schema (`model_findings_schema`) pins it to the configured set so the security role can pass its own (FR-018, T018).
- State, payload, contract: `tests/unit/test_state_store_schema19.py` (T008, T010), `tests/contract/test_review_findings_payload.py` (T009, T011, T083), `tests/unit/graph/nodes/test_review_findings_lift.py` (T038), `tests/unit/graph/nodes/test_review_findings_dispatch.py` (T039, T075), `tests/unit/workflows/test_adapter_reviewer_toolkit.py` (T012, T013).
- Steps: `tests/unit/workflows/reviewer/test_intake.py` (T015, T016, T047), `test_survey_coverage.py` (T017, T041), `test_gate_rules.py` (T019 to T024, T042, T049, T056, T046, T054, T057: every rule with the mutation named in its comment; 14 mutations killed in the real tree, hash-checked restore), `test_post.py` (T025, T050, T055), `test_workflow_end_to_end.py` (T026, T036, T045, T066, T082 and the T014 persona placeholders), `agent/performer/tests/unit/test_reviewer_report_path.py` (T037, T040, T080: prose path unchanged without the report key).
- Eval: `tests/eval/reviewer_scenarios/` fixtures, stub model and runner, recording poster (T027, T028, T043, T044, T051, T070 to T074), `src/coordinare/eval/reviewer_scenarios.py` registered in `tests/eval/test_scenario_runners.py`.
- Repair lane: `tests/unit/workflows/implementer/test_repair_lane.py` (T058 to T064) with the real-repo end-to-end scenario; the quality set runs after every repair turn (FR-013).
- T067 does not apply: the reviewer runs no harness turns, so there is no per-turn Score persona to assemble.

## Requirement coverage (T081)

| FR | where it is proven |
| --- | --- |
| FR-001 order by code | `test_workflow_end_to_end.py::test_clean_diff_is_approved_with_one_comment_review` (step events in order) |
| FR-002 intake | `test_intake.py`, `test_diffparse_unified.py` |
| FR-003 survey allow-list, records, opened files | `test_survey_coverage.py` (refusals recorded and never run; `command_names_path`) |
| FR-004 one coverage pass | `test_survey_coverage.py::test_a_truncated_diff_runs_exactly_one_coverage_pass_naming_the_unread_file`, e2e `truncated` |
| FR-005 one schema-guarded call, 30 cap, no verdict key | `test_models_bounds.py::test_category_in_set`, `test_gate_rules.py::test_findings_are_capped_at_thirty` |
| FR-006 drop and re-anchor once | `test_gate_rules.py` anchor tests, e2e `hallucinated_anchor` (3 model calls) |
| FR-007 unaddressed feedback | `test_gate_rules.py::test_every_prior_comment_without_a_disposition_becomes_a_rule_finding`, e2e `prior_feedback` |
| FR-008 documentation by implementer | `test_gate_rules.py::test_documentation_finding_needs_the_brief_and_a_documentation_path`, e2e `docs_by_implementer` |
| FR-009 verdict and coverage | `test_gate_rules.py::test_verdict_is_derived_by_code`, `::test_full_coverage_needs_every_file_read_and_the_pass_after_truncation`, e2e `unread_files_after_the_coverage_pass_hold` |
| FR-010 one review, events, fixed named, no resolve | `test_post.py`, `test_reviewer_report_path.py::test_approved_verdict_reports_approved_without_a_second_post_or_thread_resolution` |
| FR-011 statuses, failed post is a hold | `test_reviewer_report_path.py`, e2e `a_failed_post_is_an_environment_hold` |
| FR-012 lift, clear, inject, contract | `test_review_findings_lift.py`, `test_review_findings_dispatch.py`, `test_state_store_schema19.py`, `test_review_findings_payload.py`, `test_dispatch_payload.py::test_review_findings_are_not_dropped` |
| FR-013 repair lane | `tests/unit/workflows/implementer/test_repair_lane.py` |
| FR-014 write-free | e2e `a_dirty_tree_fails_the_round`, `report.build_report` |
| FR-015 prose path unchanged | `test_reviewer_report_path.py::test_output_without_the_report_key_takes_the_prose_path`, `test_dispatch_branch.py` |
| FR-016 timing and events | e2e `every_step_is_timed_and_logged` |
| FR-017 pure rules, mutation | `test_gate_rules.py`; 14 mutations killed (PR #270 table) |
| FR-018 categories as a parameter | e2e `the_categories_are_a_workflow_parameter`, `test_category_in_set` |

Live rounds (T079): `clean` approved in 100s, `findings` changes_requested in 179s with three anchored findings, both in `coordinare-performer:full` rebuilt from the branch (T078) through the LiteLLM gateway; see PR #270.
