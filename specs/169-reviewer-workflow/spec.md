# Feature Specification: Reviewer Workflow with Verified Findings and a Structured Hand-off

**Feature Branch**: `169-reviewer-workflow`
**Created**: 2026-09-07
**Status**: Draft
**Input**: User description: "Extend the spec-164 role-workflow layer to the reviewer performer: a bounded review over the PR diff plus a read-only survey of the surrounding code, structured findings whose anchors are verified against the diff, prior feedback dispositioned by rule, a coverage rule before any approval, the verdict derived by code (any finding blocks, none approves), one GitHub review posted with inline comments, and the findings lifted into coordinare state and handed to the implementer as a repair brief. Default off via workflow: reviewer."

## Problem

The reviewer is one prose turn over a sanitized, size-capped PR diff. The persona asks it to read surrounding code, check conventions and tests, disposition earlier feedback, and return `approved` plus free-text comments anchored to `path:line`. Nothing checks that a comment points at a line in the diff, that every open comment from an earlier round was addressed, or that the model read anything beyond the injected text; a truncated diff silently hides the rest of a large PR. On the live fleet reviews have rejected PRs with "no code changes were supplied" and have approved without looking. The binary rule (a finding blocks, only a human approves) lives in persona text, and the findings reach the implementer as relayed prose.

## Goals

- The review is a code-driven workflow: intake, a bounded read-only survey of the checked-out branch, one schema-guarded findings call, a gate of pure rules, one posted GitHub review, a report.
- Every finding is anchored to a line the reviewer can prove it saw: in the diff, or in a file the survey opened. Findings that point at nothing are dropped, and the model is asked once to re-anchor.
- Every open comment from an earlier round is dispositioned; a missing disposition becomes a finding by rule.
- An approval is possible only when every changed file was read, in the diff or by the survey. A review that did not look cannot approve.
- The verdict is derived by code from the surviving findings. The model never states it.
- The findings are posted as one GitHub review with inline comments and lifted into coordinare state, then handed to the implementer as a structured repair brief that selects a repair lane in the spec-167 workflow.
- The reviewer commits nothing and its toolkit has no write primitive.
- Default off. A role without `workflow: reviewer` behaves exactly as today.

## Non-goals

- Approving on behalf of a human. The posted review is `REQUEST_CHANGES` or `COMMENT`; only a human approves and merges (the review design and spec 128 remain).
- Resolving review threads. The performer names fixed comments so a human can resolve them.
- The security role. It can reuse this workflow later with its own category set and the spec-083 scanner findings as an input; the categories are a parameter for that reason, but security is not changed here.
- Replacing coordinare's feedback-cycle exhaustion. Rounds stay bounded by the existing rule.
- Editing code. A review that can edit is a review that will edit.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - A PR with real problems gets anchored, actionable findings (Priority: P1)

A PR arrives at the reviewing stage. The workflow parses the diff into changed files and hunks, lets the model survey the surrounding code with read-only commands under a budget, and asks for findings. Each finding names a file and line the reviewer saw, a category from a fixed set, the problem, why it blocks, and the offending line. Code drops any finding that points at nothing and asks once for re-anchored findings. The surviving findings are posted as one review with inline comments and the performer reports changes requested. The implementer's next dispatch carries the findings and repairs them one group at a time.

**Why this priority**: Findings the implementer can act on, anchored where a human can see them, are the reviewer's whole output.

**Independent Test**: Run the `findings` fixture with a stubbed model: the posted review has one inline comment per finding on its line; the report's findings all anchor inside the diff hunks; the performer status is changes requested; a lifted copy reaches an implementer dispatch and selects the repair lane.

**Acceptance Scenarios**:

1. **Given** a diff with two hunks and a model that returns three findings, one of them at a line outside every hunk and not in any surveyed file, **When** the gate runs, **Then** the unanchored finding is dropped and recorded, one reprompt lists it, and the posted review carries only anchored findings.
2. **Given** surviving findings, **When** the review is posted, **Then** it is one `REQUEST_CHANGES` review whose inline comments sit on the findings' lines, and the performer reports `changes_requested` with the findings as its report.
3. **Given** a lifted set of findings, **When** the implementer is dispatched next, **Then** its payload carries the findings and the spec-167 plan selects the repair lane.
4. **Given** the role has no `workflow` setting, **When** the reviewer runs, **Then** the dispatch payload, persona and behaviour are byte for byte what they are today.

---

### User Story 2 - A clean PR is approved only after the reviewer has read it all (Priority: P1)

