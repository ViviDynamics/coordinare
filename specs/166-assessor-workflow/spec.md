# Feature Specification: Assessor Role Workflow with a Structured Assessment Hand-off

**Feature Branch**: `166-assessor-workflow`
**Created**: 2026-09-06
**Status**: Draft
**Input**: User description: "Assessor role workflow with a structured assessment hand-off. Extend the spec-164/165 role-workflow layer to the assessor performer: a bounded intake, assess, gate, report workflow with exactly one schema-guarded model call, no repository access and no commits. The assessment lives in coordinare state only, is lifted on assessment_complete, cleared when the assessor is re-dispatched, and travels only into the architecting dispatch, where the spec-165 intake renders it first and refines the draft criteria into the verification brief. Code-enforced round rules: at most two questions, no re-asking an answered clarification, ready after the second answered round with the rest recorded as assumptions. Default-off via workflow: assessor."

## Problem

The assessor today is one free-form model turn. It is asked to write an assessment in prose and then a routing JSON with `sufficient`, `questions` and `dependencies`. On the live fleet the JSON frequently fails to parse, and the lenient fallback treats unparseable prose as "sufficient". In practice the assessor approves whatever it saw, and the prose interpretation it wrote is discarded: nothing downstream reads it.

The assessor also commits a file, `docs/cards/<n>/assessment.md`, on the card branch. That makes it a second documentation writer beside the documenter, which is the arrangement spec 165 removed for the architect because two writers on one branch produce force-pushes and document conflicts.

Finally the clarification loop is bounded only by persona text. A not-ready assessment blocks the card and asks the human on the issue; the answers come back and the assessor runs again. Nothing in code stops it from asking a question that was already answered, or from blocking a third and fourth time.

## Goals

- The assessor runs as a bounded, code-driven workflow with exactly one model call per round and no repository access, so a round is the container start plus tens of seconds.
- The assessment is a structured record that lives in coordinare state and reaches the architect, so the answered clarifications, the stated goal and the draft acceptance criteria are inputs to the plan instead of being thrown away.
- The assessor commits nothing. The documenter remains the only documentation writer.
- The clarification loop is bounded by code: at most two questions per round, never a question already answered, at most two blocking rounds per card.
- Default off. A role without `workflow: assessor` behaves exactly as today.

## Non-goals

- Changing how a blocked card reaches the human, how the human answers, or how answers are carried into the next assessor run. That path is unchanged.
- Giving the assessor repository access or a survey step. Product readiness is a judgement about intent; the architect owns the technical survey (spec 165).
- Running the assessor outside a performer container. A later optimisation may run this workflow in the coordinare process; this spec keeps the performer transport.
- Refining acceptance criteria to a testable standard. The assessor drafts outcome-level criteria only when the card has none; the architect's verification brief (spec 165) remains the refined, testable version.
- Dependencies between cards. Spec 046 detects them by parsing the card description in coordinare; the performer assessor has never reported them and nothing consumes an assessor-reported dependency. The assessment carries none. Model-detected dependencies, if wanted, are a separate feature.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - A clear card is assessed in one bounded round and hands the architect a structured assessment (Priority: P1)

A maintainer puts a well-written card on the board. The assessor performer runs its workflow: it reads the card and its clarifications, makes one model call, applies the gate rules, and reports the card ready with a stated goal, expected behaviour and out-of-scope items. Nothing is committed. Coordinare records the assessment on the card and advances to architecting. The architect's intake opens with the assessment, so the plan starts from the product manager's reading of the card rather than from the raw text.

**Why this priority**: This is the whole hand-off and the path every card takes. Without it there is no structured assessment and no fix for the parse-and-approve failure.

**Independent Test**: Run the workflow against the `clear` fixture with a stubbed model. The report is a valid assessment with `ready` true and no questions, no command ran, and the recorded working tree is untouched. Lift the report into a card session and dispatch the architect: the architecting payload carries the assessment and no other dispatch does.

**Acceptance Scenarios**:

1. **Given** a card with a description and acceptance criteria and `workflow: assessor` on the role, **When** the assessor runs, **Then** it makes exactly one model call, runs no commands, commits nothing, and reports `assessment_complete` with an assessment whose `criteria_source` is `card`.
2. **Given** an `assessment_complete` report from the assessing stage, **When** coordinare processes it, **Then** the assessment is recorded on the card session, survives a daemon restart, and appears in the next architecting dispatch payload.
3. **Given** the architect is dispatched with an assessment, **When** the architect workflow builds its intake, **Then** the assessment's goal, expected behaviour, out-of-scope items, assumptions, clarifications and draft criteria are the first section of the intake text.
4. **Given** the role has no `workflow` setting, **When** the assessor runs, **Then** the dispatch payload, the persona and the performer behaviour are byte for byte what they are today, including the committed assessment file.

---

### User Story 2 - An ambiguous card asks the human at most two outcome-level questions, and never twice (Priority: P1)

