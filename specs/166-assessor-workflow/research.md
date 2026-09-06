# Research: Assessor Role Workflow with a Structured Assessment Hand-off

All decisions below were resolved by reading the code on branch `166-assessor-workflow` (based on main `50fa281` which contains spec 165) and comparing against the spec-164 and spec-165 patterns.

## R1. Toolkit allows command_runner=None and model_call can be called with a refusing runner

**Decision**: The `Toolkit` class accepts `command_runner=None` at __init__ (line 45 of workflows/toolkit.py). The `run_command` method raises `RuntimeError("Toolkit has no command runner configured")` if called with a None runner. No refusing runner is needed; the assessor workflow will never call `run_command` and does not import it, so the None value is safe.

**Rationale**: The assessor workflow (like the noop workflow) has no repository commands and no execution phase. The toolkit is created with command_runner=None in the adapter and the workflow never hits the error path.

## R2. How the workflow result is detected in main.py and mapped to assessment_complete or blocked

**Decision**: The assessor workflow's report is `PerformerResponse.report = {"assessment": {...}, "workflow_metrics": {...}}`. Main.py line 1899 checks `if perf.state == "assessment_complete"` and the assessing stage (line 2083) checks for a role of `"assessing"`. When the result is a dict with an "assessment" key, the performer state is set to `"assessment_complete"`. When `ready` is false, the performer reports `blocked` with the gate's questions, exactly the status coordinare handles for the prose assessor today.

**Rationale**: The spec requires `assessment_complete` when ready and `blocked` with the gate's questions when not (FR-011). The performer assessor has never reported dependencies (spec 046 parses the card description in coordinare), so the report carries none.

## R3. Source of the answered-round count for FR-008

**Decision**: `card_clarifications` lives on `PersistedSession` (state_store.py line 231) and is injected into Score.clarifications by dispatch_performer. An answered round is one where the question and answer are both non-empty strings. The count is computed from `card_clarifications` at the time of dispatch, keyed by the question text: rounds where the human answered are carried forward. The assessor workflow counts answered rounds at the start of its intake phase by reading `Score.clarifications` and counting entries where both `question` and `answer` are non-blank.

**Rationale**: The clarifications are the canonical history (spec 045 FR-007); they already carry both question and answer. No new coordinare field is needed; the assessor reads what is already there.

## R4. Token-overlap matcher for FR-007

**Decision**: The QA workflow (qa/plan.py and qa/__init__.py) does not currently implement a token-overlap matcher by that name. However, the concept is in the spec (FR-007: "at least 60 percent token overlap"). This is a new utility function to be added as `workflows._text.match_with_overlap(question: str, answered_question: str, threshold: float = 0.6) -> bool`, a pure function that: (1) case-folds both strings, (2) removes punctuation, (3) tokenizes on whitespace, (4) computes overlap as the size of the intersection divided by the size of the smaller set. The assessor workflow imports and uses it in the gate step.

**Rationale**: The matcher is a gate rule (FR-007) that must be testable in isolation (Constitution II). Placing it in a shared `_text` module keeps the assessor from importing from QA and avoids duplication if QA later adopts the same measure.

## R5. Assessment is injected only into architecting dispatch

**Decision**: The assessment is injected into `card_context` only when the performer_stage is `"architecting"` (spec FR-013). The architect's intake (in architect/intake.py, which must be modified in 166) reads the assessment first if present, rendering the goal, expected behaviour, out-of-scope items, assumptions, clarifications, and draft criteria as the first section. The 165 architect intake will render the assessment if present; the non-workflow architect persona receives no assessment field and ignores it. When the role is not architecting (e.g., implementer, QA, documenter), the assessment field is not injected into their dispatch payloads, so they see none.

**Rationale**: The assessment is not a general resource; it is specific to the architect's planning phase (spec FR-014). Other roles do not need it and must not see it. No schema bump for Score; the field will be added to the dispatch-payload contract with a guard that it is architecting-only.

## R6. Reset-on-assessor-dispatch rule (mirror of 165's reset_blueprint_for_architect)

**Decision**: When the assessor is dispatched (performer_stage == "assessing"), any prior assessment on the card is cleared at dispatch time by setting `state["assessment"] = None` in dispatch_performer, parallel to the `reset_blueprint_for_architect` function (line 180-192 of dispatch_performer.py). This ensures a failed assessor round does not leave a stale assessment for the architect to consume. The reset happens before the performer is called, so every assessor round starts fresh.

**Rationale**: The blueprint pattern (165) proved this rule necessary (spec 165 research #266). If the assessor fails to report, a stale assessment from the prior round must not reach the architect. The rule is enforced by code, not documented in the spec as a note that only prose tracks.

## R7. Schema version bump to v18 with backward compatibility

**Decision**: `state_store.py` CURRENT_SCHEMA_VERSION bumps from 17 to 18. `PersistedSession` gains a field `assessment: dict[str, Any] | None = None` with a default of None. The Assessment object (defined in data-model.md) is stored as a plain dict for JSON portability. Older snapshots (v17 and earlier) load unchanged; the assessment field defaults to None. Migration is zero-line: the field validator allows None and any dict, with a post-load cleanup that drops malformed assessments (the same pattern as `_drop_corrupt_documenting_side`).

**Rationale**: The assessment must survive daemon restart (spec FR-012). The schema versioning pattern is established (every spec with persisted state bumps the version and provides backward-compatible defaults). A malformed assessment is dropped, not a load failure, following the Constitution principle of fault tolerance.

## R8. Dispatch payload field registration

**Decision**: A new row is added to `specs/contracts/dispatch-payload.md` for the `assessment` field: one row for the architecting stage only, with the field name, type (dict or object), bounds, and the note "Injected only for the architecting stage; other stages never receive this field." The field is declared on `Score.assessment: dict[str, Any] | None = None` so `extra="ignore"` does not silently drop it, and a contract test verifies the field is present in architecting dispatch and absent in all others.

**Rationale**: The dispatch contract tracks what each stage receives (specs/contracts/dispatch-payload.md). The test (test_dispatch_payload.py) already validates the three brief fields; a parallel check for assessment follows the same pattern.

## R9. How the workflow in main.py detects and handles the assessment

**Decision**: Main.py assessing stage post-processing (currently lines 2083-2171) currently builds a JSON verdict with `{"sufficient": bool, "questions": [str]}` and commits an assessment.md file. Under the workflow path, the assessing stage checks `if isinstance(_assessment_report, dict) and "assessment" in _assessment_report` (mirroring line 2006 for the architect blueprint). When the assessment key is present and `ready` is true, the performer state is set to `"assessment_complete"` and the assessment dict is passed through as the report for monitor_performer to lift; when `ready` is false the performer reports `blocked` with the assessment's questions and no assessment is lifted. The lenient fallback (line 2092: treat prose as sufficient) is NOT applied under the workflow path; schema violations fail with a bounded retry (spec FR-016, handled by the existing malformed-output retry path in 098/119). The assessment.md file is NOT written under the workflow (FR-003).

**Rationale**: The workflow contract (FR-001) requires that the model never decides the flow; the gate step does. The assessment is the model output plus gate rules applied, not raw prose. The lenient fallback hides schema errors; under the workflow that is a bug, not a feature.
