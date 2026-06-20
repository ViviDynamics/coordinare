# Feature Specification: Auto-Rebase Restart Resilience

**Feature Branch**: `096-auto-rebase-restart`  
**Created**: 2026-06-20  
**Status**: Draft  
**Input**: User description: "Auto-rebase restart resilience — rebase stranded in-flight branches that went stale while coordinare was down or across a restart. Continues the spec-047 auto-rebase line and the spec-094 restart-reconciliation line."

## Overview

Spec 047 (auto-rebase-on-merge) keeps every in-flight pull-request branch rebased onto the latest main, so the next card to merge does not pile up conflicts. But its rebase is **edge-triggered**: it fires only when the coordinare, *while continuously running*, notices that main has moved since the value it last saw. That "last seen" value is held only in memory and is reset on the first cycle after the coordinare starts.

The result is a blind spot across restarts. If main advances while the coordinare is stopped — or if a merge lands and the coordinare is then restarted — the coordinare comes back up, records the *current* main as its new baseline, and concludes nothing changed. Every in-flight branch that was cut from the *old* main is now behind and, in the common case, in conflict. A conflicting branch cannot be merged, so the hosting platform never runs its pull-request checks, so the card can never go green. Re-engaging the worker (e.g. via a changes-requested review) does not help, because no actor in the system is responsible for performing the rebase — the only actor that would, is asleep.

This feature makes the rebase **self-healing across restarts**: on startup, and on an ongoing basis, the coordinare brings stale or conflicting in-flight branches back into line with main, rather than relying solely on having witnessed the moment main moved.

### Observed incident (motivation)

