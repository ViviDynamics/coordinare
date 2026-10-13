# Feature Specification: Implementer-Side Rebase

**Feature Branch**: `555-implementer-side-rebase`
**Created**: 2026-10-13
**Status**: Draft
**Supersedes**: the daemon-side rebase of specs 047 (auto-rebase on merge), 096 (auto-rebase across restart) and 097 (pre-dispatch rebase guard). Their detection and anti-thrash rules are kept; their actor changes.
**Input**: User description: "Move spec 097's pre-dispatch rebase out of the daemon into the implementer role"

## Overview

Coordinare is an orchestrator. It decides when work happens and which performer does it. It must never touch the code itself. Bringing a branch up to date with main is code work, so it belongs to the implementer.

Specs 047, 096 and 097 put that work inside the daemon: the daemon cloned the repository, rebased in-flight PR branches and force-pushed them, using its own credentials. Spec 097 then made dispatch conditional on that rebase succeeding. That placement was a mistake on two counts. It breaks the orchestrator boundary, and it makes dispatch depend on a capability (git credentials inside the daemon) that a deployment may not have.

### Observed incident (motivation)

On the garden cluster (coordinare 2026.10.64, GitHub App authentication, no git credentials in the daemon), card `PVTI_lADOBjmjsc4Bkx5YzhANaH0` (conductor-e2e sample repo, PR 81) had a branch that diverged from main. The daemon's rebase failed every cycle ("Couldn't find host github.com in the .netrc file", event `rebase.failed`). The pre-dispatch guard read the failure as "do not dispatch", so the card stayed in the dispatching phase forever and no performer was ever started. The guard had no switch, so recovery needed manual intervention.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Dispatch never wedges on branch divergence (Priority: P1)

An operator runs coordinare in a deployment where the daemon has no git credentials. A card's PR branch falls behind or conflicts with main. The card keeps moving: a performer is dispatched, and nothing the divergence check does (or fails to do) can hold the card in the dispatching phase cycle after cycle.

**Why this priority**: This is the live outage. An unrecoverable dispatch wedge is the worst outcome; a performer turn is cheap by comparison.

**Independent Test**: With the feature switched off, and separately with it on and every dependency of the check failing in turn (no credentials, lookup error, unknown mergeability, unreadable main), run a dispatch cycle for a card with an open PR and confirm a performer is dispatched in each case.

**Acceptance Scenarios**:

1. **Given** the feature is switched off, **When** a card with a conflicting PR branch reaches dispatch, **Then** the performer is dispatched normally and no divergence check runs.
2. **Given** the feature is enabled and the mergeability lookup raises an error, **When** the card reaches dispatch, **Then** the performer is dispatched normally and a note is logged.
3. **Given** the feature is enabled and mergeability is not yet computed, **When** the card reaches dispatch, **Then** the performer is dispatched normally and a note is logged.
4. **Given** the feature is enabled and the daemon cannot determine the current main revision, **When** the card reaches dispatch, **Then** the performer is dispatched normally.

---

### User Story 2 - The implementer brings a diverged branch up to date (Priority: P1)

When an in-flight card's PR branch is conflicting with, or behind, main, the daemon routes the card to the implementer with an explicit instruction to bring the branch up to date with main as part of its turn. The implementer does that inside its own container, where git credentials already exist, resolves any conflicts, and pushes.

**Why this priority**: Without it, removing the daemon rebase would leave diverged branches with no owner.

**Independent Test**: Enable the feature, present a card whose PR is reported conflicting, run a cycle, and confirm the implementer is dispatched carrying the divergence instruction and that the daemon ran no git command against the branch.

**Acceptance Scenarios**:

1. **Given** the feature is enabled and the card's next stage is the implementer, **When** its branch is reported conflicting or behind, **Then** the implementer is dispatched in that same cycle carrying the divergence instruction.
2. **Given** the feature is enabled and the card's next stage is another role, **When** its branch is reported conflicting or behind, **Then** the card is rerouted to the implementer carrying the divergence instruction and the implementer is dispatched on the next cycle.
3. **Given** the feature is enabled and a card is waiting with no active performer, **When** the periodic board check sees its branch conflicting or behind, **Then** the card is routed to the implementer carrying the divergence instruction.
4. **Given** a performer is actively working a card, **When** its branch diverges, **Then** the card is left alone until that performer finishes.

---

### User Story 3 - The same divergence is not handed out twice (Priority: P2)

If the implementer was already asked to resolve a divergence and neither main nor the branch has moved since, the daemon does not issue the same instruction again. It does not wait silently either: the card is moved to blocked with the reason, so a human sees it.

**Why this priority**: Prevents an endless loop of implementer turns against a conflict the implementer could not resolve. It is a dispatch decision, so it stays in the daemon.

**Independent Test**: Record a divergence request for a given main and branch revision, present the same revisions still conflicting, and confirm no second request is issued; then change either revision and confirm a request is issued again.

**Acceptance Scenarios**:

1. **Given** a divergence request was recorded for a main and branch revision pair, **When** the same pair is still conflicting at a later dispatch, **Then** no new request is issued and the card is blocked with a reason naming the branch and both revisions.
2. **Given** such a recorded request, **When** main or the branch has moved, **Then** a new request may be issued.

---

### User Story 4 - The daemon no longer clones, rebases or pushes (Priority: P1)

No code path in the daemon clones a repository in order to rebase it, rebases a branch, or pushes a branch. The divergence service runs no git command at all: the main revision is read through the hosting platform's API.

**Why this priority**: This is the design principle the change exists to restore.

