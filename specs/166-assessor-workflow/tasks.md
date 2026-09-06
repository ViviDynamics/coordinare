# Tasks: Assessor Role Workflow with a Structured Assessment Hand-off

**Input**: Design documents from `/specs/166-assessor-workflow/`
**Prerequisites**: plan.md, research.md, data-model.md, contracts/assessment.schema.json, contracts/dispatch-payload-additions.md, quickstart.md

**Tests**: required. Every gate rule (cap_questions, drop_answered, force_ready_after_rounds, ready_wins, criteria_source) gets a test written first and mutation-checked once per instance of the rule (standing rule: mutate in the worktree with `PYTHONPATH=src:agent/performer/src`, assert the mutation applied, restore, run the suite after restoring). The two test trees run separately: `tests/` and `agent/performer/tests/`.

**Organization**: tasks grouped by user story. US1 (clear card) and US2 (ambiguous card with bounded questions) are both P1 and share the workflow layer; US3 (draft criteria) is P2. Phases 1 and 2 are prerequisites; US1/US2/US3 tasks follow.

## Format: `[ID] [P?] [Story] Description`

## Phase 1: Setup

- [x] T001 Create the package skeleton `agent/performer/src/performer/workflows/assessor/` with `__init__.py`, `models.py`, `personas.py`, `intake.py`, `assess.py`, `gate.py`, `report.py`, and `tests/unit/workflows/assessor/__init__.py`
- [x] T002 [P] Register `"assessor"` in `SUPPORTED_WORKFLOWS` in `agent/performer/src/performer/workflows/__init__.py` and in `KNOWN_WORKFLOWS` in `src/coordinare/config.py`; extend `tests/unit/workflows/test_registry.py` (or create it if not shared with 164/165) so it accepts `assessor` and still rejects an unknown name
- [x] T003 Add `"assess": 3000` to `_STEP_BUDGETS` in `agent/performer/src/performer/workflows/budget.py` and write a test in `tests/unit/workflows/test_budget_floors.py` that the floor is enforced

## Phase 2: Foundational (blocks all user stories)

- [x] T004 [P] Write failing tests in `tests/unit/workflows/test_text.py`: (a) `token_overlap("Which audience?", "What audience is this page for?", threshold=0.6)` returns True; (b) `token_overlap("Which audience?", "What is the colour scheme?", threshold=0.6)` returns False; (c) `token_overlap("WHICH AUDIENCE?", "which audience?")` returns True (case-folded); (d) punctuation is stripped before tokenization; (e) edge cases: empty strings, single token, threshold exactly met (at least 60 percent overlap)
- [x] T005 Implement `token_overlap(a: str, b: str, threshold: float = 0.6) -> bool` and `matches_answered(question: str, answered_question: str, threshold: float = 0.6) -> bool` in `agent/performer/src/performer/workflows/_text.py` per research R4: case-fold, strip punctuation, tokenize on whitespace, compute intersection over minimum set size; make T004 pass
- [x] T006 Write failing tests in `tests/unit/test_state_store_schema18.py`: `PersistedSession.assessment` defaults to `None`, round-trips through save and load, a v17 snapshot loads with `assessment: None`, and `CURRENT_SCHEMA_VERSION == 18`
- [x] T007 Add `assessment: dict[str, Any] | None = None` to `PersistedSession` in `src/coordinare/state_store.py`; bump `CURRENT_SCHEMA_VERSION` to 18 with the header comment convention and a comment referencing spec 166; add a post-load validator that drops malformed assessments (pattern: `_drop_corrupt_documenting_side` from 165); make T006 pass
- [x] T008 Write failing contract tests in `tests/contract/test_dispatch_payload.py` for `assessment` field: present when architecting dispatch follows `assessment_complete`, absent from all other dispatches and all stages when the role has no `workflow: assessor`; add the row to `specs/contracts/dispatch-payload.md` per `contracts/dispatch-payload-additions.md`
- [x] T009 Declare `assessment: dict[str, Any] | None = None` on `Score` in `agent/performer/src/performer/models.py` so `extra="ignore"` cannot drop it; make T008 pass
- [x] T010 Write failing tests in `tests/unit/graph/nodes/test_assessment_session_wiring.py`: `PersistedSession.assessment` field is tracked in `daemon.py` save/restore (mirrors session.py handling of `blueprint` and `documenting_side`); the field name exists in `session.py _SESSION_FIELDS`; the field is initialized in `graph/state.py` TypedDict and initial_state default
- [x] T011 Add assessment field to `src/coordinare/daemon.py` (save/restore mapping), `src/coordinare/session.py` (_SESSION_FIELDS), and `src/coordinare/graph/state.py` (TypedDict, initial_state default) per patterns from 165; make T010 pass
- [x] T012 [P] Write `tests/unit/workflows/assessor/test_models.py`: every list and string bound from data-model.md is enforced (one test per bound at the boundary, e.g. 2 questions accepted, 3 rejected; 5 out-of-scope rejected; 8 criteria accepted, 9 rejected; 10 assumptions accepted, 11 rejected; empty goal rejected; `extra="forbid"` rejects unknown fields; not-ready assessment must have at least one question; ready assessment may have zero questions)
- [x] T013 Implement `agent/performer/src/performer/workflows/assessor/models.py` (Assessment, GateRecord, ClarificationRound per data-model.md; pydantic models with `extra="forbid"`, bounds enforced, `render_schema` compatible with 164 `schema_guard`); make T012 pass