A card says "make the services page better". The assessor cannot state the goal, so it returns not ready with one or two questions about the outcome. Coordinare blocks the card and posts the questions on the issue exactly as today. The maintainer answers. On the next run the assessor sees the answers, does not ask them again, and either reports ready or asks something new. After the second answered round the assessor must report ready, recording whatever is still uncertain as stated assumptions the human can correct on the issue.

**Why this priority**: The clarification loop is the one place a human is consulted before work starts, and it is also where unbounded rounds waste days. Both halves matter equally.

**Independent Test**: Run the `ambiguous` fixture: the gate keeps at most two questions and the report is `blocked`. Run the `answered` fixture, which carries two answered rounds: the gate turns any remaining questions into assumptions and the report is ready. Feed the gate a question whose wording matches an answered clarification: it is dropped and the answer appears in `assumptions`.

**Acceptance Scenarios**:

1. **Given** the model returns not ready with four questions, **When** the gate runs, **Then** two questions remain, two are recorded as dropped, and the report is `blocked` with exactly those two questions.
2. **Given** a clarification "Which audience?" was answered "Prospective clients" and the model asks "What audience is this page for?", **When** the gate runs, **Then** the question is dropped and `assumptions` contains the recorded answer.
3. **Given** the card already has two answered clarification rounds, **When** the model returns not ready with a new question, **Then** the gate returns ready, the question appears in `assumptions` phrased as a decision, and the card is not blocked.
4. **Given** the model returns not ready with no usable question after the dedupe, **When** the gate runs, **Then** the report is ready and the assessment records why.
5. **Given** a blocked assessment, **When** coordinare processes it, **Then** the open questions, the issue comment and the carry-forward of answers behave exactly as today.

---

### User Story 3 - A card without acceptance criteria gets an outcome-level draft the architect refines (Priority: P2)

A card has a title and a paragraph but no acceptance criteria. The assessor drafts a small set of outcome-level criteria in the same shape the blueprint uses. The architect receives them marked as a draft and refines them into its verification brief, which QA plans from. When the card already has criteria, the assessor's draft is discarded by code, so the human's criteria are never rewritten by the product judgement.

**Why this priority**: Cards without criteria are common on the live board, and QA's plan step needs criteria from somewhere. But the pipeline works without this story: the architect can still derive criteria itself.

**Independent Test**: Run the gate with a card that has criteria and a model draft: `criteria` is empty and `criteria_source` is `card`. Run it with a card that has none: `criteria` is the model's draft, bounded, and `criteria_source` is `assessor`.

**Acceptance Scenarios**:

1. **Given** a card with no acceptance criteria, **When** the assessor runs, **Then** the assessment carries between one and eight draft criteria and `criteria_source` is `assessor`.
2. **Given** a card with acceptance criteria, **When** the assessor runs, **Then** the assessment's `criteria` are empty and `criteria_source` is `card`, whatever the model proposed; the card's own criteria reach the architect with the card as today.
3. **Given** an assessment with draft criteria reaches the architect, **When** the architect's intake renders it, **Then** the criteria are labelled as a draft to refine, and the blueprint's criteria are what QA later plans from.

---

### Edge Cases

