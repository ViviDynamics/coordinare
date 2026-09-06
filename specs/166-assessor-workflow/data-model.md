# Data Model: Assessor Role Workflow with a Structured Assessment Hand-off

## Assessment (performer output, persisted on the session)

| Field | Type | Bounds | Notes |
| --- | --- | --- | --- |
| `ready` | bool | required | true when the assessor is confident in the outcome, false when human input is needed |
| `goal` | str | 1-500 chars | one sentence, what the card is trying to achieve |
| `expected_behavior` | str | 0-1000 chars | what the user or system does / what changes; empty means only goal, no behaviour change |
| `out_of_scope` | list[str] | 0-5 items, each <= 300 chars | things explicitly not in this card (e.g. refactoring, optimization, future feature) |
| `questions` | list[str] | 0-2 items, each <= 300 chars | clarifications needed from the human; only when `ready` is false |
| `assumptions` | list[str] | 0-10 items, each <= 300 chars | decisions the assessor made for the human to correct on the issue (no model rewrite) |
| `criteria` | list[Criterion] | 0-8 items | draft acceptance criteria in the blueprint criterion shape (spec 165); empty when `criteria_source` is `card`; at least one item when `assessor` and the assessment is ready (enforced by the gate, FR-005) |
| `criteria_source` | "card" \| "assessor" | required | "card" when the card has criteria, "assessor" when drafted by the model (FR-005) |
| `clarifications` | list[ClarificationRound] | 0-N | carried from the input for the architect to see context |
| `assessment_hash` | str | SHA-256 hex | fingerprint of `{goal, expected_behavior, out_of_scope, criteria}` for de-duplication |

**Criterion**: identical to spec-165 Blueprint Criterion. `surface` (<= 120), `action` (<= 200), `expected` (<= 300), `kind: functional|visual|command`.

**ClarificationRound**: `{"question": str, "answer": str}`, carried from the dispatch Score.clarifications list. No new fields added; same shape as today.

Validation: pydantic models with `extra="forbid"`, `min_length`/`max_length` on every list and string. A model answer with an empty goal is invalid (schema rejects it); the criteria floor depends on the source and is enforced by the gate. A not-ready assessment must have at least one question; a ready assessment can have zero questions. Unknown fields are rejected (FR-004).

## GateRecord (travels in the report, not persisted)

Returned by the gate step to document what changed in the model's answer:

| Field | Type | Notes |
| --- | --- | --- |
| `questions_kept` | int | count of questions kept (0-2) |
| `questions_dropped_by_cap` | list[str] | questions rejected because count exceeded 2 |
| `questions_dropped_as_answered` | list[str] | questions that matched an answered clarification under FR-007 |
| `answers_matched` | list[str] | the answers from clarifications that matched questions (for logging) |
| `questions_turned_to_assumptions` | list[str] | questions moved to assumptions when `ready` was forced true by FR-008 |
| `round_count` | int | number of answered clarification rounds seen at this dispatch |

The gate record is included in the PerformerResponse for eval and logging; coordinare does not consume it. Coordinare sees the final Assessment only.

## ClarificationRound (carried, not mutated)

One question-answer pair from the card's history:

| Field | Type | Notes |
| --- | --- | --- |
| `question` | str | the human's question posted to the issue |
| `answer` | str | the human's answer; blank is treated as "not answered" (FR-009) |

Sourced from dispatch_performer, which injects the session's `card_clarifications` (the existing clarification history, spec 045) as `Score.clarifications`. No mutation; the assessor workflow reads and passes through.

## State transitions (on the card session)

- **no assessment** -> **assessed** (when assessing stage reports `assessment_complete` with a valid assessment): monitor_performer lifts the assessment from report["assessment"]
- **assessed** -> **cleared** (when dispatch_performer is called for assessing stage): reset_on_assessor_dispatch sets state["assessment"] = None before the performer runs
- **assessed** (on architecting dispatch): the assessment is copied into card_context["assessment"] for the architect to read; other stages never see it

The assessment lives for exactly one cycle: from assessment_complete report through the architecting dispatch. On an assessor re-dispatch or daemon restart, it either survives (if persisted) or is discarded (if reset).

## Coordinare persistence (schema v18)

`PersistedSession.assessment: dict[str, Any] | None` (the validated Assessment dict with all fields, plus `assessment_hash` and `created_at` timestamp). A plain dict for JSON portability; the pydantic Assessment model is used in the workflow and at dispatch time only.

Rules: a new `assessment_complete` report replaces any prior assessment; a clearance (re-dispatch of assessing stage) sets it to None; an older snapshot (v17) loads with None.

Backward compatibility: older snapshots (v17 and earlier) have no assessment field; pydantic default of None keeps them loading. A malformed assessment dict is dropped to None on load (same pattern as `_drop_corrupt_documenting_side` in state_store.py).

## Dispatch payload field

`assessment: dict[str, Any] | None` is injected into card_context ONLY for the architecting stage (FR-013). Absence from other stages is enforced by `inject_assessment` function (parallel to `inject_briefs`), and verified by contract test.

The architect's intake (spec-165 architect/intake.py) renders the assessment when present, as the first section: "Product Assessment: goal, expected behaviour, out-of-scope items, assumptions, clarifications, and draft criteria (to refine into the verification brief)."

Other roles (implementer, QA, documenter, reviewer, security, etc.) never receive this field; it stays off their dispatch payloads for both pre-workflow and workflow paths.

## Answered-round count (read-only, derived)

At assessor dispatch, the count of answered clarification rounds is computed from Score.clarifications (injected from card_clarifications on PersistedSession). A round is answered when both question and answer are non-blank strings. The assessor intake logs this count; the gate uses it to decide whether to force `ready=true` (FR-008: after two answered rounds, must report ready).