## Phase 3: User Story 1 - A clear card is assessed and hands the architect a structured assessment (P1)

**Goal**: the assessor workflow runs intake, assess, gate, report inside the performer, commits nothing, and coordinare lifts the assessment to the architecting dispatch.

**Independent test**: `tests/unit/workflows/assessor/test_workflow_end_to_end.py` with a stub model answering the assess prompt against the `clear` fixture: no writes, budget respected, valid assessment with `ready: true` and `criteria_source: "card"`, no command ran, working tree untouched. Coordinare wiring: assessment is persisted, restored after restart, and injected only into architecting dispatch.

### Tests first

- [x] T014 [P] [US1] Write `tests/unit/workflows/assessor/test_intake.py`: `build_intake(score)` assemble card, criteria, clarifications, answered round count (only non-blank answers count per FR-009); test the exact text sections assembled; answered round count matches the clarification history

- [x] T015 [P] [US1] Write `tests/unit/workflows/assessor/test_assess.py`: schema-guarded call under budget "assess" (3000 completion tokens per FR-002), one reprompt on schema violation (reuse 164 `validate_with_reprompt`), exactly one call on success, second violation raises, budget overflow raises

- [x] T016 [P] [US1] Write `tests/unit/workflows/assessor/test_report.py`: the report carries `assessment`, `gate_record`, `write_free_check` and `workflow_metrics`; `write_free_check` records `commands_run` (always 0, read from `toolkit.metrics`) and `has_command_runner: false`; a report built from a toolkit that has a command runner or ran any command fails; the assessment dict is valid per T012 bounds

- [x] T017 [P] [US1] Write `tests/unit/graph/nodes/test_assessment_lift.py`: `monitor_performer` lifts `report["assessment"]` into `state["assessment"]` on `status: "assessment_complete"`, replaces a prior assessment, logs `assessment.lifted`, and does nothing on the prose path (no `report.assessment`)

- [x] T018 [P] [US1] Write `tests/unit/graph/nodes/test_assessment_dispatch.py`: (a) `dispatch_performer` injects assessment into `card_context` only when `performer_stage == "architecting"`; (b) assessment is absent from all other stages; (c) `reset_assessment_for_assessor` clears `state["assessment"]` when dispatching `performer_stage == "assessing"`, mirrors `reset_blueprint_for_architect` (T005 pattern)

- [x] T019 [P] [US1] Write `agent/performer/tests/unit/test_assessor_report_path.py`: `main.py` assessing post-processing skips the `docs/cards/<n>/assessment.md` commit and returns `status: "assessment_complete"` with `report` when the backend output has an "assessment" key (mirrors the 165 architect path); the prose path is unchanged when it does not exist (existing tests still pass)

- [x] T020 [P] [US1] Write `tests/unit/workflows/assessor/test_workflow_end_to_end.py` per the independent test above, plus: the workflow never calls `commit_file`, `push_branch`, or any write primitive (assert via a toolkit spy); toolkit has no command runner (`command_runner=None`)

- [x] T021 [P] [US1] Write `tests/unit/workflows/architect/test_intake_assessment.py`: when `score.assessment` is present, `architect/intake.py` builds an "Assessment" section FIRST containing goal, expected behaviour, out-of-scope items, assumptions, clarifications, and draft criteria (labelled as draft when `criteria_source: "assessor"`); when absent, no section is built

