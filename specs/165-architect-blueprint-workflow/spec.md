# Feature Specification: Architect Role Workflow with a Blueprint Hand-off

**Feature Branch**: `165-architect-blueprint-workflow`
**Created**: 2026-09-06
**Status**: draft, awaiting review
**Related**: spec 164 (role-workflow layer, QA as first consumer), #256 (documenter as the single writer), #263 (infrastructure failures), #264 (QA budgets sized from the first live run)
**Input**: "Architect role workflow with a blueprint hand-off. Extend the spec-164 role-workflow layer to the architect performer: a bounded intake, survey, blueprint, size, report workflow that produces one structured blueprint and commits nothing. The blueprint lives in coordinare state only, travels in dispatch payloads like qa_findings, and is sliced per reader. Complexity is decided by code from the blueprint's size. Default-off via workflow: architect."

## Problem

The architect is a planning role with a prose contract: write two markdown documents (plan and tasks) that the performer commits under `docs/cards/<id>/` and the implementer reads milestone by milestone. On the live website run of 2026-09-06 that contract failed in three ways, all visible in one architect's own session log:

- **It did implementer work.** 55 tool calls in 31 minutes: reading was fine, but then it wrote a migration and a spec, ran `bundle install`, polled for it, and started "checking release-versioning conventions before committing". The persona says breadth over depth, stop and write the plan. Nothing enforced it.
- **Context grew until every call was slow.** Raw command output accumulated to roughly 40k of a 66k token context. Prefill on the shared model costs about 40 s at that size (measured: 12k tokens 19 s, 50k 39 s, 104k 46 s), so by turn 30 each call paid 40 s before generating anything. The median call was 17 s early and 71 s at the 90th percentile late.
- **It never produced the plan.** Two architect rounds that day ran to the 7200 s ceiling and were reaped with no plan committed. One of them was a single hung call (fixed separately in #265); the other was drift.

Meanwhile the documents the architect does produce duplicate what other roles write. The assessor writes an assessment, the architect a plan and tasks, the implementer commits "mark milestone complete" notes, and the documenter maintains the wiki from the diff. Each restart of a card re-writes some of these. The repository accumulates near-duplicate prose that drifts.

## Goals

1. Bound the architect: a fixed sequence of steps in which the model proposes and code executes, with a hard tool budget, read-only access, and no writes to the repository.
2. One output, three readers. The architect produces a single structured blueprint. The implementer receives exactly what to implement, the documenter exactly what to document, QA exactly what to verify. Acceptance criteria are made testable once, by the role that has read the code.
3. No duplicated prose in the repository. The blueprint is carried in coordinare state and dispatch payloads, never committed. Only the readers' products land: code and tests from the implementer, documentation from the documenter.
4. Ceremony follows size. Code decides, from the blueprint, whether a card gets a single implementer turn or a milestone loop, and whether a documenter runs at all.
5. The documenter can start as soon as the blueprint exists, concurrently with implementation, because the two write to disjoint parts of the repository.

## Non-goals

- Changing the assessor. It remains the gate that says the card is specified well enough to start.
- Replacing the implementer's or documenter's harness. They stay persona-driven agents; only their inputs change.
- Migrating the other roles (reviewer, security, closer) to workflows. This spec adds the second consumer of the 164 layer and its first cross-role hand-off; each further role is its own spec.
- Retiring the prose path. It stays the default until a role opts in.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - A large card gets a bounded plan and three tailored briefs (Priority: P1)

An operator sets `workflow: architect` on the architect role. A schema-plus-interfaces card is dispatched. The architect reads the card, the assessment and a bounded slice of the repository, then produces a blueprint with four milestones, a data model section, two interfaces, three risks, five testable criteria and two documentation topics. It commits nothing. The implementer is dispatched with the implementation brief and works one milestone per turn. The documenter is dispatched at the same time with the documentation brief and updates the wiki. QA later plans from the five criteria.

**Why this priority**: this is the case that burned two hours per round on the live run. Everything else in the spec is in service of it.

**Independent Test**: run the architect workflow against a fixture card of this shape with a stubbed model that answers the survey and blueprint prompts. Assert: no file written or command with side effects executed; a blueprint that validates; the three slices contain exactly their fields; size classified as large.

**Acceptance Scenarios**:

1. **Given** a card whose assessment names three affected modules, **When** the architect workflow runs, **Then** the survey issues at most the configured number of read-only commands, each output is truncated to the configured length, and no command that writes, installs or runs tests is executed even if the model asks for one.
2. **Given** the model returns a blueprint that fails validation, **When** the schema guard reprompts once, **Then** a valid blueprint is returned or the run ends with a schema violation that names the fields, never with an empty plan committed.
3. **Given** a valid blueprint with two documentation topics, **When** coordinare lifts it, **Then** the implementer's dispatch payload carries the implementation brief and not the documentation brief, the documenter's carries the documentation brief and not the milestones, and QA's carries the criteria.
4. **Given** the architect finished, **When** the card's branch is inspected, **Then** it has no commit authored by the architect stage.

---

### User Story 2 - A small card gets a small plan and no ceremony (Priority: P1)

A one-line copy fix is dispatched. The blueprint has one milestone, no data model changes, no interfaces, two criteria and no documentation topics. Code classifies it small. The implementer is dispatched for a single turn with no partial-progress loop. No documenter runs. QA verifies two criteria.

**Why this priority**: the original fear was that a workflow adds ceremony to every card. It must remove ceremony from small ones or it is not worth having.

**Independent Test**: run the workflow against the trivial fixture; assert size small, implementer brief marked single-turn, documentation brief empty, documenter not dispatched.

**Acceptance Scenarios**:

1. **Given** a blueprint with one milestone and no data model or interface changes, **When** it is sized, **Then** the size is small and the implementer dispatch omits the milestone loop instructions.
2. **Given** an empty documentation brief, **When** the lifecycle advances past architecting, **Then** no documenting side run is dispatched and the card proceeds to implementing only.

---

### User Story 3 - The documenter runs alongside implementation without conflict (Priority: P2)

For a card with documentation topics, the documenter and implementer run concurrently. The documenter commits only under the documentation tree. Both push to the card's branch. Neither overwrites the other's work.

**Why this priority**: it is the part of #256 this spec realises, and the part most likely to surface mechanical conflicts, so it ships behind the same flag but is independently testable.

**Independent Test**: simulate two performers committing to disjoint paths on one branch in sequence with a fetch-and-rebase before each push; assert both commits land and the tree is the union.

**Acceptance Scenarios**:

1. **Given** the implementer pushed while the documenter was working, **When** the documenter pushes, **Then** it rebases onto the new head first and its push is accepted.
2. **Given** the documenter attempts to modify a file outside the documentation tree, **When** it commits, **Then** the commit is rejected by the performer and the run reports the violation.
3. **Given** implementation changed something the blueprint described differently, **When** the end-of-lifecycle documenting pass runs (unchanged, SHA-gated), **Then** it reconciles the wiki with the code as it does today.

---

### User Story 4 - QA plans from the verification brief (Priority: P2)

QA (spec 164 workflow) receives the verification brief. Its plan step turns the criteria into checks directly instead of deriving criteria from the card text.

**Why this priority**: consistency between "done when" and "verified when" is the reason the criteria live in the blueprint.

**Independent Test**: run the QA plan step with a payload carrying a verification brief and a card whose text says something different; assert the plan's criteria come from the brief. Run it without a brief; assert the existing behaviour.

**Acceptance Scenarios**:

1. **Given** a verification brief with three criteria, **When** the QA plan step runs, **Then** the plan contains exactly those three criteria with their surfaces and expected observations, and the model is not asked to invent criteria.
2. **Given** no verification brief (prose path or a pre-165 card), **When** the QA plan step runs, **Then** it behaves exactly as before this feature.

### Edge Cases

- The model's survey asks for a command that writes, installs, runs tests or leaves the repository (`bundle install`, `rails db:migrate`, `rm`, `curl`): the command is refused, the refusal is recorded, and the survey continues with the budget consumed by one.
- The blueprint is valid but hollow (zero milestones): the run fails with a named reason; nothing is dispatched downstream; coordinare treats it as a performer error, not as a question for a human.
- The card is re-dispatched to the architect after a bounce: the previous blueprint is replaced, not merged, and downstream slices are re-derived.
- A restart of coordinare mid-lifecycle: the blueprint is persisted with the card's session and survives, exactly as `qa_findings` does.
- A blueprint larger than the dispatch payload comfortably carries: milestones, criteria and topics are bounded by count and per-item length in the schema, so the payload has a known maximum size.
- The role is configured with `workflow: architect` but the card also has a hand-written plan under `docs/cards/<id>/`: the blueprint wins for dispatch; the file is left untouched and not read.

## Requirements *(mandatory)*

### Functional Requirements

**The workflow**

- **FR-001** The architect workflow runs entirely inside the performer behind the spec-164 layer, selected by `workflow: architect` on the role, default off. With the key absent the prose path is byte-for-byte unchanged.
- **FR-002** Steps run in a fixed order: intake, survey, blueprint, size, report. No step may be skipped or repeated by the model; only code advances the sequence.
- **FR-003** Intake assembles, without a model call: card title and body, acceptance criteria, the assessor's assessment when present, clarifications answered by humans, and the repository's own agent instructions when present.
- **FR-004** Survey is model-proposed and code-executed. The model names what it needs to inspect; code runs only read-only commands from an allow-list (file listing, file reads, text search, git history and diff). Every command output is truncated to a configured maximum. The survey has a hard budget of commands; when it is spent the survey ends. Refused commands consume budget and are reported.
- **FR-005** Blueprint is a single model call (one truncation retry and one schema reprompt as in 164) producing a document validated against a schema: milestones (goal, scope, done when), affected modules, data model changes, interfaces, risks, criteria (surface, action, expected observation), documentation topics (topic, location, what to say). Every list has a maximum length and every string a maximum length.
- **FR-006** Size is decided by code from the blueprint alone. The rule is a pure function with documented thresholds (for example: one milestone and no data model or interface change is small). The model does not choose the size.
- **FR-007** The architect writes nothing to the working tree and creates no commits. Any attempt is a defect, and the report records the write-free property as an executed check.
- **FR-008** The report carries the blueprint and its size, plus workflow metrics (calls, retries, reprompts, per-step durations) in the same shape 164 reports for QA.

**The carrier**

- **FR-009** Coordinare lifts the blueprint from the architect's report into the card's persisted session, replacing any previous blueprint for that card. It is registered in the dispatch-payload contract with a contract test, alongside `qa_findings`.
- **FR-010** Dispatch slices the blueprint per reader: the implementer receives the implementation brief (milestones, modules, data model, interfaces, risks, size); the documenter receives the documentation brief (topics, plus the summary needed to write them); QA receives the verification brief (criteria). No reader receives another reader's slice.
- **FR-011** When the workflow is active, the architect stage produces no `plan.md` or `tasks.md`, and the implementer persona's per-turn procedure reads milestones from its brief instead of from those files. When the workflow is inactive both behave as today.
- **FR-012** The implementer persona states that it writes code and tests only and does not create or edit documentation. A documentation change in an implementer commit is reported as a finding by the review stage.

**Size-driven ceremony**

- **FR-013** A small blueprint dispatches the implementer for a single turn: the partial-progress loop is not offered and the brief says the whole card is one unit of work.
- **FR-014** An empty documentation brief means no documenting side run is dispatched for that card. A non-empty brief dispatches it (FR-015).

**The documenter side run**

- **FR-015** When the documentation brief is non-empty, documenting is dispatched immediately after architecting and runs concurrently with implementing. It writes only under the documentation tree; the performer rejects commits touching other paths. It fetches and rebases before every push.
- **FR-016** The existing end-of-lifecycle documenting pass is unchanged: SHA-gated, reconciling the wiki with what was actually implemented.

**QA consumption**

- **FR-017** The QA workflow's plan step uses the verification brief's criteria when present, without asking the model to derive criteria; absent a brief it behaves exactly as before.

**Observability and failure**

- **FR-018** Every step logs its duration and every model call its elapsed time and completion tokens, in the same events 164 emits, so budgets can be measured from live logs.
- **FR-019** A hollow or invalid blueprint ends the run as a performer error with a reason that names the problem. It is never surfaced to humans as a question.

### Key Entities

- **Blueprint**: the architect's single output. Milestones, modules, data model changes, interfaces, risks, criteria, documentation topics, plus size. Bounded in count and length. Persisted with the card's session; never committed.
- **Brief**: a reader-specific projection of the blueprint. Three kinds: implementation, documentation, verification. Derived by code at dispatch; never stored separately.
- **Size**: small or large, a pure function of the blueprint, deciding the implementer's turn shape and whether a documenter runs.
- **Survey budget**: the count of read-only commands the architect may run, and the per-output truncation length.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001** An architect round on the live fleet completes in under 20 minutes at the 90th percentile, measured from the first live ten cards after enablement, against the 60 to 120 minute rounds observed before.
- **SC-002** Zero architect-authored commits on any card branch once the workflow is enabled.
- **SC-003** The survey never exceeds its command budget, and no refused command is ever executed, on every fixture and every live run (checked by the executed-check record, not by trust).
- **SC-004** On the scenario eval, the trivial fixture is sized small and dispatches no documenter; the schema fixture is sized large with at least three milestones, at least one data model change and at least one documentation topic; the mid fixture is sized large with two to four milestones, no data model change and exactly one documentation topic. Scoring is qualitative, as in 164, and not a CI gate.
- **SC-005** For cards with a blueprint, QA's planned criteria equal the blueprint's criteria in count and text on every eval fixture.
- **SC-006** Turning the flag off restores the prose path with no behavioural difference, verified by the existing 164 no-workflow tests extended to the architect role.

### Performance budgets (Constitution IV, provisional until measured live)

| Budget | Value | Basis |
| --- | --- | --- |
| Survey command budget | 12 commands, 4000 characters each | keeps the blueprint call's context under roughly 20k tokens, where prefill measured about 20 s |
| Blueprint model call | 8000 tokens, one doubled retry | the 164 plan budget after #264 |
| Model read timeout | 900 s | shared with 164 |
| Whole round | under 20 minutes p90 | SC-001; replaces the provisional value once ten live rounds are logged |

## Assumptions

- The performer's push path can fetch and rebase before pushing; if it cannot today, adding it is in scope for FR-015.
- Slot pools are per stage, so a documenting side run and an implementing stage can hold slots at the same time under the existing concurrency limit.
- The documentation tree is `docs/` at the repository root for the current symphonies; the path is configurable per symphony.
- The existing scenario-eval harness from 164 can host the three architect fixtures with a stubbed model.

## Rollout

Default off. Enable per role with `workflow: architect` after the scenario eval passes and one live card of each size has been observed. The documenter side run is part of this spec and the same flag; if live runs show branch conflicts the side run can be disabled independently while the blueprint hand-off stays on.
