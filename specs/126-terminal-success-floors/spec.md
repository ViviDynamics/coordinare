# Feature Specification: Terminal-Success Progress Floors

**Feature Branch**: `126-terminal-success-floors`
**Created**: 2026-07-04
**Status**: Draft
**Input**: User description: "Terminal-success progress floors: role-aware verification that performer 'done' claims carry real progress (head-delta floor for implementer/tech_writer, feedback-addressed contract on changes_requested bounces, dispute path)"

## Overview

Coordinare independently verifies performer *failure* claims — the spec-070/072 zero-progress guardrails catch a `blocked` or `partial_progress` report that moved nothing — but performer *success* claims advance the card unconditionally. An implementer bounced with reviewer feedback can commit an unrelated trivial change (or nothing at all), report done, and the card marches through the downstream stages only to receive the identical feedback again: pure thrash that burns the card's content-feedback budget without the work moving. A tech_writer can report documentation committed while having modified zero files, and coordinare records a successful documentation pass that never happened. Nothing checks that the specific feedback a card was bounced with was actually addressed — the CI gate checks only that required checks pass.

This feature adds role-aware progress floors on terminal success: an implementer completing a feedback-driven dispatch must have moved the PR head beyond the commit the feedback was raised against (or explicitly dispute the feedback), feedback items get stable identities with per-item dispositions (addressed / disputed / not addressed), disputes route back to the stage that raised them instead of being silently swallowed, and a documentation pass is only recorded when documentation actually changed. Verdict roles (reviewer, security, qa, closer) are exempt — they legitimately complete without commits. Diagnosed in the 2026-07-03 architectural review (findings 3, 6, 8 of the merged set).

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Implementer "done" after a feedback bounce must move the head (Priority: P1)

A reviewer bounces a card with changes_requested feedback raised against head commit `F`. The implementer is re-dispatched with that feedback. When the implementer reports terminal success, coordinare accepts the completion only if the PR head now differs from `F` — meaning at least one commit landed since the verdict that bounced the card — or the implementer explicitly disputed the feedback (User Story 2). A completion that changed nothing and disputed nothing is treated like the existing zero-progress cases: one re-dispatch with a strengthened directive, then a hold for the operator.

**Why this priority**: This is the thrash-loop closer. Today a no-op "done" advances through up to five downstream stages, collects the same feedback again, and burns one content-feedback cycle per lap — a card can exhaust its budget and enter blocked without the head ever moving.

**Independent Test**: Bounce a card with feedback at head `F`, have the implementer return success without committing, and observe: no stage advance, one strengthened re-dispatch, then (on repeat) a hold with operator notification — and zero content-feedback budget consumed by the loop.

**Acceptance Scenarios**:

1. **Given** a card bounced with feedback raised against head `F`, **When** the implementer reports terminal success and the PR head is now `F'` ≠ `F`, **Then** the completion is accepted and the card advances as today.
2. **Given** a card bounced with feedback raised against head `F`, **When** the implementer reports terminal success, the head is still `F`, and no feedback item was disputed, **Then** the completion is NOT accepted; the implementer is re-dispatched once with a strengthened directive naming the unaddressed feedback.
3. **Given** the strengthened re-dispatch also completes with head still `F` and nothing disputed, **When** coordinare evaluates the completion, **Then** the card is held for the operator with a cause naming the unmoved head and the outstanding feedback (no further automatic re-dispatch).
4. **Given** a first-pass implementer dispatch (no feedback bounce, e.g. initial implementation), **When** it reports terminal success, **Then** existing behaviour applies unchanged — this floor only arms on feedback-driven dispatches.
5. **Given** coordinare cannot resolve the feedback-origin head or the current head, **When** the completion is evaluated, **Then** it is accepted as today (fail-open; never hold a card on missing bookkeeping).
6. **Given** floor-triggered re-dispatches, **When** budgets are accounted, **Then** the loop consumes the bounded zero-progress allowance — not content-feedback cycles and not transient-error cycles.

---

### User Story 2 - Feedback items carry identities and dispositions; disputes go back to the raiser (Priority: P2)

Each feedback item delivered to an implementer on a bounce carries a stable identifier. The implementer's completion enumerates a disposition per item: *addressed* (with a one-line summary of what changed) or *disputed* (with a reason — e.g. "already implemented in `S`", "reviewer misread the diff"). Coordinare treats items with no disposition as not addressed. Disputed items are not silently accepted: they are attached as context to the next run of the stage that raised them, so the disputing claim is adjudicated by the role that made the demand — not by the implementer's own say-so and not by coordinare guesswork.

