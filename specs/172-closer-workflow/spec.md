# Feature Specification: Closer Workflow with Thread Resolution Decided by Code

**Feature Branch**: `172-closer-workflow`
**Created**: 2026-09-07
**Status**: Draft
**Input**: User description: "Extend the spec-164 role-workflow layer to the closer (closing_review) performer: the review threads are fetched and classified by code, only genuinely ambiguous threads reach one schema-guarded model call, every judgement must quote the comment that justifies it, the threads that are addressed are resolved through the existing path, the verdict is derived by code, and a clean card makes no model call at all. Default off via workflow: closer."

## Problem

The closer shares the reviewer's code path. Its persona says the substantive review is done and its only job is to confirm that every open review thread has been resolved, then it forbids re-reviewing, forbids new feedback, and forbids gating on CI. Everything it is allowed to decide is a boolean GitHub already stores: `isResolved` per thread. Yet the stage spends a full model turn re-reading the PR, and on the live fleet it has both approved without checking and rejected on grounds the persona forbids. The one judgement worth making, whether a thread nobody marked resolved was in fact answered in a reply, is buried in that turn with nothing checking the answer against what the thread actually says.

## Goals

- A card whose threads are all resolved passes with no model call, in seconds.
- Threads are classified by code from what GitHub reports; only the genuinely ambiguous ones reach the model.
- Every model judgement is checked against the thread's own comments and discarded when it cannot be.
- The threads that are addressed are resolved through the existing path, with the reason on the record, so a human opens a clean PR.
- The verdict is derived by code, and CI is never gated here (spec 064 owns it).
- Without the workflow flag the stage behaves byte for byte as today.

## Non-goals

- Re-reviewing the diff, or raising anything no earlier stage raised.
- Gating on remote CI, which spec 064's rollup gate owns.
- Changing which stage follows the closing review, or the human approval that follows it.
- Resolving threads on a card the closer rejects.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - A card whose threads are all resolved passes immediately (Priority: P1)

Every review thread on the PR is marked resolved. The workflow fetches them, classifies them, makes no model call, posts one review, and reports approved.

**Why this priority**: this is the common case and the whole reason the stage should be cheap.

**Independent Test**: run the `clean` fixture; zero model calls, verdict approved, one COMMENT review, nothing resolved.

**Acceptance Scenarios**:

1. **Given** every thread reports `isResolved`, **When** the workflow runs, **Then** the verdict is approved, no model call was made, and one review was posted.
2. **Given** the PR has no review threads at all, **When** the workflow runs, **Then** the verdict is approved with an empty thread list.

---

### User Story 2 - A thread answered in a reply is resolved, with the quote on the record (Priority: P1)

A reviewer asked for a change; the implementer replied explaining the fix; nobody marked the thread resolved. Code classifies it as answered, the model judges it addressed and quotes the reply, the gate finds that quote in the thread, code resolves the thread, and the card passes.

**Why this priority**: this is the only judgement the stage adds, and an unchecked judgement here silently closes real feedback.

**Independent Test**: run the `answered` fixture; one model call, the thread resolved through the injected resolver, the quote on the record, verdict approved.

**Acceptance Scenarios**:

1. **Given** an unresolved thread whose last comment is by someone other than the raiser and postdates the raising comment, **When** the workflow runs, **Then** exactly that thread is sent to the model.
2. **Given** the model answers addressed with a quote that appears in the thread, **When** the gate runs, **Then** the thread is resolved and the quote is on the record.
3. **Given** the model answers addressed with a quote that appears nowhere in the thread, **When** the gate runs, **Then** the judgement is discarded, the thread stays open, and the verdict is changes requested.
4. **Given** the model answers not addressed, **When** the gate runs, **Then** the thread stays open and its reason is on the record.

---

### User Story 3 - An unanswered thread blocks the hand-off (Priority: P1)

A thread has one comment and no reply. Code classifies it as open without asking the model, the verdict is changes requested, its path and line are relayed, and nothing is resolved.

**Independent Test**: run the `open` fixture; the thread is never sent to the model, verdict changes requested, no resolution call, the comment names the thread.

**Acceptance Scenarios**:

1. **Given** an unresolved thread whose only comment is the raiser's, **When** the workflow runs, **Then** it is classified open, no model call is made for it, and the verdict is changes requested.
2. **Given** the card has already been through the configured number of closing cycles, **When** the verdict is changes requested, **Then** the stage blocks as it does today.

---

### User Story 4 - A thread whose code moved is resolved by rule (Priority: P2)

A thread is unresolved but GitHub reports it outdated: the lines it pointed at have changed. Code treats that as addressed by the commit, resolves it with that reason, and never asks the model.

**Independent Test**: run the `outdated` fixture; zero model calls, the thread resolved with reason `outdated`, verdict approved.

**Acceptance Scenarios**:

