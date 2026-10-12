# Feature Specification: Orphaned PR reconciliation for completed cards

**Feature Branch**: `553-orphan-pr-reconciliation`
**Created**: 2026-10-11
**Status**: Draft
**Input**: User description: "Reconcile orphaned open PRs of completed cards: completion-time closing of superseded PRs and a config-gated janitor sweep with explanatory comments (issue #78)"

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Superseded PRs close when the card completes (Priority: P1)

A card's lifecycle can leave more than one open PR behind: each scenario re-run of a story creates a new card identity (new board item, new branch, new PR), and the losing PR stays open after the winning PR merges and the card completes. An operator who closes a card should never also have to hand-close the losing PRs. When a card completes (its PR merged, issue closed, board DONE), every other open PR attributable to that card is closed automatically with a comment explaining why.

**Why this priority**: This is the primary leak. Without it, every re-run of a story leaves a stale open PR, and the mismatch between "board says Done" and "PR list says open" is what confused operators in the live incident.

**Independent Test**: Can be fully tested by simulating a card with two open PRs where one merges: the completion step closes the other and posts the comment, delivering a repo where the PR list matches the board.

**Acceptance Scenarios**:

1. **Given** a card with one open PR that is about to merge and a second open PR attributable to the same card, **When** the merge succeeds and the card completes, **Then** the second PR is closed by the daemon with a comment naming the merged (superseding) PR, the card's terminal state, and the reason.
2. **Given** a card with exactly one open PR that merges, **When** the card completes, **Then** no additional PR-closing action occurs (nothing to reconcile) and no error is emitted.
3. **Given** a card whose merge attempt does not complete (e.g. CI still red), **When** the cycle ends, **Then** no PR of that card is closed (reconciliation only follows a completed card).

---

### User Story 2 - Janitor sweep heals historical orphans (Priority: P2)

Open PRs left behind by past runs (before any fix, or left by restarts mid-flight) still need reconciliation. Once per cycle, the daemon scans open PRs in the target repository, resolves each to its board card, and closes those whose card is in a terminal state (DONE/CLOSED) with an explanatory comment. This must work regardless of branch-name prefix: older eras used a different branch prefix than current cards, and matching by prefix alone would miss them.

**Why this priority**: This is what actually cleans up the live incident's PR21 and PR3 without operator intervention, and it is the safety net for every future restart or race the completion-time path cannot see.

**Independent Test**: Can be fully tested by pre-seeding a repo with open PRs whose cards are already Done, enabling the sweep, and observing one cycle: orphans close with comments; nothing else changes.

**Acceptance Scenarios**:

1. **Given** an open PR whose board card is DONE and whose card is not in the active-session map, **When** the sweep runs, **Then** the PR is closed with a comment identifying the card, its terminal state, and the superseding merged PR when one exists.
2. **Given** an open PR whose board card is in an active state (TODO/IN_PROGRESS/IN_REVIEW/BLOCKED) or which has a live session, **When** the sweep runs, **Then** the PR is left untouched.
3. **Given** an open PR that cannot be resolved to a board card with confidence, **When** the sweep runs, **Then** the PR is skipped (never closed) and the skip is logged.
4. **Given** two open PRs attributable to the same completed card, **When** the sweep runs, **Then** both are closed in the same sweep (not one per cycle).

---

### User Story 3 - Operator opt-in with observable decisions (Priority: P3)

Both behaviors are opt-in and off by default, so a default deployment behaves byte-identically to today. Each reconciliation decision is emitted as a structured log line (per-PR outcome: closed, close-failed, skipped-active, skipped-unresolved, disabled, skipped-comment-failed) so an operator can audit exactly why any PR was or was not closed.

**Why this priority**: The 090 precedent: autonomy layers default off so routing, verdicts, and merge decisions stay identical until explicitly enabled. Observability without action would not clean anything up, and action without observability would not be auditable.

**Independent Test**: Can be fully tested by running the daemon with the feature disabled and confirming zero new events, zero PR closures, and byte-identical cycle behavior; then enabling it and confirming the decision log lines appear.

**Acceptance Scenarios**:

1. **Given** the feature disabled in config, **When** the daemon runs any number of cycles, **Then** no PR is closed, no reconciliation comment is posted, and no reconciliation decision is logged.
2. **Given** the feature enabled, **When** the sweep evaluates a candidate, **Then** a structured log line records the PR, the resolution basis, and the decision.
3. **Given** a transient failure while closing a PR (network, permission), **When** the sweep runs, **Then** the failure is logged, the PR is left open, and the daemon continues (no crash, no partial state).

---

### Edge Cases

- How does the system handle an open PR whose head branch was deleted? It is still attributable to the card and still gets reconciled (the comment notes the deleted branch).
- How does the system handle a PR attributable to a card whose board item cannot be read (permission, archived item)? Skip, log `skipped-unresolved`, never close.
- What about a PR opened by a human against a completed card (e.g. a follow-up)? Card identity, not author identity, drives the decision; the comment states the card is complete so the author can redirect.
- What if the merge that completed the card is ambiguous (multiple merged PRs for the same card)? The comment names the most recently merged PR and links the others.
- What if the sweep and the completion path race on the same PR? Closure is idempotent: closing an already-closed PR is a no-op, not an error.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: System MUST, at card completion, close every open PR attributable to that card other than the PR that completed it, each with a posted comment stating the card is complete, naming the superseding (merged) PR, and giving the reason for closure.
- **FR-002**: System MUST provide a per-cycle janitor sweep that closes open PRs whose resolved board card is in a terminal state (DONE/CLOSED), each with the same explanatory comment.
- **FR-003**: Attribution MUST NOT rely solely on the current canonical branch prefix; it MUST also recognize PRs from prior eras' branch naming and PRs linked by issue/body reference.
- **FR-004**: The feature MUST be opt-in via configuration, default disabled, with independent switches for completion-time reconciliation and the janitor sweep.
- **FR-005**: System MUST emit one structured-log decision per evaluated PR with outcome vocabulary `closed | close-failed | skipped-active | skipped-unresolved | disabled | skipped-comment-failed`, including the PR number, the resolution basis, and the superseding PR reference when one exists.
- **FR-006**: System MUST never close a PR whose card is active (has a live session or is in a non-terminal board column).
- **FR-007**: System MUST fail safe: resolution or lookup failures skip the candidate (log and continue), and closure is idempotent.
- **FR-008**: The reconciliation comment MUST identify the daemon (attribution marker consistent with existing lifecycle comments), the card, the superseding PR, and the reason.

### Key Entities *(include if feature involves data)*

- **OrphanPrCandidate**: an open PR plus its resolution basis — the board card it belongs to, the card's terminal state, and the superseding merged PR when known.
- **ReconciliationDecision**: the per-PR outcome (`closed | close-failed | skipped-active | skipped-unresolved | disabled | skipped-comment-failed`) with the reason and the evidence used to decide.
- **ReconciliationConfig**: the opt-in switches (enabled, completion-time switch, sweep switch) and their defaults (off).

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: With the feature enabled, live orphans PR21 and PR3 in conductor-e2e-sample are closed by the daemon with comments naming the superseding merged PRs (22 and 2 respectively) within one daemon cycle of enablement.
- **SC-002**: Zero PRs belonging to active cards are closed across the validation run (audited via decision logs).
- **SC-003**: With the feature disabled, daemon behavior is byte-identical to a pre-feature build: no new events, no closures, no decision logs.
- **SC-004**: Every evaluated candidate produces exactly one decision log line, so an operator can reconcile the sweep's actions against the PR list with no unexplained closures.