**Why this priority**: The floor in User Story 1 without a dispute path would trap legitimate "no change needed" cases in the zero-progress loop; the dispute path without identities would be unauditable. Together they convert today's silent pass-through into an explicit, checkable contract.

**Independent Test**: Bounce a card with two feedback items; have the implementer address one and dispute the other without committing for it; observe the completion is accepted (dispute present), the raising stage's next dispatch carries the dispute, and the audit trail shows both dispositions.

**Acceptance Scenarios**:

1. **Given** a bounce delivering feedback items A and B, **When** the implementer completes with A addressed (head moved) and B disputed with a reason, **Then** the completion is accepted and B's dispute (with reason) is queued as context for the stage that raised B.
2. **Given** a disputed item queued for the raising stage, **When** that stage next runs, **Then** its input includes the dispute and its verdict resolves it: re-raising the item (rejecting the dispute) or passing (accepting it).
3. **Given** a stage re-raises a previously disputed item, **When** the card bounces again, **Then** the re-raised item is marked as re-raised in the implementer's context (the implementer cannot dispute the same item twice on unchanged reasoning — a second dispute of a re-raised item routes the card to the operator hold).
4. **Given** a completion that lists no disposition for a delivered item, **When** the floor evaluates, **Then** the missing item counts as not addressed (contributes to the User Story 1 floor decision).
5. **Given** a card where every feedback item is disputed and the head never moved, **When** the raising stage runs with the disputes and passes, **Then** the card advances normally — a validated dispute is a legitimate no-op completion.

---

### User Story 3 - A documentation pass is only recorded when documentation changed (Priority: P3)

The tech_writer reports `docs_committed`. Coordinare checks the completion's reported modified files and the head delta for the session: if documentation actually changed, the pass is recorded as today (and feeds spec-125's last-documented marker). If nothing changed — no modified files, no head movement — the stage still advances (documentation is not a gating verdict), but coordinare records a distinct no-op completion instead of a documentation pass, logs it, and does not credit the card with up-to-date docs.

**Why this priority**: Lowest-risk floor: it never blocks a card, it only stops coordinare's bookkeeping from asserting documentation work that did not happen — which matters once spec-125 starts skipping documenting runs based on that bookkeeping.

**Independent Test**: Have a tech_writer return `docs_committed` with no modified files and no new commit; observe the card advances, a no-op completion event is logged, and no documentation-pass record is written.

**Acceptance Scenarios**:

