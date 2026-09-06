# Tasks: Architect Role Workflow with a Blueprint Hand-off

**Input**: Design documents from `/specs/165-architect-blueprint-workflow/`
**Prerequisites**: plan.md, research.md, data-model.md, contracts/blueprint.schema.json, contracts/dispatch-payload-additions.md, quickstart.md

**Tests**: required. Every rule (allow-list, size thresholds, slice disjointness, push rebase, no-force) gets a test written first and mutation-checked once per instance of the rule (standing rule: mutate in the worktree with `PYTHONPATH=src:agent/performer/src`, assert the mutation applied, restore, run the suite after restoring). The two test trees run separately: `tests/` and `agent/performer/tests/`.

**Organization**: tasks grouped by user story. US1 (large card) and US2 (small card) are both P1 and share the workflow; US2's tasks are the sizing branch. US3 is the documenter side run; US4 is QA consumption.

## Format: `[ID] [P?] [Story] Description`

## Phase 1: Setup

- [x] T001 Create the package skeleton `agent/performer/src/performer/workflows/architect/` with `__init__.py`, and `tests/unit/workflows/architect/__init__.py`
- [x] T002 [P] Register `"architect"` in `SUPPORTED_WORKFLOWS` in `agent/performer/src/performer/workflows/__init__.py` and in `KNOWN_WORKFLOWS` in `src/coordinare/config.py`; extend `tests/unit/workflows/test_registry.py` and `tests/unit/test_config_workflow_field.py` so both accept `architect` and still reject an unknown name
- [x] T003 [P] Copy `specs/165-architect-blueprint-workflow/contracts/blueprint.schema.json` into a test fixture path `tests/unit/workflows/architect/fixtures/blueprint.schema.json` and add a test that the pydantic models' rendered schema (T010) matches it field for field

## Phase 2: Foundational (blocks all user stories)

- [x] T004 Write failing tests in `agent/performer/tests/unit/test_push_branch_rebase.py`: (a) when the remote branch exists and has diverged, `push_branch` runs `git fetch origin <branch>` then `git rebase origin/<branch>` and pushes WITHOUT `--force`; (b) when the remote branch does not exist, it pushes and may use `--force` only after the plain push fails; (c) a rebase conflict raises `WorkspaceSetupError` with `rebase_conflict` in the message and no push is attempted; (d) `git push --force` is never issued after a non-fast-forward rejection. Use a fake `_run_git` recording the command sequence
- [x] T005 Implement the rebase-before-push in `push_branch` in `agent/performer/src/performer/workspace.py` per research R3, logging `push_branch.rebased` and `push_branch.rebase_conflict`; keep `strip_agent_artifacts` first; make T004 pass; mutation-check by re-enabling the force fallback on divergence
- [x] T006 Write failing tests in `tests/unit/test_state_store_schema17.py`: `PersistedSession.blueprint` and `.documenting_side` default to `None`, round-trip through save and load, a v16 snapshot loads with both `None`, and `CURRENT_SCHEMA_VERSION == 17`
- [x] T007 Add `blueprint: dict | None` and `documenting_side: DocumentingSideRun | None` (pydantic model per data-model.md) to `PersistedSession` in `src/coordinare/state_store.py`; bump `CURRENT_SCHEMA_VERSION` to 17 with the header comment convention; make T006 pass
- [x] T008 Write failing contract tests in `tests/contract/test_dispatch_payload.py` for `implementation_brief`, `documentation_brief`, `verification_brief`, `implementer_single_turn` (present when produced, absent otherwise, survive `Score` validation) and add the four rows to `specs/contracts/dispatch-payload.md` per `contracts/dispatch-payload-additions.md`
- [x] T009 Declare the four fields on `Score` in `agent/performer/src/performer/models.py` (dicts default to empty, bool default False) so `extra="ignore"` cannot drop them; make T008 pass

## Phase 3: User Story 1 - A large card gets a bounded plan and three tailored briefs (P1)

**Goal**: the architect workflow runs intake, survey, blueprint, size, report inside the performer, commits nothing, and coordinare slices the blueprint to the three readers.

**Independent test**: `tests/unit/workflows/architect/test_workflow_end_to_end.py` with a stub model answering survey and blueprint prompts against the `schema` fixture: no writes, budget respected, valid blueprint, size large, three disjoint briefs after dispatch.

### Tests first

