# Feature Specification: Stale / Addressed Human Review Handling

**Feature Branch**: `128-stale-review-handling`
**Created**: 2026-07-08
**Status**: Draft
**Input**: Surface and correctly handle stale or already-addressed human "changes requested" reviews so a card is never silently blocked forever by an outstanding change-request whose feedback is already resolved or superseded.

## Context / Problem

When a human submits a GitHub **"Changes Requested"** review, GitHub's `reviewDecision` stays `CHANGES_REQUESTED` until that same human explicitly re-reviews or the review is dismissed. Branch-protection's `dismiss_stale_reviews` only dismisses stale **approvals** on new commits — **not** change-requests. So a human change-request placed on an early commit remains the authoritative gate even after the requested changes are addressed across many later commits, QA passes, the build goes green, and trusted bots re-approve.

Observed live on card #111 / PR #134: a human change-request from ~7 weeks earlier, on a commit dozens of commits behind the PR head, kept the card parked in **BLOCKED** with no fresh explanation — so right after a passing QA run it looked like a mystery block. Coordinare was correctly honoring the human review ("only humans approve/gate"), but did so **silently** and treated already-addressed feedback as if it were still outstanding.

Two gaps: (1) coordinare counts **resolved** and **superseded** review feedback as still-outstanding, and (2) when feedback *has* been addressed it leaves the card silently BLOCKED instead of putting the ball back in the reviewer's court.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Addressed change-request returns to review, not silent block (Priority: P1)

A PR carries an outstanding human `CHANGES_REQUESTED` review submitted on an early commit. Since then the implementer has landed fixes across new commits and resolved the review's inline threads. Instead of leaving the card silently in BLOCKED, coordinare **re-requests review from the original reviewer** and moves the card to **IN_REVIEW**, and fires exactly one operator notification explaining the situation.

**Why this priority**: This is the core defect — cards silently stuck forever after their feedback was addressed. Fixing it restores forward flow and makes the state honest, without violating the human-approval gate.

**Independent Test**: On a PR whose only formal gate is a human `CHANGES_REQUESTED` review on a commit N+ behind head with all its threads resolved, run one evaluation cycle and confirm the card moves BLOCKED → IN_REVIEW, a re-review is requested from that reviewer, and one notification fires.

**Acceptance Scenarios**:

1. **Given** a PR with `reviewDecision=CHANGES_REQUESTED` from a human review on commit C, **and** the head is N+ commits (or H+ hours) ahead of C, **and** that review's inline threads are all resolved, **When** coordinare evaluates the card, **Then** the card is placed IN_REVIEW (not BLOCKED), a re-review request is sent to the original reviewer, and one deduplicated notification is emitted.
2. **Given** the same card was already surfaced once, **When** coordinare re-evaluates on the next cycle with no new change, **Then** no duplicate notification and no duplicate re-request are emitted.
3. **Given** the original reviewer subsequently approves (or dismisses their review), **When** coordinare evaluates, **Then** the card proceeds through the normal merge path.

---

### User Story 2 - Implementer resolves review threads as it fixes them (Priority: P2)

As the implementer addresses each inline review comment, it marks that review thread **resolved** and leaves a short note referencing the fixing commit. This produces a reliable "all feedback addressed" signal and speeds the human's re-review.

**Why this priority**: It creates the resolved-thread signal US1 relies on and reduces reviewer re-work, but US1 can ship first using commit-staleness alone.

**Independent Test**: Give the implementer a PR with open review threads; after its run, confirm each thread it addressed is marked resolved with an "addressed in `<commit>`" note, and threads it did not address remain unresolved.

**Acceptance Scenarios**:

1. **Given** a PR with open inline review threads, **When** the implementer addresses a thread's feedback in a commit, **Then** that thread is marked resolved with a note naming the fixing commit.
2. **Given** a thread whose feedback the implementer did **not** address, **When** the implementer finishes, **Then** that thread remains unresolved.

---

### User Story 3 - Fresh change-requests are unchanged (Priority: P1)

A human requests changes on (or near) the current PR head — the feedback is genuinely outstanding. Coordinare keeps its existing behavior (address-the-feedback flow); it must **not** prematurely re-request review or flip the card to IN_REVIEW as if the feedback were addressed.

**Why this priority**: Guards against a regression that would let genuinely-unaddressed feedback slip through — as critical as US1.

**Independent Test**: On a PR with a human `CHANGES_REQUESTED` on the current head with unresolved threads, run a cycle and confirm the existing behavior is preserved (no re-request, no premature IN_REVIEW).

**Acceptance Scenarios**:

1. **Given** a human `CHANGES_REQUESTED` review whose commit equals (or is within threshold of) the current head with unresolved threads, **When** coordinare evaluates, **Then** the existing address-the-feedback behavior applies and the stale-handling path does not trigger.

---

### Edge Cases

