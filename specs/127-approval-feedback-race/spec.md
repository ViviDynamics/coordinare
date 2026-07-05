# Feature Specification: Approval/Feedback Race — No Merge Over Unprocessed Feedback

**Feature Branch**: `127-approval-feedback-race`
**Created**: 2026-07-04
**Status**: Draft
**Input**: User description: "Approval/feedback race: a human APPROVED review in the same poll batch as CHANGES_REQUESTED or COMMENTED reviews must not advance to merging while dropping the actionable feedback"

## Overview

When coordinare polls a PR's reviews, it evaluates the whole new-since-cutoff batch at once. If that batch contains a human APPROVED review, coordinare advances the card to merging — even when the same batch also contains actionable reviews (CHANGES_REQUESTED or COMMENTED, from a human or a trusted bot). The actionable reviews are collected but the approval branch wins, the relay/classification path never runs for them, and because the card moves to merging they are never revisited: the feedback is silently and permanently dropped, and a PR can merge with an unaddressed change request sitting on it. Two reviewers submitting within one poll window — or one reviewer approving while a trusted bot raises findings — is all it takes. Diagnosed and code-verified in the 2026-07-03 architectural review (finding: `monitor_pr` approval precedence).

This feature makes the rule explicit: **actionable feedback always gets processed; an approval is deferred — never discarded — while unprocessed actionable feedback coexists.** Merging proceeds only from an evaluation with a standing approval and no unprocessed actionable reviews.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Same-batch approval and change request: feedback first, merge deferred (Priority: P1)