- [x] T010 [P] [US1] Write `tests/unit/workflows/architect/test_models.py`: every list and string bound from data-model.md is enforced (one test per bound at the boundary, e.g. 7 milestones accepted, 8 rejected; 0 milestones rejected; 0 criteria rejected; `extra="forbid"`)
- [x] T011 [P] [US1] Write `tests/unit/workflows/architect/test_allowlist.py`: allowed (`ls`, `cat`, `head`, `tail`, `sed -n '1,40p' f`, `rg`, `grep`, `find . -name x`, `wc`, `git log|show|diff|ls-files|status|blame`), refused (`bundle install`, `rails db:migrate`, `rm`, `curl`, `sleep`, `find -exec`, `find -delete`, `sed -i`, `git push|commit|checkout|reset`, redirections `>`/`>>`, `;` and `&&` chaining to a refused command), pipes only between allowed commands. One test per rule instance
- [x] T012 [P] [US1] Write `tests/unit/workflows/architect/test_survey.py`: the model's proposed list is executed in order; refused commands are recorded with a reason and consume budget; output truncated to `max_output_chars`; the survey stops at `max_commands`; env knobs `ARCHITECT_SURVEY_MAX_COMMANDS` and `ARCHITECT_SURVEY_MAX_OUTPUT_CHARS` are honoured from `score.workflow_env`
- [x] T013 [P] [US1] Write `tests/unit/workflows/architect/test_blueprint_step.py`: schema-guarded call with one reprompt (reuse 164 `validate_with_reprompt`), truncation retry via `call_with_budget`, budget `Budget.for_step("blueprint") >= 8000`, a hollow blueprint (zero milestones) raises `SchemaViolation` naming `milestones`
- [x] T014 [P] [US1] Write `tests/unit/workflows/architect/test_report.py`: the report carries `blueprint`, `size`, `write_free_check` (an executed `git status --porcelain` with exit code and `passed`), refused-command count, and `workflow_metrics`; a dirty tree makes `write_free_check.passed` false and the run fail
- [x] T015 [US1] Write `tests/unit/workflows/architect/test_workflow_end_to_end.py` per the independent test above, plus: the workflow never calls `commit_file`, `push_branch` or any write primitive (assert via a toolkit spy)
- [x] T016 [P] [US1] Write `tests/unit/graph/nodes/test_blueprint_lift.py`: `monitor_performer` lifts `report.blueprint` and `report.size` into the session on architect success, replaces a prior blueprint, resets `documenting_side`, and does nothing on the prose path (no `report.blueprint`)
- [x] T017 [P] [US1] Write `tests/unit/graph/nodes/test_brief_slicing.py`: `dispatch_performer` projects the three briefs per data-model.md; disjointness (docs never to implementer or QA; milestones never to documenter or QA; criteria to QA only); keys absent when no blueprint; `implementer_single_turn` only for small
- [x] T018 [P] [US1] Write `tests/unit/workflows/test_brief_prompt.py`: `_card_docs` renders the implementation brief into the implementer prompt (milestones numbered, done-when present, no docs topics) and the documentation brief into the documenter prompt; nothing rendered when absent
- [x] T019 [P] [US1] Write `agent/performer/tests/unit/test_architect_report_path.py`: `main.py` architecting post-processing skips the `plan.md`/`tasks.md` commits and returns `status: done` with `report` when the backend output is the workflow report; the prose path is unchanged when it is not (existing tests still pass)

### Implementation