A small, clean PR arrives. The diff fits entirely in the injected text. The model surveys, returns no findings, and every prior comment is dispositioned fixed or there were none. Code confirms every changed file was covered and derives approval: a `COMMENT` review is posted saying the automated review found nothing, and the performer reports approved. On a large PR whose injected diff was truncated, the survey must open the files the diff cut off; if after one coverage pass files remain unread, the run ends as a hold naming them, not as an approval.

**Why this priority**: Approval is the dangerous verdict. It must be earned by coverage, not asserted.

**Independent Test**: Run the `clean` fixture: approval with full coverage recorded. Run the `truncated` fixture with a survey that opens the cut files: approval. Run it with a survey that does not: environment hold naming the unread files, no approval, nothing posted as approval.

**Acceptance Scenarios**:

1. **Given** no findings and every changed file present in the diff or opened by the survey, **When** the gate runs, **Then** the verdict is approved, a `COMMENT` review is posted, and the report lists the coverage.
2. **Given** a truncated diff whose cut files the first survey did not open, **When** the coverage pass runs, **Then** the model is given the exact list of unread files and one more survey turn.
3. **Given** files still unread after the coverage pass and no findings, **When** the gate runs, **Then** the run ends as an environment hold naming the files and no review is posted.
4. **Given** files unread and at least one anchored finding, **When** the gate runs, **Then** the finding stands and the verdict is changes requested; coverage never suppresses a real finding.

---

### User Story 3 - Earlier feedback is never lost (Priority: P1)

A second review round. Coordinare relays the open comments from the first round. The model must disposition each: fixed, or not fixed with a new finding. Any comment it forgets becomes a finding by rule, anchored to the comment's own file and line, so the implementer sees it again. Comments dispositioned fixed are named in the review body for the human to resolve their threads.

**Why this priority**: Unaddressed prior feedback that silently disappears is how a card ships with a known problem.

**Independent Test**: Run the `prior_feedback` fixture with two open comments and a model that dispositions one: the other becomes an `unaddressed_feedback` finding by code and the verdict is changes requested; the posted body names the fixed one.

**Acceptance Scenarios**:

1. **Given** open comments from an earlier round, **When** the model returns dispositions for some, **Then** every missing one is added as a finding anchored to the comment's path and line.
2. **Given** a comment dispositioned fixed, **When** the review is posted, **Then** its identifier is named in the body and no thread is resolved by the performer.

---

### User Story 4 - What the implementer was told not to do is checked by code (Priority: P2)

When the dispatch carries a spec-165 implementation brief, the implementer was told to write code and tests only. If the diff touches the documentation tree, code adds a `documentation_by_implementer` finding without asking the model.

**Why this priority**: Spec 165's FR-012 promised this finding; today it is a persona sentence.

**Independent Test**: Run the `findings` fixture with a brief and a diff touching `docs/`: the finding is present even when the model returned none.

**Acceptance Scenarios**:

1. **Given** an implementation brief and a changed path under the documentation tree, **When** the gate runs, **Then** a `documentation_by_implementer` finding anchored to that file is present regardless of the model's output.

---

### Edge Cases