- **Schema violation twice.** The model returns something that does not validate, and the single reprompt does not fix it. The round fails as a malformed output and the existing bounded retry (spec 098, 119) applies. The lenient "prose means sufficient" fallback is not available under the workflow.
- **The model returns ready with questions.** Questions are dropped and recorded; ready wins. Questions are for the not-ready case only.
- **The model returns not ready with an empty goal.** The schema rejects an empty goal; the reprompt asks for it. A not-ready assessment must still say what the assessor understood.
- **Re-dispatched assessor.** Dispatching the assessor clears any previous assessment on the card. A round that fails to report leaves no assessment behind, and the architect is never dispatched with a stale one.
- **Answered clarifications with empty answers.** A clarification whose answer is blank does not count as answered: it does not raise the round counter and it does not cause a matching question to be dropped.
- **The architect role runs without the workflow.** The assessment is still injected into the architecting payload; the prose architect persona may ignore it. Only the 165 intake renders it.
- **Restart between assessment and architecting.** The assessment is persisted with the card session (schema v18) and restored; snapshots from v17 and earlier load with no assessment.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001** With `workflow: assessor` on the assessor role, the assessing stage MUST run the assessor workflow: intake, assess, gate, report, in that order, advanced by code and never by the model.
- **FR-002** The workflow MUST make exactly one model call per round, plus at most one reprompt on a schema violation, under a fixed completion-token budget.
- **FR-003** The workflow MUST have no command runner and no repository access; it MUST commit nothing and write nothing to the working tree. The assessment file written by today's assessor MUST NOT be written under the workflow.
- **FR-004** The assessment MUST be a bounded, validated record with: `ready`; `goal`; `expected_behavior`; `out_of_scope` (at most 5); `questions` (at most 2); `assumptions`; `criteria` (at most 8, in the blueprint criteria shape); `criteria_source` (`card` or `assessor`); and the carried `clarifications`. Every field is required; unknown fields are rejected.
- **FR-005** `criteria_source` and `clarifications` MUST be set by code, never by the model. When the card has acceptance criteria, `criteria` MUST be empty and `criteria_source` MUST be `card` (the card's own criteria stand and travel with the card as today); the model's draft is discarded. When the card has none, `criteria` MUST be the model's draft and `criteria_source` MUST be `assessor`; a ready assessment MUST then carry at least one draft item, while a not-ready assessment may carry none (there is no settled outcome to write criteria for yet).
- **FR-006** The gate MUST keep at most two questions, keeping the first two and recording the rest as dropped.
- **FR-007** The gate MUST drop any question that matches an answered clarification (case-folded, punctuation stripped, then equal or with at least 60 percent token overlap) and MUST record the existing answer in `assumptions`.
- **FR-008** When the card already has two or more answered clarification rounds, or when no usable question remains after FR-006 and FR-007, the gate MUST set `ready` true and MUST move every remaining question into `assumptions` phrased as a decision. A card therefore blocks for clarification at most twice.
- **FR-009** A clarification counts as answered only when its answer is non-blank.
- **FR-010** The assessment MUST NOT carry dependencies; the schema rejects the field. Dependency detection stays with spec 046 in coordinare.
- **FR-011** When the assessment is ready, the performer MUST report `assessment_complete` with the assessment as its report. When not ready, it MUST report `blocked` with the gate's questions.
- **FR-012** Coordinare MUST record the assessment on the card session when the assessing stage reports `assessment_complete` with a valid assessment, MUST persist it (schema v18, backward compatible: earlier snapshots load with no assessment), and MUST clear it whenever the assessor is dispatched.
- **FR-013** Coordinare MUST inject the assessment into the architecting dispatch payload and into no other dispatch. The payload field MUST be registered in the dispatch-payload contract.
- **FR-014** The spec-165 architect intake MUST render the assessment, when present, as the first section the architect reads: goal, expected behaviour, out-of-scope items, assumptions, clarifications, and the draft criteria labelled as a draft to refine into the verification brief.
- **FR-015** The blocked path MUST be unchanged: open questions on the card, the issue comment, and the carry-forward of answers into the next assessor run behave exactly as today.
- **FR-016** Under the workflow, unparseable or invalid model output MUST fail into the existing malformed-output retry path. The lenient fallback that treats prose as sufficient MUST NOT apply.
- **FR-017** Without `workflow: assessor`, the assessing stage MUST behave byte for byte as today, including the committed assessment file.
- **FR-018** Every step MUST log its duration, and the model call its elapsed time and completion tokens, in the same events specs 164 and 165 emit.
- **FR-019** Every gate rule MUST be a pure function with its own test, and each rule test MUST be shown to fail under a mutation of the rule it guards.

### Key Entities

- **Assessment**: the structured product reading of one card: readiness, goal, expected behaviour, out-of-scope items, questions, assumptions, criteria with their source, carried clarifications, and an `assessment_hash`. Lives on the card session; produced by one assessor round; replaced by the next.
- **Gate record**: what the gate changed in the model's answer: questions dropped by cap, questions dropped as already answered, questions turned into assumptions by the round rule. Travels in the report for logging and the eval; not consumed by other roles.
- **Clarification round**: one question asked of the human and its answer. Answered rounds are counted from the card's clarification history; the count drives FR-008.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001** An assessor round on the live fleet completes in under five minutes at the 90th percentile including container start, measured over the first ten live rounds after enablement.
- **SC-002** Zero assessor-authored commits on any card branch once the workflow is enabled.
- **SC-003** Zero re-asked questions: across a card's clarification rounds, no question posted to the human matches an earlier answered one under the FR-007 measure.
- **SC-004** No card is blocked for clarification more than twice.
- **SC-005** Every architecting dispatch that follows a workflow assessment carries that assessment, and the architect intake renders it, on every fixture and on live rounds.
- **SC-006** The three eval fixtures (`clear`, `ambiguous`, `answered`) pass deterministically with the stubbed model in CI, and are runnable live through the gateway as a rate to read.

### Performance budgets (Constitution IV, provisional until measured live)

| Step | Budget | Source |
| --- | --- | --- |
| Assess call | 3000 completion tokens, one reprompt | FR-002 |
| Whole round | under 5 minutes p90 including container start | SC-001; replaced once ten live rounds are logged |

## Assumptions

- The card's clarification history (question and answer pairs) is available to the dispatch and is the source of the answered-round count.
- The architect intake from spec 165 is the only consumer that renders the assessment; the prose architect persona is not changed to read it.
- The 60 percent token-overlap measure (shared tokens over the smaller token set, after case folding and punctuation stripping) is one small shared pure helper in the workflow layer; QA has no such matcher today, so nothing is reused.
- "Too thin" acceptance criteria are not judged. The assessor drafts only when the card has none.

## Rollout

Default off. Enabled per symphony by setting `workflow: assessor` on the assessor role after the four fixtures pass stubbed and the `clear` and `ambiguous` fixtures have each been run once live in a performer container with round durations recorded against SC-001. The performer images must be rebuilt (base, full, extra) before enabling.