- [x] T020 [P] [US1] Implement `agent/performer/src/performer/workflows/architect/models.py` (Blueprint, Milestone, Module, DataModel, DataModelChange, Interface, Criterion, DocTopic; bounds; `extra="forbid"`; `render_schema` compatible with 164 `schema_guard`)
- [x] T021 [P] [US1] Implement `agent/performer/src/performer/workflows/architect/allowlist.py` (`is_allowed(command) -> tuple[bool, str]`, pure; pipe and chaining handling per T011)
- [x] T022 [US1] Implement `agent/performer/src/performer/workflows/architect/personas.py` (SURVEY: propose up to N read-only commands as a JSON list with a one-line reason each; BLUEPRINT: the schema instruction is prepended by the toolkit) and `intake.py` (assemble card, criteria, assessment when `docs/cards/<id>/assessment.md` exists in the workspace, clarifications, `AGENTS.md`/`CLAUDE.md` when present; no model call)
- [x] T023 [US1] Implement `agent/performer/src/performer/workflows/architect/survey.py` (`run_survey_step(toolkit, score, intake, budget)`; `SurveyRecord` per command; truncation; budget from `workflow_env` with defaults 12 and 4000; logs `architect.survey` per command)
- [x] T024 [US1] Implement `agent/performer/src/performer/workflows/architect/blueprint.py` (`run_blueprint_step(toolkit, intake, survey)` using `validate_with_reprompt` and `call_with_budget`; add `"blueprint": 8000` and `"survey": 3000` to `_STEP_BUDGETS` in `workflows/budget.py` with the floors test extended)
- [x] T025 [P] [US1] Implement `agent/performer/src/performer/workflows/architect/size.py` (`size_of(blueprint) -> Literal["small","large"]`) and `report.py` (`build_report(blueprint, size, survey, write_free_check, metrics)`; the executed `git status --porcelain` check through the toolkit's command runner)
- [x] T026 [US1] Implement `ArchitectWorkflow.run` in `agent/performer/src/performer/workflows/architect/__init__.py`: fixed step order, `qa.`-style progress events named `architect.<step>`, per-step `step_durations_ms`, no cleanup needed (nothing is created), hollow or invalid blueprint raises with the field names
- [x] T027 [US1] In `agent/performer/src/performer/main.py` architecting post-processing: when `backend_status.output` parses as a report with `blueprint`, return `PerformerResponse(status="done", report=...)` and skip the commits; otherwise the existing prose path verbatim; make T019 pass
- [x] T028 [US1] In `src/coordinare/graph/nodes/monitor_performer.py`: lift `report.blueprint` + `report.size` into `state["blueprint"]` (and through the daemon's session persistence into `PersistedSession.blueprint` with `blueprint_hash`, `created_at`), replacing a prior value and resetting `documenting_side`; log `blueprint.lifted`; make T016 pass
- [x] T029 [US1] In `src/coordinare/graph/nodes/dispatch_performer.py`: `inject_briefs(card_context, state, role)` projecting the three briefs and `implementer_single_turn` per data-model.md, mirroring `inject_qa_findings`; make T017 pass
- [x] T030 [US1] In `agent/performer/src/performer/backends/_card_docs.py`: `implementation_brief_prompt_section(score)` and `documentation_brief_prompt_section(score)`, called from the seven `_build_task_prompt`s exactly where `qa_findings_prompt_section` is; make T018 pass
- [x] T031 [US1] In `src/coordinare/services/persona_service.py`: the implementer per-turn procedure reads milestones from the brief when present (fallback to `plan.md`/`tasks.md` text otherwise) and the Forbidden list gains "creating or editing documentation"; test in `tests/unit/services/test_persona_implementer_brief.py` that both variants render and that the no-documentation line is present in both

## Phase 4: User Story 2 - A small card gets a small plan and no ceremony (P1)

**Goal**: code sizes the blueprint; a small card gets a single-turn implementer and no documenter.

**Independent test**: `tests/unit/workflows/architect/test_size.py` boundaries plus `tests/unit/graph/nodes/test_single_turn_dispatch.py`.

- [x] T032 [P] [US2] Write `tests/unit/workflows/architect/test_size.py`: one milestone and nothing else is small; one milestone plus one column change is large; one milestone plus one interface is large; two milestones are large; mutation-check each threshold
- [x] T033 [P] [US2] Write `tests/unit/graph/nodes/test_single_turn_dispatch.py`: `implementer_single_turn: true` reaches the implementer's card_context only for small; the implementer persona variant omits the PARTIAL_PROGRESS instructions when set (test in `tests/unit/services/test_persona_implementer_brief.py`)
- [x] T034 [US2] Implement the single-turn persona variant in `src/coordinare/services/persona_service.py` and the `implementer_single_turn` plumbing in `dispatch_performer.py` (already projected in T029; this task wires the persona selection); make T032 and T033 pass
- [x] T035 [US2] Write `tests/unit/graph/nodes/test_documenter_skip_on_empty_brief.py`: an empty `docs` list means `documenting_side` is never dispatched and the card proceeds to implementing only (the trigger is implemented in US3; this test pins the skip rule and is made to pass by T041)

## Phase 5: User Story 3 - The documenter runs alongside implementation without conflict (P2)

**Goal**: an out-of-lifecycle documenter run, dispatched once per blueprint hash, writing only under the documentation tree, with rebase-before-push.

**Independent test**: `tests/unit/services/test_documenting_side.py` with a fake performer service.

- [x] T036 [P] [US3] Write `tests/unit/services/test_documenting_side.py`: dispatch once per `blueprint_hash` (a second cycle does not re-dispatch); dispatched only after the card advanced past architecting and only when `docs` is non-empty; monitor records `done` with `head_sha` or `failed` with `result_reason`; a failed run is not retried within the card; a new blueprint hash resets and re-dispatches
- [x] T037 [P] [US3] Write `agent/performer/tests/unit/test_documenter_tree_guard.py`: a commit touching a path outside the symphony's documentation tree (default `docs/`) is rejected before push with a named reason; commits inside it pass; the tree root is configurable via `workflow_env` `DOCUMENTER_TREE`
- [x] T038 [US3] Implement `src/coordinare/services/documenting_side.py` (`DocumentingSideRun` model from data-model.md; `maybe_dispatch(session, state, performer_service)`; `monitor(session, performer_service)`; logs `documenting_side.dispatched|completed|failed`), following `env_cache.check_and_trigger` for the out-of-lifecycle dispatch and result handling
- [x] T039 [US3] Wire the side run into `src/coordinare/graph/nodes/check_board.py` (trigger and monitor each cycle for sessions with a blueprint) and persist `documenting_side` through the daemon's session save; make T036 pass
- [x] T040 [US3] Implement the documentation-tree guard in `agent/performer/src/performer/workspace.py` (applied when `score.role == "documenting"` and the dispatch carries `documentation_brief`), make T037 pass; confirm the end-of-lifecycle documenting stage is untouched (existing tests in `tests/unit/graph/nodes/` for spec 125 still pass)
- [x] T041 [US3] Make T035 pass (empty brief skips dispatch) and add `tests/unit/services/test_documenting_side_concurrency.py`: implementer and documenter both push to one branch with disjoint paths in either order; with the T005 push path both land and neither force-pushes (fake git recording sequences)

## Phase 6: User Story 4 - QA plans from the verification brief (P2)

**Goal**: the QA plan step uses the blueprint's criteria when present and is unchanged otherwise.

**Independent test**: `tests/unit/workflows/qa/test_plan_from_brief.py`.

- [x] T042 [P] [US4] Write `tests/unit/workflows/qa/test_plan_from_brief.py`: with `score.verification_brief` the plan's criteria equal the brief's criteria in count and text, the prompt states they are fixed, and the model is not asked to derive criteria; without a brief the existing prompt and behaviour are unchanged (reuse `tests/unit/workflows/qa/test_steps.py` fixtures)
- [x] T043 [US4] Implement the brief-aware criteria source in `agent/performer/src/performer/workflows/qa/plan.py` (`criteria_source="blueprint"` recorded on the plan and in the report); make T042 pass

## Phase 7: Evaluation and polish

- [x] T044 [P] Create `tests/eval/architect_scenarios/` with `fixtures.py` (three cards: trivial copy fix, mid-size feature, schema plus interfaces), `stub_model.py` (deterministic canned survey and blueprint answers per fixture), `scoring.py` (size class, no side effects via the write-free check, slice contents, docs presence), `README.md`; and `src/coordinare/eval/architect_scenarios.py` with `--live` (gateway) and default stubbed modes; the stubbed mode runs under pytest, the live mode is not a CI gate
- [x] T045 [P] Extend `tests/unit/workflows/test_dispatch_branch.py` (164 FR-005 tests) to the architect role: with no `workflow` the architect path is byte-for-byte the prose path
- [x] T046 [P] Add the `architect` example to `config.example.yaml` (commented) and a "Role workflows: architect" subsection to `docs/onboarding/04-harnesses-and-shims.md` describing the blueprint, the three briefs, the side run and the push-path change
- [x] T047 Run both test trees in the worktree with `PYTHONPATH=src:agent/performer/src`, `ruff check src tests agent/performer`, and the stubbed eval; record the mutation checks performed (one per rule test) in the PR description
- [x] T048 Adversarial review (Workflow: diverse-lens finders plus refute-by-execution) over the full diff before opening the PR; disposition every finding (standing rule)
- [x] T049 Rebuild `coordinare-performer:{base,full,extra}` from the branch, run one live architect round on the `trivial` and `schema` fixtures against the gateway in a performer container, and record the round durations against SC-001 before enabling the flag on the website role

## Dependencies

- Phase 2 before every story (T005 push path and T007 schema are prerequisites for US3 and the lift respectively; T008/T009 for every brief).
- US1 (Phase 3) before US2 (the size rule reads the blueprint) and before US3/US4 (they consume slices). US2 and US4 are independent of each other. US3 depends on US1 and T005.
- T035 is written in US2 and made to pass in US3 (T041).

## Parallel execution examples

- Phase 3 tests T010 to T014 and T016 to T019 can be written concurrently (distinct files); T020, T021 and T025 can be implemented concurrently once T010/T011/T014 exist.
- T032, T033 (US2) and T042 (US4) can run in parallel with US3's T036/T037.
- Phase 7: T044, T045, T046 in parallel; T047 to T049 sequential.

## Implementation strategy

MVP is Phase 2 plus Phase 3 plus T032 to T034: a bounded architect that hands the implementer a brief, with sizing. That alone removes the two-hour rounds and the duplicated prose. US3 (the side run) and US4 (QA from the brief) ship in the same PR behind the same flag but are independently testable and can be disabled in config if a live run shows branch conflicts.