**Independent Test**: Inspect the daemon package for the removed entry points and confirm none remain; confirm the four former call sites (pre-dispatch guard, board check, post-merge, multi-session preflight) no longer invoke them.

**Acceptance Scenarios**:

1. **Given** a PR merges, **When** the merge step completes, **Then** the daemon records the new main revision and runs no git operation on other branches.
2. **Given** main moves between cycles, **When** the board check runs, **Then** the daemon records the new main revision and only classifies branches through the hosting platform's read interface.

### Edge Cases

- The branch name on the PR does not carry the coordinare prefix: the daemon cannot be sure it owns the branch. Dispatch proceeds normally with a note; no instruction is issued.
- The card already carries pending feedback for the stage being dispatched: the check is skipped so pending work is never starved (unchanged from 097).
- The card is paused on the board, or its PR is closed: no instruction is issued (unchanged from 551/552).
- The hosting platform only reports "behind" when the repository requires up-to-date branches. Branches that are merely behind without such a rule, and not conflicting, are left alone. This is narrower than 047, which rebased every branch on every main move.
- A rebase rewrites published history, and the implementer's push path otherwise never overwrites an existing branch (spec 165). The rebased branch is therefore published only with a lease pinned to the exact branch revision the daemon classified. If anyone else pushed in the meantime, nothing is overwritten and the daemon classifies again.
- A documenter side run may be committing to the same branch. No rebase is requested while one is running.
- An implementer that merges main instead of rebasing still publishes normally; the result is accepted.
- A snapshot written by an older build carries anti-thrash markers with the old outcome values: they keep their meaning.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The daemon MUST NOT clone, rebase or push any branch. The daemon-side rebase entry points MUST be removed, not disabled.
- **FR-002**: The daemon MAY classify an in-flight PR branch as conflicting or behind using only the hosting platform's read interface.
- **FR-003**: When divergence handling is enabled and a branch is classified as conflicting or behind, the daemon MUST route the card to the implementer with a divergence instruction on the existing relay-feedback channel, preserving any feedback already queued.
- **FR-004**: The divergence instruction MUST tell the implementer to rebase the branch onto main inside its own workspace, resolve conflicts and verify. The performer MUST attempt the rebase itself before the agent starts, so a clean rebase needs no model.
- **FR-005**: The pre-dispatch check MUST fail open. A disabled feature, a missing dependency, a classification error, unknown mergeability, an unreadable main revision or an unidentifiable branch MUST each result in a normal dispatch with a logged note.
- **FR-006**: The only case in which the pre-dispatch check may refuse a dispatch is the anti-thrash case of FR-007. A reroute to the implementer may defer by exactly one cycle.
- **FR-007**: The daemon MUST NOT issue a second divergence instruction for the same main and branch revision pair. A change to either revision re-enables the instruction.
- **FR-008**: Divergence handling MUST sit behind a new configuration key. It defaults to enabled (owner decision, 2026-10-13: it replaces behavior that was previously unconditional, and it cannot wedge dispatch).
- **FR-009**: A card with an active performer MUST NOT be rerouted (047 FR-006 unchanged). A board-paused card or a card whose PR is closed MUST NOT be rerouted.
- **FR-010**: Structured log events MUST keep their existing names where behavior is unchanged. Events whose owner changed MUST be documented with the old and new names.
- **FR-011**: The persisted snapshot schema MUST NOT change.
- **FR-012**: Other daemon-side paths that touch code or run git MUST be listed for the owner's review. Only the main-revision lookup is changed by this feature (it moves from a git command to the platform API); the rest are tracked as separate issues.
- **FR-013**: When the anti-thrash case of FR-007 refuses a dispatch, the card MUST be moved to blocked with a human-readable reason. It MUST NOT wait silently in the dispatching phase.
- **FR-014**: The performer MUST overwrite an existing remote branch only when all of these hold: the dispatch carries the daemon's divergence request, the role is the implementer, no rebase is left in progress, the tracked tree is clean, the main revision the daemon saw is contained in the new history, and the remote branch is still at the revision the daemon classified. Otherwise nothing is overwritten.
- **FR-015**: No divergence request is issued while a documenter side run is active on the card.

### Key Entities

- **Divergence classification**: the daemon's read-only verdict for one PR branch: proceed, unknown, request the implementer, or held as already requested.
- **Divergence request marker**: the existing per-card anti-thrash marker (main revision, branch revision, outcome). It gains one outcome value meaning "handed to the implementer".
- **Divergence instruction**: one relay-feedback entry addressed to the implementer.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: In a deployment whose daemon has no git credentials, a card with a diverged branch is dispatched to a performer within one cycle of becoming eligible, in 100% of cases.
- **SC-002**: With the feature enabled, a diverged branch reaches the implementer with the divergence instruction within two cycles of being detected.
- **SC-003**: Zero daemon-initiated clone, rebase or push operations occur in any cycle.
- **SC-004**: An unresolved divergence produces at most one implementer turn per main and branch revision pair.
- **SC-005**: With the feature switched off, dispatch behavior is independent of branch mergeability.
- **SC-006**: Zero commits by another author are lost to a rebase publish.
- **SC-007**: A branch that is behind main with no conflict is brought up to date without any model call being required for the rebase itself.

## Assumptions

- The implementer's workspace is a fresh clone taken at dispatch time, so main in that workspace is current.
- The key defaults to enabled, which departs from the house style for new keys. The owner chose this because the key replaces behavior that was unconditional before, and the guard fails open.
- A human who unblocks a card held by FR-013 has either moved the branch or left an instruction; either re-enables dispatch.