- **Body-only change-request (no inline threads)** — as in #111. Staleness must be determinable from the review commit vs head alone (thread-resolution is a bonus signal, not a requirement).
- **Multiple human change-requests** from different reviewers — each is evaluated independently; the card only advances past all of them.
- **Reviewer re-request already pending** — do not spam duplicate re-review requests; re-request at most once per stale situation.
- **New commit lands after re-request** — the situation is re-evaluated; a genuinely new fresh review resets to US3 behavior.
- **Mixed threads** (some resolved, some not) on a stale review — treated as still-outstanding (not fully addressed) → do not auto-advance; surface as needing attention.
- **Coordinare bot lacks permission** to resolve threads or re-request review — must fail safe (leave blocked + notify), never crash the cycle.
- **Reviewer is also the approver-of-record** — re-requesting must not create a self-approval loophole; the human-approval gate is unchanged.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: Coordinare MUST classify an outstanding human `CHANGES_REQUESTED` review as **stale** when the review's commit differs from the current PR head by at least a configurable threshold (commits-behind and/or hours-behind).
- **FR-002**: Coordinare MUST classify a stale change-request as **addressed** when its inline threads are all resolved, OR (for body-only reviews with no threads) when new commits have landed since the review.
- **FR-003**: For a stale, addressed change-request, coordinare MUST move the card to **IN_REVIEW** (not BLOCKED) and **re-request review** from the original reviewer(s).
- **FR-004**: Coordinare MUST emit exactly **one** deduplicated operator notification per stale-review situation — naming the reviewer, review date, the review's commit vs current head, the PR and review identifiers, and the next action ("re-review or dismiss") — and MUST NOT re-notify or re-request on subsequent cycles absent a new change.
- **FR-005**: Coordinare MUST NOT auto-dismiss or auto-approve any human review (the human-approval gate is preserved).
- **FR-006**: Coordinare MUST NOT bounce a stale change-request to the implementer indefinitely; the stale-handling path replaces re-bouncing once feedback is addressed.
- **FR-007**: For a **fresh** human change-request (review commit within threshold of head, or with unresolved threads), coordinare MUST preserve existing behavior and MUST NOT trigger the stale path.
- **FR-008**: When determining whether actionable review feedback remains, coordinare MUST **exclude** resolved inline threads and reviews whose commit is superseded per FR-001/FR-002.
- **FR-009**: The implementer MUST mark inline review threads resolved as it addresses each, annotating with the fixing commit; threads it does not address MUST remain unresolved.
- **FR-010**: The staleness threshold(s) MUST be configurable, with a sensible default, and the default MUST be safe (never auto-advance a genuinely-fresh review).
- **FR-011**: When a human subsequently approves or dismisses the gating review, the card MUST proceed through the normal merge path with no manual intervention.
- **FR-012**: All new outward actions (re-request review, thread resolution, notification) MUST fail safe — if an API call or permission is unavailable, the card stays blocked/parked and the failure is surfaced, never crashing the poll cycle.

### Key Entities

- **Review**: a human PR review — identifier, author, state (APPROVED / CHANGES_REQUESTED / COMMENTED), the commit it was submitted against, submission time, body. Only formal APPROVED/CHANGES_REQUESTED states drive the merge decision.
- **Review Thread**: an inline conversation on a PR — resolved/unresolved, associated review, comments. Resolution signals that a specific piece of feedback was addressed.
- **Pull Request**: head commit, `reviewDecision`, associated card/session.
- **Stale-review situation**: the derived condition (PR + gating review) used as the notification/re-request dedup key so each situation is acted on once.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A card whose review feedback has been addressed no longer sits silently in BLOCKED — within one evaluation cycle of the fixes landing it is IN_REVIEW with a re-review requested.
- **SC-002**: Operators receive exactly **one** actionable notification per stale-review situation (zero per-cycle repeats).
- **SC-003**: **Zero** human reviews are auto-dismissed or auto-approved by coordinare across all runs.
- **SC-004**: Fresh change-requests exhibit **no** behavior change (no regression) versus today.
- **SC-005**: 100% of resolved inline threads are excluded from the outstanding-feedback determination.
- **SC-006**: For the #111/#134 class of situation (weeks-stale change-request on a far-behind commit), the card reaches IN_REVIEW with a re-review request instead of remaining silently blocked.

## Assumptions

- GitHub keeps a human `CHANGES_REQUESTED` verdict authoritative until the human re-reviews or it is dismissed; `dismiss_stale_reviews` clears only approvals. This feature works *with* that, not around it.
- The coordinare bot identity has permission to re-request reviews and resolve review threads on the target repos; where it does not, FR-012 fail-safe applies.
- "Only humans approve / gate" remains a hard project principle; this feature never clears a human verdict on the human's behalf.
- The existing review data already exposes review id, author, state, body, submission time, and associated commit; thread resolution state is additionally required.

## Out of Scope

- Auto-dismissing or auto-approving human reviews.
- Changing how genuinely-fresh change-requests are handled.
- Changing the human-approval merge gate itself.
- Any auto-merge behavior.