1. **Given** a tech_writer completion reporting modified documentation files and a head delta, **When** coordinare processes it, **Then** a documentation pass is recorded (today's behaviour).
2. **Given** a tech_writer completion reporting `docs_committed` with zero modified files and no head delta, **When** coordinare processes it, **Then** the stage advances, a `documenting_noop_completion` event is logged, and NO documentation-pass record is written.
3. **Given** a tech_writer inheriting a PR whose docs were already current, **When** it honestly completes without changes, **Then** scenario 2 applies — this is a legitimate outcome, not an error; the card is never bounced for it.

---

### Edge Cases

- **Feedback addressed by a prior session's commits**: the floor compares against the head the feedback was *raised* against, not the head at dispatch — commits landed by an earlier partial run (before a crash or retry) already moved the head past `F`, so an honest "it's already done" completion passes the floor without a dispute.
- **Force-push that rewrites but does not change content**: the head SHA differs, the floor passes. Accepted: the floor is a progress *floor*, not a semantic diff — downstream verdict stages still judge the content.
- **Feedback raised by CI rather than a review stage** (lint gate, CI-failure relay): these items carry the CI gate as their raiser; a dispute routes to the operator hold (CI cannot adjudicate), preventing an implementer from disputing away a red check.
- **Mixed completions**: some items addressed with commits, others neither addressed nor disputed — the floor treats the completion as incomplete only when *no* progress exists (head unmoved AND nothing disputed); partial progress with the head moved advances, and unaddressed items ride forward to the next verdict stage, which will re-raise them if they matter. This keeps the floor from over-blocking.
- **Interaction with spec-125 verdict cache**: a validated dispute or any floor-accepted no-op completion must NOT let a downstream stage's cached verdict skip the re-adjudication — a queued dispute for a stage vetoes that stage's cache skip.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: When coordinare bounces a card with feedback, it MUST record the PR head SHA the feedback was raised against and assign each feedback item a stable identifier, both surviving restart.
- **FR-002**: An implementer terminal success on a feedback-driven dispatch MUST be accepted only when (a) the current PR head differs from the feedback-origin SHA, or (b) at least one delivered feedback item is explicitly disputed. Otherwise coordinare MUST apply zero-progress handling: one re-dispatch with a strengthened directive enumerating unaddressed items, then an operator hold on repeat.
- **FR-003**: Floor-triggered re-dispatches MUST consume the existing bounded zero-progress allowance and MUST NOT increment content-feedback or transient-error budgets.
- **FR-004**: Feedback delivered to the implementer MUST include item identifiers, and the completion contract MUST accept a per-item disposition of *addressed* (with summary) or *disputed* (with reason); items without a disposition count as not addressed.
- **FR-005**: Disputed items MUST be queued for the stage that raised them and included in that stage's next dispatch context; that stage's verdict adjudicates the dispute (re-raise or pass).
- **FR-006**: A feedback item re-raised after a rejected dispute MUST be marked as re-raised in subsequent implementer context; a second dispute of a re-raised item MUST route the card to an operator hold rather than another automatic cycle.
- **FR-007**: Feedback items whose raiser cannot adjudicate (CI-originated items) MUST route disputes to the operator hold, never to automatic acceptance.
- **FR-008**: A tech_writer `docs_committed` completion with zero reported modified files and no head delta MUST advance the stage while recording a distinct no-op completion event and MUST NOT be recorded as a documentation pass.
- **FR-009**: Verdict roles (reviewer, security, qa, closer) MUST be exempt from head-delta floors — their terminal successes are processed as today.
- **FR-010**: All floor decisions (acceptance, strengthened re-dispatch, hold, dispute routing, no-op completion) MUST emit structured, per-card observable events.
- **FR-011**: Every floor MUST fail open: missing feedback-origin bookkeeping, an unresolvable head, or a completion predating this feature results in today's acceptance behaviour.
- **FR-012**: A queued dispute for a stage MUST veto any cached-verdict skip of that stage (coordination requirement with spec-125's FR-002(c)).

### Key Entities

- **Feedback item**: {identifier, raising stage, origin head SHA, body, disposition, re-raised flag}. Created at bounce time; disposition set from the implementer completion; adjudicated by the raising stage.
- **Feedback-origin SHA**: the PR head commit a bounce's feedback was raised against; the reference point for the implementer progress floor.
- **No-op completion record**: per-stage marker that a terminal success carried no work product; observability only, never a gate by itself.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A no-op implementer completion after a feedback bounce never silently advances the card: 0 occurrences of downstream stages running on an unmoved head with undisputed feedback (today: every such completion advances).
- **SC-002**: Thrash is bounded: a card whose head never moves consumes at most one strengthened re-dispatch before an operator hold, and consumes zero content-feedback cycles in the process (today: up to the full 5-cycle budget).
- **SC-003**: 100% of feedback items delivered on a bounce reach a recorded terminal disposition (addressed, dispute-accepted, dispute-rejected/re-raised, or escalated).
- **SC-004**: 100% of disputes are adjudicated by the raising stage or the operator — none are auto-accepted by coordinare.
- **SC-005**: Coordinare's documentation bookkeeping is truthful: no documentation-pass record exists without changed documentation (audit of pass records vs. actual changes shows zero divergence).
- **SC-006**: Cards whose implementer always moves the head and addresses feedback see zero behavioural change (regression suite equivalence).

## Assumptions

- The performer completion contract can carry per-item feedback dispositions as a structured field, mirroring how existing structured response fields (modified files, comment deltas) already flow back; the implementer persona is updated in this feature to require dispositions.
- "Head delta" observations reuse the head bookkeeping coordinare already maintains for the zero-progress guards; this spec adds the feedback-origin SHA, not a new head-tracking mechanism.
- The strengthened-directive re-dispatch reuses the existing zero-progress re-dispatch machinery (spec 070/072) rather than introducing a parallel loop.
- If implemented in the same release as spec-125, the persisted additions share one snapshot schema bump; if implemented separately, this feature takes its own backward-compatible bump.