- [x] T022 [P] [US1] Write `tests/unit/workflows/test_dispatch_branch.py` extended (or created) to the assessor role: with no `workflow` the assessor path is byte-for-byte the prose path (commits assessment.md, returns the existing status/verdict shape)

### Implementation

- [x] T023 [US1] Implement `agent/performer/src/performer/workflows/assessor/intake.py` (`build_intake(score) -> str`, assemble card, criteria, clarifications, count answered rounds per FR-009; log `assessor.intake`); make T014 pass

- [x] T024 [US1] Implement `agent/performer/src/performer/workflows/assessor/personas.py` (ASSESS step persona: read the card and clarifications, produce a JSON assessment per the schema; output is the schema constraint only; forbid implementation questions per FR-002 and FR-003; product manager tone)

- [x] T025 [US1] Implement `agent/performer/src/performer/workflows/assessor/assess.py` (`run_assess_step(toolkit, score, intake, budget)` using `validate_with_reprompt` and `call_with_budget`; exactly one call under budget "assess" per FR-002; log `assessor.assess`); make T015 pass

- [x] T026 [P] [US1] Implement `agent/performer/src/performer/workflows/assessor/report.py` (`build_report(assessment, gate_record, toolkit) -> dict`; the write-free proof is structural: the Toolkit has no command runner and `metrics` shows zero commands, recorded as `write_free_check: {commands_run, has_command_runner, passed}`; raise if either is violated); make T016 pass

- [x] T027 [US1] Implement `AssessorWorkflow.run` in `agent/performer/src/performer/workflows/assessor/__init__.py`: fixed step order (intake, assess, gate, report), `assessor.`-style progress events, per-step `step_durations_ms`, per T017 specification; gate step (T029) integrates here

- [x] T028 [US1] In `agent/performer/src/performer/main.py` assessing post-processing: when `isinstance(_assessment_report, dict) and "assessment" in _assessment_report`, set `status = "assessment_complete"` and return `PerformerResponse(status=status, report=_assessment_report)` (mirrors 165 architect path); skip the assessment.md commit under workflow; apply the prose path when assessment key is absent or malformed; make T019 pass

- [x] T029 [US1] Implement the gate step in `agent/performer/src/performer/workflows/assessor/gate.py` (pure functions per Constitution II; FR-006 to FR-009; see Phase 4 and Phase 5); integrate into `AssessorWorkflow.run` per T027; log `assessor.gate`

- [x] T030 [US1] In `src/coordinare/graph/nodes/monitor_performer.py`: lift `report["assessment"]` into `state["assessment"]` when `perf.state == "assessment_complete"`, recording `assessment_hash` and `created_at`, replacing a prior value; log `assessment.lifted`; make T017 pass

- [x] T031 [US1] In `src/coordinare/graph/nodes/dispatch_performer.py`: (a) `inject_assessment(card_context, state)` projecting assessment into `card_context["assessment"]` when present, mirroring `inject_briefs`; (b) call it only when `performer_stage == "architecting"` per FR-013; (c) `reset_assessment_for_assessor` clearing `state["assessment"] = None` before the assessor is dispatched, mirrors `reset_blueprint_for_architect`; make T018 pass

- [x] T032 [US1] Implement assessment rendering in `agent/performer/src/performer/workflows/architect/intake.py`: when `score.assessment` is present, build an "Assessment" section FIRST containing goal, expected behaviour, out-of-scope items, assumptions, clarifications, and draft criteria (labelled as draft when `criteria_source: "assessor"`, "from the card" when `card`); make T021 pass

- [x] T033 [P] [US1] Create `tests/eval/assessor_scenarios/` with `fixtures.py` (three cards: `clear` with criteria and a straightforward goal, `ambiguous` with minimal description, `answered` carrying two answered rounds), `stub_model.py` (deterministic canned assess answers per fixture), `scoring.py` (assess: readiness, question count, no-re-ask rule, criteria source, write-free record, assessment reaches architecting only), `README.md`; and `src/coordinare/eval/assessor_scenarios.py` with `--live` (gateway) and default stubbed modes per quickstart.md

- [x] T034 [P] [US1] Extend `tests/unit/workflows/test_dispatch_branch.py` (164 FR-005 tests) to the assessor role: with no `workflow` the assessor path is byte-for-byte the prose path (make T022 pass)