Reviewer A approves the PR; within the same poll window, reviewer B requests changes. Coordinare routes the card to feedback classification (as it would if only B's review existed) instead of merging. B's feedback flows through the normal relay/bounce path. The approval is not lost: once the actionable feedback has been processed and no unprocessed actionable reviews remain, a still-standing approval takes the card to merging on a subsequent evaluation without requiring A to re-approve.

**Why this priority**: This is the whole defect — a silent, permanent feedback drop that can merge a PR over an explicit human change request. Correctness of the human-review contract outranks any efficiency concern.

**Independent Test**: Seed a review batch containing one human APPROVED and one human CHANGES_REQUESTED review; observe the card enters the feedback path (not merging), the change request reaches classification, and — after the feedback is processed — a later evaluation with the standing approval merges.

**Acceptance Scenarios**:

1. **Given** a poll batch with a human APPROVED review and a human CHANGES_REQUESTED review, **When** coordinare evaluates the PR, **Then** the card routes to feedback classification, NOT merging, and the change request is processed exactly as it would be without the approval.
2. **Given** a poll batch with a human APPROVED review and a trusted-bot COMMENTED review, **When** coordinare evaluates the PR, **Then** the bot feedback routes to classification and merging is deferred.
3. **Given** a deferred approval whose coexisting feedback has since been fully processed (classified and, where applicable, addressed through the normal lifecycle), **When** a subsequent evaluation finds no unprocessed actionable reviews and the approval standing, **Then** the card advances to merging without human re-approval.
4. **Given** a poll batch containing only a human APPROVED review, **When** coordinare evaluates the PR, **Then** behaviour is unchanged from today (including the existing base-branch red-check refusal before merging).
5. **Given** a poll batch containing only actionable reviews, **When** coordinare evaluates the PR, **Then** behaviour is unchanged from today (feedback relayed; no merge).
6. **Given** a merge deferral, **When** it occurs, **Then** a structured event is logged naming the approval and the coexisting actionable review(s) so an operator can see why the merge waited.

---

### User Story 2 - Latest review state per reviewer governs (Priority: P2)

A single reviewer submits CHANGES_REQUESTED and then, after a fix discussion in the same window, APPROVED. Within one evaluation batch, only each reviewer's latest review state counts: the earlier change request from the same reviewer is superseded and does not defer the merge. Conversely, an APPROVED followed by the same reviewer's later CHANGES_REQUESTED must defer — the stale approval cannot win on batch ordering luck.

**Why this priority**: Without per-reviewer ordering, the User Story 1 rule would over-defer (blocking merges on feedback the reviewer themselves already withdrew) or under-defer (honouring a stale approval), depending on arrival order.

**Independent Test**: Seed both orderings of two reviews from one reviewer in a single batch and observe: change-request-then-approval merges; approval-then-change-request defers and relays.

**Acceptance Scenarios**:

1. **Given** one reviewer's CHANGES_REQUESTED followed by their later APPROVED in the same batch, **When** coordinare evaluates the PR, **Then** the approval governs and the card advances to merging (no deferral on the superseded request).
2. **Given** one reviewer's APPROVED followed by their later CHANGES_REQUESTED in the same batch, **When** coordinare evaluates the PR, **Then** the change request governs: feedback is relayed and merging is deferred.
3. **Given** reviews from different reviewers, **When** ordering is evaluated, **Then** supersession applies only within the same reviewer — one reviewer's approval never supersedes another's change request.

---

### Edge Cases

- **COMMENTED-only coexistence**: a COMMENTED review may be a question rather than a demand. It still routes through classification first (classification already distinguishes actionable from informational); merging proceeds once classification disposes of it. The deferral cost is one poll cycle, not a bounce.
- **Approval visible, feedback already processed in a prior cycle**: reviews already marked processed do not defer — only *unprocessed* actionable reviews in the current evaluation count.
- **Feedback cycle changes the code after a deferred approval**: if processing the feedback bounces the card and new commits land, the normal lifecycle governs from there (downstream stages re-run per existing rules). This feature neither dismisses nor refreshes the human approval; it only sequences coordinare's own actions. Whether a stale approval should merge new commits is repository policy (branch-protection re-approval rules), not coordinare's decision.
- **Trusted-bot approvals**: unchanged — bots cannot approve; only human approvals trigger merging, as today.
- **Batch with approval + actionable review that classification later deems ignorable**: the merge simply proceeds on the next evaluation; the deferral is self-releasing, and no operator action is ever required to "un-defer".

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: Coordinare MUST NOT advance a card to merging from an evaluation whose batch contains unprocessed actionable reviews (human or trusted-bot CHANGES_REQUESTED/COMMENTED), regardless of any coexisting approval.
- **FR-002**: When an approval coexists with unprocessed actionable reviews, the actionable reviews MUST be routed to the existing feedback classification path exactly as they would be absent the approval — no items skipped, none double-processed.
- **FR-003**: A deferred approval MUST NOT be consumed or discarded: once no unprocessed actionable reviews remain, a standing approval MUST advance the card to merging on a subsequent evaluation without human re-approval.
- **FR-004**: Within a single evaluation batch, only each reviewer's latest review state MUST govern; earlier reviews from the same reviewer are superseded for both approval and deferral decisions.
- **FR-005**: Every merge deferral MUST emit a structured observable event identifying the approval and the coexisting actionable review(s).
- **FR-006**: Evaluations containing only approvals, or only actionable reviews, MUST behave identically to today (including the existing pre-merge base-branch check).

### Key Entities

- **Evaluation batch**: the set of new-since-cutoff, not-yet-processed reviews coordinare considers in one poll of a PR; the unit over which the deferral rule and per-reviewer supersession apply.
- **Deferred approval**: an approval observed in a batch that also contained unprocessed actionable reviews; implicit state — re-derived from PR review state on later evaluations rather than stored as a separate flag.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Zero actionable reviews are dropped when they share a batch with an approval (today: 100% of them are silently dropped in that scenario).
- **SC-002**: No PR merges from an evaluation containing an unprocessed change request (audit: every merge event's batch shows zero unprocessed actionable reviews).
- **SC-003**: A deferred merge completes within one poll cycle after its coexisting feedback is processed, with no human re-approval required.
- **SC-004**: Approval-only and feedback-only batches show byte-identical behaviour to the pre-feature baseline (regression suite equivalence).

## Assumptions

- Review "processed" bookkeeping (the existing processed-review memory) remains the authority for which reviews still need classification; this feature adds no parallel tracking.
- Per-reviewer supersession uses review submission timestamps as ordered by the hosting platform; ties (identical timestamps) resolve conservatively — the actionable state governs.
- Repository branch-protection rules remain the backstop for stale-approval policy on new commits; coordinare does not attempt to dismiss or refresh approvals.
