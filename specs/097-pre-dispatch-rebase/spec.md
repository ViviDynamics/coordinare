# Feature Specification: Pre-Dispatch Rebase Guard

**Feature Branch**: `097-pre-dispatch-rebase`  
**Created**: 2026-06-20  
**Status**: Draft  
**Input**: User description: "Pre-dispatch rebase guard — rebase a conflicting/behind in-flight branch BEFORE (re)dispatching a performer to it, so the spec-096 auto-rebase heal is not starved by perpetual performer re-dispatch."

## Overview

Spec 096 added a proactive rebase that heals in-flight branches the hosting platform reports as conflicting with (or behind) main. But — inheriting spec-047's rule that you never rebase a branch while a performer is actively working it (rebasing under a live performer would clobber its work) — 096 *skips* any branch that has an active performer.

That rule is correct in isolation, but it leaves a **livelock** for a card whose branch is conflicting with main: the coordinare keeps re-dispatching a performer onto the conflicting (stale-base) branch each cycle; the performer **cannot** fix a stale-base merge conflict (only a rebase can); the card stays in the "performer working" state; 096's heal skips it every cycle because a performer is active; the performer does futile work (e.g. an unrelated "report" commit) and the card never converges. The one actor that *can* fix the branch — the rebase — is blocked precisely because a performer was dispatched onto a base it can't repair.

This feature closes the loop by moving the rebase to the **dispatch decision point**: before dispatching a performer to an in-flight card whose branch is conflicting/behind, the coordinare rebases the branch first, so the performer always starts on a current, mergeable base — and is never sent to fix a conflict it structurally cannot.

### Observed incident (motivation)

On 2026-06-20, website cards #169/#171 (PRs #173/#175) were both conflicting with main. With 096 live, the coordinare kept dispatching performers; 096 logged "skipped (active performer)" every cycle and never got a rebase window. The cards only unblocked after a manual operator rebase of the branches onto main. The performer was the wrong actor; sending it to a conflicting base wasted a full performer cycle each round and blocked the rebase that would have fixed it.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - A conflicting branch is rebased before a performer is dispatched (Priority: P1)

When the coordinare is about to dispatch (or re-dispatch) a performer to an in-flight card that already has an open PR whose branch is conflicting with — or behind — main, it first rebases that branch onto current main. On a clean rebase, the branch is republished and the performer is dispatched onto the now-current base. On a genuine conflict, the branch is routed to the existing performer conflict-resolution path or held for an operator — and **no fresh performer is dispatched onto the unfixable conflicting base**.

**Why this priority**: This is the exact livelock that stranded two live cards and forced a manual operator rebase. Without it, any card whose branch drifts into conflict while it still has work to do can churn performers indefinitely. Implementing only this restores convergence — it is the MVP.

**Independent Test**: Set up an in-flight card with an open PR whose branch is conflicting with main and is eligible for performer dispatch; run a cycle; verify the branch is rebased onto current main *before* any performer is dispatched (clean → performer dispatched on the rebased head; conflict → conflict-resolution/blocked, no fresh dispatch onto the conflicting base).

**Acceptance Scenarios**:

1. **Given** an in-flight card with an open PR whose branch is behind main and cleanly rebasable, **When** the coordinare reaches the point of dispatching a performer, **Then** the branch is first rebased onto current main and republished, and the performer is dispatched onto the rebased head.
2. **Given** an in-flight card whose branch genuinely conflicts with main, **When** the coordinare reaches the dispatch point, **Then** it routes the branch to conflict resolution (or holds the card for an operator) and does **not** dispatch a fresh performer onto the conflicting base.
3. **Given** an in-flight card whose branch is already current with main, **When** the coordinare reaches the dispatch point, **Then** no rebase is performed and the performer is dispatched exactly as today.

---

### User Story 2 - The guard never clobbers an already-running performer (Priority: P2)