1. **Given** an unresolved thread reported outdated, **When** the workflow runs, **Then** it is resolved by rule and the record names the rule.
2. **Given** an outdated thread whose last comment says the concern still stands, **When** the workflow runs, **Then** it is still resolved by rule; re-litigating it is the reviewer's job, not the closer's.

---

### Edge Cases

- Fetching the threads fails, or the PR URL is missing: environment hold, nothing resolved, nothing approved.
- Resolving a thread fails: the run does not approve; the verdict becomes an environment hold naming the thread, because a card cannot pass with a thread the closer believed resolved but did not close.
- More than 100 threads: the fetch pages until exhausted, and the record says how many were read.
- The model returns a judgement for a thread that was not sent: discarded and recorded.
- The model returns fewer judgements than threads sent: the missing ones stay open.
- A thread whose last comment is the raiser's own follow-up: not answered, it is open.
- The review post fails: environment hold with the error, nothing resolved (the post happens before any resolution).

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001** With `workflow: closer` on the closer role, the closing_review stage MUST run intake, classify, judge, gate, act, report, in that order, advanced by code.
- **FR-002** Intake MUST fetch every review thread of the PR with its id, resolved and outdated flags, path, line, and every comment's author, body and timestamp, paging until exhausted, plus the PR head SHA.
- **FR-003** Classification MUST be pure rules: `resolved` when GitHub reports it resolved; `stale` when it is unresolved and outdated; `answered` when it is unresolved, not outdated, and its last comment is by an author other than the thread's first commenter and is not earlier than that first comment; `open` otherwise.
- **FR-004** The model MUST be called at most once, only when the answered set is non-empty, with those threads only, and MUST return per thread `addressed` with a verbatim quote from that thread, or `not_addressed` with a reason. The schema MUST reject a verdict field.
- **FR-005** The gate MUST discard any judgement whose thread was not sent, and any `addressed` whose quote does not appear in that thread's comment bodies; a discarded judgement leaves the thread open and MUST be recorded.
- **FR-006** The verdict MUST be derived by code: any thread still open is changes requested; none is approved. Remote CI MUST NOT be consulted.
- **FR-007** The workflow MUST post exactly one GitHub review (`COMMENT`, since only humans approve formally) naming what was resolved and what remains, before resolving anything.
- **FR-008** After a passing verdict the workflow MUST resolve exactly the `stale` threads and the `addressed` threads, each with its reason on the record; on a failing verdict it MUST resolve nothing.
- **FR-009** A failure to fetch, to post, or to resolve MUST end the round as an environment hold naming the failure; the stage MUST NOT report approved when any intended resolution failed.
- **FR-010** The performer MUST report `approved` or `changes_requested`, the statuses coordinare handles today, with the comments naming the open threads by path and line; the closing cycle limit MUST apply as today.
- **FR-011** Without `workflow: closer` the closing_review stage MUST behave byte for byte as today, including the shared reviewer code path.
- **FR-012** Every step MUST log its duration and any model call its elapsed time and completion tokens, in the events specs 164 to 171 emit.
- **FR-013** Every classification and gate rule MUST be a pure function with its own test, shown to fail under a mutation.
- **FR-014** A card whose threads are all resolved or stale MUST make zero model calls.

### Key Entities

- **Thread**: id, path, line, resolved, outdated, comments (author, body, created_at), first author, last author.
- **Classification**: one of resolved, stale, answered, open, with the rule that decided it.
- **Judgement**: thread id, addressed, quote, reason, accepted, discard reason.
- **ClosingRecord**: head SHA, threads read, classifications, judgements, resolved ids with reasons, open threads, verdict, hold reason, posted review URL, workflow metrics.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001** Zero approvals with an open thread, across all fixtures and the first ten live rounds.
- **SC-002** Zero model calls on cards whose threads are all resolved or stale.
- **SC-003** Every resolved thread on the record carries either the outdated rule or a quote found in that thread.
- **SC-004** A closing round completes in under two minutes at the 90th percentile including container start when a model call is needed, and under thirty seconds when none is.
- **SC-005** The five fixtures (`clean`, `answered`, `open`, `outdated`, `hallucinated_quote`) pass deterministically in CI, and `clean` and `answered` pass live through the gateway.

### Performance budgets (Constitution IV, provisional until measured live)

- One judgement call at 4000 completion tokens plus one reprompt, at most 20 threads per call.
- Thread fetch paged at 100 per request, at most 5 pages.

## Assumptions

- The GraphQL thread query and the per-thread resolve mutation already used by `resolve_pr_review_threads` are extended in place, so the reviewer's existing behaviour is unchanged.
- Coordinare's handling of `approved` and `changes_requested` from the closing_review stage needs no change.
- Spec 064's rollup gate runs after approval and remains the only CI gate.

## Rollout

Default off. Enable per symphony with `workflow: closer` on the closer role. The prose path returns by removing the line.