- **Diff unavailable.** Coordinare could not fetch the diff: the run ends as an environment hold before any turn.
- **More than 30 findings.** Schema violation; one reprompt asks for the blocking ones only; a second miss is the malformed-output path.
- **Findings at a deleted line.** Anchors are checked against the new-side line numbers of hunks; a finding about removed code anchors to the hunk header line and says so.
- **Survey refusals.** Refused commands are recorded as in spec 165 and never run.
- **Review post fails** (API error): environment hold; nothing lifted; the findings stay in the report for the retry.
- **Model returns a verdict field.** The schema rejects it; the verdict is code's.
- **Re-dispatched reviewer.** Dispatching the reviewer clears the previous lifted findings; a new round replaces them.
- **Implementer repair lane with a finding whose file no longer exists.** The finding is carried verbatim; the implementer's persona says to treat a stale anchor as already fixed and say so.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001** With `workflow: reviewer` on the reviewer role, the reviewing stage MUST run intake, survey, findings, gate, post, report, in that order, advanced by code.
- **FR-002** Intake MUST parse the injected diff into changed files with hunks and new-side line ranges, record whether the injected diff was truncated, normalise the relayed open comments to id, path, line and body, and carry the implementation brief and the CI state when present.
- **FR-003** The survey MUST run only commands the spec-165 allow-list accepts, under a command and output budget, and MUST record every command and refusal. It MUST track which changed files were opened.
- **FR-004** When the diff was truncated or any changed file was neither fully in the diff nor opened, the workflow MUST run exactly one more survey turn naming the unread files.
- **FR-005** The findings call MUST be one schema-guarded model call with one reprompt: findings (path, line, category from the fixed set, problem, why blocking, evidence) at most 30, and dispositions for the open comments. The schema MUST reject a verdict field.
- **FR-006** The gate MUST admit a changed path only in its hunks or after survey, and an unchanged path only after a successful admitted survey command names it. Every finding must name a changed cause in `introduced_by`; an omitted cause defaults to its own path for legacy changed-path findings only. Evidence must match a line of the diff or surveyed output. Drop and record findings that fail these checks and reprompt once. Unchanged-path findings are posted in the review body (#281).
- **FR-007** The gate MUST add an `unaddressed_feedback` finding for every open comment without a disposition, anchored to the comment's path and line.
- **FR-008** When an implementation brief is present and the diff touches the documentation tree, the gate MUST add a `documentation_by_implementer` finding.
- **FR-009** The gate MUST derive the verdict: any surviving finding is changes requested; none is approved. Approval MUST additionally require full coverage; without it the run MUST end as an environment hold naming the unread files.
- **FR-010** The workflow MUST post exactly one GitHub review: `REQUEST_CHANGES` with one inline comment per finding when findings survive, `COMMENT` with a short body when none do; fixed dispositions MUST be named in the body; no thread MUST be resolved.
- **FR-011** The performer MUST report `changes_requested` with the findings as the report, or `approved` with the report, the same statuses coordinare handles today. A failed post MUST be an environment hold with nothing lifted.
- **FR-012** Coordinare MUST lift the findings into the card session (schema v19, cleared when the reviewer is dispatched) and inject them into the implementer dispatch only, registered in the dispatch-payload contract.
- **FR-013** The spec-167 implementer plan MUST select a repair lane when review findings are present: no red step, one bounded turn per file group of findings carrying them verbatim, milestone tests and the quality set after each turn, then the existing push, CI wait and hand-off, under the implementation attempt cap.
- **FR-014** The reviewer MUST commit nothing and its toolkit MUST have no write primitive; the report MUST record the structural proof as spec 166 does.
- **FR-015** Without `workflow: reviewer` the reviewing stage MUST behave byte for byte as today.
- **FR-016** Every step MUST log its duration and every model call its elapsed time and completion tokens, in the events specs 164 to 167 emit.
- **FR-017** Every gate rule MUST be a pure function with its own test, shown to fail under a mutation.
- **FR-018** The finding categories MUST be a parameter of the workflow so the security role can reuse it with its own set.

### Key Entities

- **Changed file**: path, hunks with new-side line ranges, whether fully present in the injected diff, whether opened by the survey.
- **Finding**: path, line, category, problem, why blocking, evidence, origin (model, rule).
- **Disposition**: prior comment id, fixed or not fixed, linked finding when not fixed.
- **Review record**: survey commands and refusals, coverage per changed file, findings dropped and reprompted, dispositions, verdict, post result. Travels in the report; the findings alone are lifted.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001** Every finding in every posted review anchors to a line the report shows the reviewer saw, on all fixtures and the first ten live reviews.
- **SC-002** Zero approvals without full coverage on all fixtures and live reviews.
- **SC-003** Zero open prior comments left undispositioned across a card's review rounds.
- **SC-004** A review round completes in under ten minutes at the 90th percentile including container start, over the first ten live rounds.
- **SC-005** Every implementer dispatch that follows a changes-requested review carries the findings and runs the repair lane.
- **SC-006** The five fixtures (`clean`, `findings`, `hallucinated_anchor`, `prior_feedback`, `truncated`) pass deterministically in CI and `clean` and `findings` run live.

### Performance budgets (Constitution IV, provisional until measured live)

| Item | Budget | Source |
| --- | --- | --- |
| Survey | 12 commands plus one coverage pass, 4000 chars each | FR-003, FR-004 |
| Findings call | 8000 completion tokens, one reprompt | FR-005 |
| Findings | 30 | FR-005 |
| Round | under 10 minutes p90 | SC-004 |

## Assumptions

- The diff coordinare injects today carries enough structure to recover files and hunks; the sanitizer keeps hunk headers.
- Open comments from earlier rounds are what coordinare already relays to the reviewer.
- The spec-167 repair lane is an addition to that workflow's plan step, not a new workflow.

## Rollout

Default off. Enable per symphony with `workflow: reviewer` after the fixtures pass and the `clean` and `findings` live rounds are recorded. Rebuild the performer images first.