The guard acts only at the *decision* to start a performer — never on a branch that already has a performer mid-run. A branch with an active performer is left untouched (spec-047's rule preserved); the guard's rebase happens in the window before a performer is (re)started.

**Why this priority**: Preserves the safety property 096/047 rely on (no rebase under a live performer) while still closing the livelock. It bounds where the new rebase may occur.

**Independent Test**: With a performer already mid-run on a conflicting branch, verify the guard does not rebase it; only once that performer has finished and the coordinare is deciding whether to dispatch again does the guard rebase (before the next performer starts).

**Acceptance Scenarios**:

1. **Given** a branch with a performer currently mid-run, **When** a cycle executes, **Then** the guard does not rebase that branch.
2. **Given** the hosting platform has not yet computed a branch's mergeability, **When** the coordinare reaches the dispatch point, **Then** it defers the dispatch decision one cycle rather than rebasing on a guess.

---

### User Story 3 - No thrash, isolation, and operator visibility (Priority: P3)

A branch blocked on an unresolvable conflict is not re-rebased every cycle; a rebase failure for one card never blocks dispatch decisions for the others; and every pre-dispatch rebase is recorded for the operator with non-sensitive identifiers.

**Why this priority**: Makes the guard safe to run continuously and debuggable. Rides on US1/US2.

**Independent Test**: Trigger a pre-dispatch rebase that blocks on conflict; verify it is not re-attempted next cycle until the branch head or main changes; verify a forced failure on one card does not stop other cards being dispatched; verify a secret-free record is emitted.

**Acceptance Scenarios**:

1. **Given** a branch already recorded as blocked-on-conflict for the current main, **When** the next cycle runs and neither the branch head nor main has changed, **Then** the guard does not re-rebase it.
2. **Given** a pre-dispatch rebase fails for one card, **When** the cycle continues, **Then** other cards' dispatch decisions are unaffected.
3. **Given** any pre-dispatch rebase, **When** it completes, **Then** a record is emitted with the card id, branch, prior/current main reference, and outcome — and no secret values.

---

### Edge Cases

- **Already-current branch**: no rebase, performer dispatched exactly as today (no churn).
- **No open PR yet** (first implementer dispatch, branch not yet published): the guard does not apply — there is no conflicting published branch to rebase; dispatch proceeds normally.
- **Mergeability unknown / not yet computed**: defer the dispatch one cycle; never rebase on a guess.
- **Unresolvable conflict**: route to the existing conflict-resolution path or block for an operator; do not dispatch a fresh performer onto the conflicting base; do not re-rebase every cycle (anti-thrash).
- **Active performer already running**: untouched (US2).
- **Clean rebase then dispatch**: the performer starts on the rebased head; its first action is against current main.
- **Gate disabled**: dispatch behaviour is byte-identical to today.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: Before (re)dispatching a performer to an in-flight card that has an open PR whose branch is reported conflicting with — or behind — main, the coordinare MUST rebase that branch onto current main first, using the existing 047/096 rebase machinery.
- **FR-002**: On a clean rebase, the coordinare MUST republish the branch (lease-protected) and then dispatch the performer onto the rebased head.
- **FR-003**: On a genuine conflict, the coordinare MUST route the branch to the existing performer conflict-resolution path or hold the card for an operator, and MUST NOT dispatch a fresh performer onto the conflicting base.
- **FR-004**: The guard MUST NOT rebase a branch that already has a performer mid-run (spec-047 safety preserved); it acts only at the dispatch decision point.
- **FR-005**: When the branch's mergeability is unknown/not-yet-computed, the coordinare MUST defer the dispatch decision one cycle rather than rebasing on a guess.
- **FR-006**: The guard MUST NOT apply when the branch is already current with main, when the card has no open published PR, or when the auto-rebase capability is disabled — in those cases dispatch behaviour is unchanged.
- **FR-007**: Rebase publication MUST always use a lease (never an unconditional force-publish).
- **FR-008**: The guard MUST reuse spec-096's per-card anti-thrash marker so a branch blocked on an unresolvable conflict is not re-rebased until its head or main changes.
- **FR-009**: A pre-dispatch rebase failure for one card MUST NOT block dispatch decisions for other cards (per-card isolation).
- **FR-010**: The coordinare MUST emit a secret-free observability record for each pre-dispatch rebase (card id, branch, prior/current main reference, outcome) — never secret values (carried invariant from 088/090/095/096).
- **FR-011**: This feature MUST NOT change the spec-047 merge-time rebase path or spec-096's restart/proactive triggers; it adds a rebase check at the dispatch decision only. No new external dependency; the persisted state model is unchanged (reuses 096's last-known-main reference + per-card rebase marker).

### Key Entities *(include if feature involves data)*

- **In-flight dispatch candidate**: a card about to receive a performer that already has a published PR, with a branch mergeability state (current / behind / conflicting / unknown) relative to main.
- **Pre-dispatch rebase outcome record**: the observable result of a guard-triggered rebase — card id, branch, prior/current main reference, outcome (clean / conflict-routed / blocked / failed), free of secret values.
- **Anti-thrash marker** (reused from 096): the per-card record of the last rebase attempt (target main, branch head, outcome) that prevents re-rebasing an unresolvable conflict every cycle.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A performer is never dispatched onto a branch that is conflicting with main; in 100% of dispatch decisions for a conflicting in-flight branch, a rebase (clean → dispatch, or conflict → resolve/block) happens first.
- **SC-002**: A card whose branch is conflicting with main converges (rebased clean → CI runs, or routed to conflict-resolution/operator) within a bounded number of cycles, with **zero** futile performer dispatches onto the conflicting base — closing the livelock.
- **SC-003**: A branch already current with main incurs zero pre-dispatch rebases and zero republishes (no churn); a branch blocked on conflict is not re-rebased until its head or main changes (no thrash).
- **SC-004**: Across a cycle where one pre-dispatch rebase fails, every other card's dispatch decision still proceeds.
- **SC-005**: 100% of guard-triggered rebases emit a record with card/branch/main/outcome, and 0% contain secret values.
- **SC-006**: With the capability disabled, dispatch behaviour and all existing dispatch/rebase tests are byte-identical to before this feature (no regression to 047/096).

## Assumptions

- The hosting platform exposes a per-PR mergeability signal sufficient to classify a branch as current / behind / conflicting / unknown (the same signal spec 096 uses); unknown → defer.
- The existing 047/096 machinery (rebase round, per-card rebase, lease-based republish, conflict-resolution path, per-card isolation, anti-thrash marker) is the substrate this guard invokes at the dispatch point; this feature adds a check before dispatch, not a new rebase implementation.
- "In-flight card with an open PR" means a card that already has a published branch + PR and is about to be (re)dispatched to a performer — not a brand-new card whose first implementer run will create the branch.
- Single-host single-process JSON-snapshot state model is unchanged (reuses 096's persisted last-known-main reference and per-card rebase marker).

## Dependencies

- Spec 047 (auto-rebase-on-merge) — the rebase round, per-card rebase, lease-based republish, and conflict-resolution path this guard invokes.
- Spec 096 (auto-rebase restart resilience) — the proactive mergeability read and the per-card anti-thrash marker this guard reuses.
- The coordinare's existing dispatch-decision point for in-flight cards.

## Out of Scope

- Changing the spec-047 merge-time rebase path or spec-096's restart/proactive triggers.
- Improving the quality of the performer's conflict resolution.
- Auto-resolving genuine code conflicts beyond the existing 047 performer path.
- Any hosting-platform CI configuration.