On 2026-06-20 three website cards (#168/#169/#171, PRs #172/#173/#175) were each cut from a main that predated a baseline test fix (#176). After #176 merged, the coordinare was restarted. On restart it adopted the post-#176 main as its baseline and never rebased the three branches. They sat in a conflicting state with **zero** check runs, and re-issuing review feedback only caused the worker to do unrelated work (it added a report commit) — never the rebase that was actually required. The cards could not converge by any in-system path.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Stale branches are healed after a restart (Priority: P1)

After the coordinare restarts, any in-flight card whose branch is behind or in conflict with the current main is rebased onto main automatically, without an operator having to notice or intervene. A clean rebase republishes the branch and lets checks re-run; a conflicting rebase falls into the existing worker conflict-resolution path, and only blocks the card for an operator if that path cannot resolve it.

**Why this priority**: This is the exact failure that stranded three live cards indefinitely. Without it, every restart that follows a merge can silently wedge work, and the system has no way to recover on its own. It is the minimum viable slice — implementing only this restores self-healing.

**Independent Test**: Start with an in-flight card whose branch is based on an older main; advance main; restart the coordinare; verify that within a bounded number of cycles the branch is rebased onto the current main (clean case republishes and checks re-run; conflict case routes to conflict resolution or an operator-visible blocked state) — with no operator action.

**Acceptance Scenarios**:

1. **Given** main advanced while the coordinare was stopped and an in-flight branch is now behind it, **When** the coordinare starts and completes its first reconciliation, **Then** that branch is rebased onto the current main (or routed to conflict resolution / blocked if the rebase cannot apply cleanly).
2. **Given** an in-flight branch that is already current with main, **When** the coordinare starts, **Then** no rebase is performed for it and its published branch is left untouched.
3. **Given** several in-flight branches when the coordinare starts and one of them fails to rebase, **When** reconciliation runs, **Then** the others are still rebased independently (one failure does not block the rest).

---

### User Story 2 - Conflicting in-flight branches are detected and acted on every cycle (Priority: P2)

Independent of whether the coordinare witnessed main move, a card whose published branch is reported by the hosting platform as un-mergeable (conflicting) or behind main is recognized as needing a rebase and is acted on, rather than being left to cycle on a branch that can never pass checks.

**Why this priority**: A restart is the most common cause but not the only one (a missed event, a merge performed out-of-band, a transient that dropped the edge). Treating the *state of the branch* — not only the *event of main moving* — as the trigger makes the healing robust to any cause. It builds on US1's machinery.

**Independent Test**: Put an in-flight branch into a conflicting state against main without the coordinare observing the merge that caused it; verify the coordinare detects the conflicting/behind state and initiates the same rebase/resolution path as US1.

**Acceptance Scenarios**:

1. **Given** an in-flight card whose branch the platform reports as conflicting with main, **When** a reconciliation cycle runs, **Then** the coordinare initiates a rebase for that branch.
2. **Given** an in-flight branch reported as behind main but still cleanly mergeable, **When** a cycle runs, **Then** the coordinare rebases it onto current main so checks run against an up-to-date base.
3. **Given** an in-flight branch that has no open pull request, **When** a cycle runs, **Then** the coordinare does not attempt to rebase it.

---

### User Story 3 - Operators can see and trust what was rebased (Priority: P3)

Every restart-, drift-, or conflict-driven rebase the coordinare initiates is recorded with enough context for an operator to understand what happened and why, carrying only non-sensitive identifiers.

**Why this priority**: The healing must be observable to be trusted and debugged, but it is not required for the healing itself to function. It rides on US1/US2.

**Independent Test**: Trigger a restart-driven rebase and verify an observability record is emitted naming the card, the branch, the old and new main reference, and the outcome — and that it contains no secret values.

**Acceptance Scenarios**:

1. **Given** the coordinare rebases a stranded branch on startup, **When** the rebase completes (clean, conflict, or failure), **Then** a record is emitted with the card identifier, branch name, prior and current main reference, and outcome.
2. **Given** any such record, **When** it is inspected, **Then** it contains only names, references, branch names, outcomes, and identifiers — never secret values.

---

### Edge Cases

- **Already-current branch**: a branch already on the current main is left untouched — no rebase, no force-publish, no churn.
- **No pull request / no branch**: a card with no open PR (or no remote branch) is skipped, not rebased.
- **Rebase thrash**: the coordinare must not re-rebase the same branch head every cycle. Once a branch has been rebased to the current main (or recorded as blocked-on-conflict for that main), it is not rebased again until its head or main changes.
- **Conflict that the worker cannot resolve**: falls back to an operator-visible blocked state (the existing 047 behavior), rather than looping.
- **Main moves again mid-reconciliation**: a newer main observed on a later cycle supersedes the prior target; the branch converges to the newest main without manual intervention.
- **Restart with no drift**: if main has not moved since the last persisted baseline and no branch is conflicting, startup performs no rebases (no false healing).
- **Platform mergeability not yet computed**: when the hosting platform has not finished computing a branch's mergeability, the coordinare treats the state as "unknown" and re-checks on a later cycle rather than force-rebasing on a guess.
- **A card not in a rebase-eligible lifecycle stage** (e.g. not yet dispatched, or already merging): excluded from proactive rebasing per the existing 047 scope.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: On startup, the coordinare MUST reconcile its notion of main against the live main so that a main advance which occurred while it was stopped is treated as drift to be acted on — NOT silently adopted as the new baseline with no rebase.
- **FR-002**: When startup drift is detected, the coordinare MUST rebase every in-flight branch that is behind or in conflict with the current main, using the existing rebase machinery (clean rebase → republish with lease; conflict → existing worker conflict-resolution path; unresolved → block the card for an operator).
- **FR-003**: Independent of whether a "main moved" event was observed during the current run, the coordinare MUST identify in-flight cards whose published branch is reported as conflicting with, or behind, the current main and initiate a rebase for them.
- **FR-004**: The coordinare MUST NOT rebase a branch that is already current with main, a card that has no open pull request, or a card outside the rebase-eligible lifecycle scope already defined by spec 047.
- **FR-005**: Rebase publication MUST always use a lease (never an unconditional force-publish), so a concurrent worker push is never silently overwritten.
- **FR-006**: A rebase failure or block on one card MUST NOT prevent the coordinare from rebasing the other in-flight cards (per-card isolation, inherited from 047).
- **FR-007**: The coordinare MUST avoid rebase thrash: a branch already rebased to (or recorded as blocked-on-conflict against) the current main MUST NOT be rebased again until its head or main changes.
- **FR-008**: The coordinare MUST emit an observability record whenever it initiates a startup-, drift-, or conflict-driven rebase, including the card identifier, branch name, prior and current main reference, and outcome.
- **FR-009**: Observability records and persisted state MUST carry only names, references, branch names, outcomes, and identifiers — never secret values (carried invariant from 088/090).
- **FR-010**: The persisted baseline of "last known main" (if used to satisfy FR-001) MUST load safely from snapshots written before this feature existed, defaulting to "unknown" so the first post-upgrade reconciliation treats the live main as the comparison point and heals any conflicting branch via FR-003 rather than crashing or skipping.
- **FR-011**: This feature MUST NOT change the merge-time rebase behavior of spec 047, the worker's conflict-resolution quality, or any hosting-platform CI configuration — it only adds restart/drift/conflict-driven *triggering* of the existing rebase path.

### Key Entities *(include if feature involves data)*

- **Last-known-main reference**: the main-branch reference the coordinare most recently reconciled against. To survive restarts it must be durable; on first read after an upgrade it is "unknown".
- **In-flight card branch**: the published branch backing an active card's pull request, with a current head and a mergeability state (current / behind / conflicting / unknown) relative to main.
- **Rebase outcome record**: the observable result of a triggered rebase — card identifier, branch name, prior and current main reference, and outcome (clean / conflict-routed / blocked / failed), free of secret values.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: After a restart that follows one or more merges to main, 100% of in-flight branches that are behind or conflicting are either rebased onto the current main or routed to conflict-resolution/operator-blocked within a bounded number of poll cycles — with zero operator actions.
- **SC-002**: An in-flight branch that is conflicting with main never remains in a "no checks can run" state indefinitely; the coordinare acts on it within a bounded number of cycles of the conflicting state being observable.
- **SC-003**: A branch already current with main incurs zero rebases and zero republishes across restarts and cycles (no churn, no thrash).
- **SC-004**: Across a reconciliation that includes a failing or blocked rebase, every other in-flight branch is still processed (one failure isolates to its own card).
- **SC-005**: 100% of triggered rebases produce an observability record naming the card, branch, prior/current main reference, and outcome, and 0% of those records contain secret values.
- **SC-006**: The merge-time spec-047 path and all existing rebase tests behave identically to before this feature (no regression to 047).

## Assumptions

- The hosting platform exposes, per pull request, a mergeability/aheadness signal sufficient to classify a branch as current / behind / conflicting / unknown; when that signal is "unknown" the coordinare defers rather than guessing.
- The existing 047 machinery (rebase round, per-card rebase, lease-based republish, worker conflict-resolution path, per-card isolation) is the substrate this feature triggers; this feature adds triggering conditions, not a new rebase implementation.
- The single-host, single-process JSON-snapshot state model is unchanged except for optionally persisting the last-known-main reference (a backward-compatible addition).
- "In-flight" means a card with an active session and an open pull request in a lifecycle stage that 047 already considers rebase-eligible.

## Dependencies

- Spec 047 (auto-rebase-on-merge) — provides the rebase round, per-card rebase, lease-based republish, and worker conflict-resolution path this feature triggers.
- Spec 094 (restart-session-reconciliation) — establishes the startup-reconciliation pass into which the restart drift check naturally fits.
- The hosting-platform read used to obtain the current main reference and per-PR mergeability state.

## Out of Scope

- Changing the merge-time rebase behavior of spec 047.
- Improving the quality of the worker's conflict resolution.
- Any hosting-platform CI/workflow configuration (the "no checks on an un-mergeable branch" behavior is the platform's, not this system's, to change).
- Auto-resolving genuine code conflicts beyond the existing 047 worker path.