## Phase 4: User Story 2 - An ambiguous card asks at most two questions and never twice (P1)

**Goal**: code-driven gate rules bound the clarification loop: at most two questions per round, never re-asking an answered question, ready after two answered rounds with the rest as assumptions.

**Independent test**: `tests/unit/workflows/assessor/test_gate.py` per fixtures below. Gate keeps at most two questions (cap_questions rule). Gate drops questions matching answered clarifications under FR-007 measure (drop_answered rule). After two answered rounds, gate forces ready and moves questions to assumptions (force_ready_after_rounds rule). Ready assessment wins (drops questions, records them as assumptions).

- [x] T035 [P] [US2] Write `tests/unit/workflows/assessor/test_gate_cap_questions.py`: (a) model returns four questions, gate keeps first two and records rest as dropped; (b) model returns two questions, gate keeps both; (c) model returns one question, gate keeps it; (d) model returns zero questions, gate keeps none; mutation-check by removing the cap or changing the number

- [x] T036 [P] [US2] Write `tests/unit/workflows/assessor/test_gate_drop_answered.py`: (a) model asks "Which audience?" and clarifications carry `{"question": "Which audience?", "answer": "Prospective clients"}`, gate drops the question and records the answer in assumptions; (b) a question that does not match (case-folded, 60 percent overlap) is kept; (c) an answered question with a blank answer does not count (FR-009); (d) mutation-check by removing the overlap check or changing the threshold

- [x] T037 [P] [US2] Write `tests/unit/workflows/assessor/test_gate_force_ready_after_rounds.py`: (a) second answered round with new questions, gate forces ready and moves questions to assumptions phrased as decisions; (b) first answered round with new questions, gate returns not ready with questions; (c) zero answered rounds, gate returns not ready if model did; (d) mutation-check by removing the round counter check or changing the threshold

- [x] T038 [P] [US2] Write `tests/unit/workflows/assessor/test_gate_ready_wins.py`: (a) model returns ready with questions, gate drops questions, records them as assumptions, returns ready; (b) mutation-check by not dropping questions or not returning ready

- [x] T039 [P] [US2] Write `tests/unit/workflows/assessor/test_gate_integration.py`: apply all four gate rules in order; test the GateRecord is populated correctly (questions_kept, questions_dropped_by_cap, questions_dropped_as_answered, answers_matched, questions_turned_to_assumptions, round_count)

- [x] T040 [US2] Implement gate step pure functions in `agent/performer/src/performer/workflows/assessor/gate.py`: (a) `cap_questions(assessment, max=2) -> tuple[list, list]` (kept, dropped); (b) `drop_answered(questions, clarifications) -> tuple[list, list, dict]` (kept, dropped, matched_answers) using `workflows._text.matches_answered`; (c) `force_ready_after_rounds(ready, questions, answered_round_count) -> tuple[bool, list, list]` (new_ready, questions_to_assumptions, decisions); (d) `ready_wins(ready, questions) -> tuple[bool, list]` (drop questions if ready); each as a separate pure function; (e) `run_gate_step(assessment, clarifications, answered_round_count) -> tuple[Assessment, GateRecord]` integrating the rules; make all T035-T039 pass

- [x] T041 [US2] Write `tests/unit/graph/nodes/test_blocked_assessment_path.py`: coordinare's monitor_performer sets `open_questions` and `assessor_open_questions` when `perf.state == "blocked"`, and the issue-comment carry-forward works exactly as today (no new coordinare logic required; the gate and monitor already handle it)

## Phase 5: User Story 3 - Draft criteria when the card has none (P2)

**Goal**: when a card has no acceptance criteria, the assessor drafts outcome-level criteria; the architect receives them marked as draft and refines them into the verification brief.

**Independent test**: `tests/unit/workflows/assessor/test_gate_criteria_source.py` with fixtures `clear` (card has criteria: `criteria_source: "card"`, assessment criteria empty) and `no_criteria` (card has none: `criteria_source: "assessor"`, assessment carries draft criteria, at least one, at most eight).

- [x] T042 [P] [US3] Write `tests/unit/workflows/assessor/test_gate_criteria_source.py`: (a) card has criteria, model proposes draft criteria, gate drops them and sets `criteria_source: "card"`; (b) card has no criteria, model drafts, gate keeps at least one (rejects zero draft as invalid) and at most eight, sets `criteria_source: "assessor"`; (c) mutation-check by not dropping when card has criteria or by changing the bounds

- [x] T043 [US3] Implement the criteria_source gate rule in `agent/performer/src/performer/workflows/assessor/gate.py`: `set_criteria_source(score, assessment) -> tuple[list, str]` pure function that returns (criteria list, source string per FR-005); integrate into `run_gate_step`; make T042 pass

- [x] T044 [P] [US3] Write `tests/unit/workflows/qa/test_plan_effective_criteria.py`: QA plan uses blueprint criteria when present (from the architect's verification brief, not the assessor's draft); when no blueprint, plan uses card criteria or derives them (existing path unchanged); assessor draft criteria never reach QA directly

- [x] T045 [US3] Write `tests/unit/workflows/architect/test_intake_assessment_draft_criteria.py`: architect intake labels draft criteria as "draft acceptance criteria (to refine into your verification brief)" and card criteria as "from the card"

## Phase 6: Polish & Cross-Cutting Concerns

- [x] T046 [P] Add the `assessor` example to `config.example.yaml` (commented) and a "Role workflows: assessor" subsection to `docs/onboarding/04-harnesses-and-shims.md` describing the intake, bounded assess call, gate rules, and hand-off

- [x] T047 [P] Write `tests/unit/workflows/test_log_format.py` extended to assessor: every step logs its duration in `step_durations_ms` (intake, assess, gate, report), and the assess call logs elapsed time and completion tokens, following specs 164 and 165 format (FR-018)

- [x] T048 [P] Write `tests/unit/workflows/assessor/test_toolkit_no_command_runner.py`: the adapter builds the assessor Toolkit with `command_runner=None`; calling `run_command` raises; the report's `commands_run: 0` confirms (FR-003)

- [x] T049 Run both test trees in the worktree with `PYTHONPATH=src:agent/performer/src`, `ruff check src tests agent/performer`, and the full eval (stubbed and fixture validation); record the mutation checks performed (one per gate rule per T035-T038, one per criteria rule per T042); all US1, US2, US3 tests pass

- [x] T050 Adversarial review (Workflow: diverse-lens finders plus refute-by-execution) over the full diff before opening the PR; disposition every finding (standing rule)

- [x] T051 Rebuild `coordinare-performer:{base,full,extra}` from the branch, run one live assessor round on the `clear` and `ambiguous` fixtures against the gateway in a performer container (per quickstart.md live eval), and record the round durations against SC-001 before enabling the flag

## Dependencies

- Phase 2 (Foundational) before all user stories: T005 (text matcher) and T007 (schema bump) are prerequisites for US2 gate and US3 criteria; T008/T009/T011 are prerequisites for the coordinare lift and inject.
- US1 (Phase 3) before US2 and US3: the workflow structure and the intake, assess, report steps must exist before gate rules are implemented and tested.
- US2 (Phase 4, cap_questions/drop_answered/force_ready_after_rounds/ready_wins rules) independent of US3 (Phase 5, criteria_source rule) once Phase 2 is done.
- T032 (architect intake rendering) can run with US1 implementation; T045 depends on T032 and T043.

## Parallel execution examples

- Phase 1: T002 and T003 in parallel (separate registration files)
- Phase 2: T004/T005 (text matcher), T006/T007 (schema), T008/T009 (dispatch), T010/T011 (session wiring), T012/T013 (models) all run in parallel; they touch disjoint files
- Phase 3 tests T014-T022 can be written concurrently (distinct test files; T020/T021 depends on the model per T013); T023-T032 implementation proceeds once models exist
- Phase 4: T035-T039 (gate rules) can be written in parallel (separate test files); T040 implementation once tests pass
- Phase 5: T042 and T044/T045 can run with Phase 4 (separate concerns); T043 implementation once T042 passes
- Phase 6: T047, T048, T049 in parallel; T050, T051 sequential

## Implementation strategy

MVP is Phase 2 plus Phase 3 plus Phase 4: a bounded assessor that reads the card, makes one model call, applies bounded gate rules, and hands the architect a structured assessment with at most two questions, never repeating. That removes the lenient prose fallback, the repeated questions, and unbounded rounds. US3 (draft criteria) ships in the same PR but is independently testable and can be disabled via a gate in gate.py if live runs show issues.

Rollout: after all tests pass stubbed and `clear` and `ambiguous` fixtures each run once live in a performer container with round durations recorded against SC-001, enable via `workflow: assessor` on the assessor role in the symphony config.
