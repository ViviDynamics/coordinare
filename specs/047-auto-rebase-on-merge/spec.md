# Feature Specification: Auto-Rebase Active Branches on Merge

**Feature Branch**: `047-auto-rebase-on-merge`  
**Created**: 2026-04-16  
**Status**: Draft  
**Input**: When a PR merges to main, coordinare rebases every other in-flight PR branch onto the new main. Clean rebase → force-push-with-lease. On conflict → performer attempts resolution before falling back to blocking the card.

## User Scenarios & Testing *(mandatory)*

### User Story 1 — Clean rebase after a sibling PR merges (Priority: P1)

When the closer merges a PR into main, the coordinare identifies all other active sessions whose PR branches are based on an older main. For each, it rebases the branch onto the updated main, force-pushes with lease, and lets CI re-run. The affected card stays in its current lifecycle stage — the rebase is transparent to the performer pipeline.

**Why this priority**: In multi-card mode, stale branches are the most common source of merge conflicts that grow worse over time. Rebasing immediately after each merge keeps the drift minimal and reduces the chance of conflicts on the next card's merge.

**Independent Test**: Open two PRs (card A, card B) from the same main. Merge card A's PR. Verify card B's branch is rebased onto the new main within one poll cycle and the force-push succeeds. Verify CI re-runs on the updated branch.

**Acceptance Scenarios**:

1. **Given** card A's PR just merged to main and card B's PR branch is based on the pre-merge main, **When** the coordinare detects the merge, **Then** card B's branch is rebased onto the updated main and force-pushed with lease within one poll cycle.
2. **Given** a clean rebase with no conflicts, **When** the force-push completes, **Then** card B's lifecycle stage is unchanged (e.g., still in reviewing or monitoring_pr) and CI is triggered on the new head.
3. **Given** three in-flight PR branches when a merge occurs, **When** the coordinare rebases, **Then** all three are rebased independently (one failure does not block the others).

---

### User Story 2 — Performer resolves merge conflicts during rebase (Priority: P2)

When a rebase produces conflicts, the coordinare dispatches a performer (implementer role) to the conflicted branch with the conflict details. The performer reads the conflict markers, applies a resolution using its understanding of the codebase, commits the resolution, and completes the rebase. If the performer cannot resolve the conflicts (e.g., the changes are semantically incompatible), only then is the card blocked with a diagnostic message showing which files conflicted and the conflict content.

**Why this priority**: Blocking on every conflict defeats the purpose of automated parallel work. Most rebase conflicts are mechanical (import ordering, adjacent-line edits, lockfile drift) and a capable AI performer can resolve them in seconds. Only genuinely ambiguous semantic conflicts should escalate to a human.

**Independent Test**: Create two PRs that edit the same file in non-overlapping but adjacent lines (guaranteed rebase conflict). Merge one. Verify the performer resolves the conflict on the other PR's branch and the rebase completes. Then repeat with a genuinely ambiguous conflict (two PRs changing the same function signature differently) and verify the card is blocked with conflict details.

**Acceptance Scenarios**:

1. **Given** a rebase conflict on card B's branch involving adjacent-line edits in the same file, **When** the performer is dispatched to resolve it, **Then** the conflict is resolved, the rebase completes, and the branch is force-pushed.
2. **Given** a rebase conflict the performer cannot resolve (semantically incompatible changes), **When** the performer gives up, **Then** the card is blocked with a comment listing the conflicted files, the conflict content (truncated), and the reason it couldn't be auto-resolved.
3. **Given** a rebase conflict on one of three in-flight branches, **When** the performer resolves it, **Then** the other two branches (which had clean rebases) are unaffected and already force-pushed.

---

### User Story 3 — Operator notified of rebase outcomes (Priority: P3)

After each merge triggers rebases, the operator receives a summary in Slack and on the dashboard: which branches were rebased cleanly, which had conflicts resolved by the performer, and which were blocked. The dashboard shows the rebase status per active card.

**Why this priority**: In multi-card mode the operator needs to know that branches are staying fresh without manually checking each PR. A single post-merge summary replaces N individual checks.

**Independent Test**: Merge a PR while two other cards are in flight. Verify Slack receives one summary message listing both branches and their rebase outcome. Verify the dashboard shows "rebased at [commit]" or "conflict — blocked" per card.

**Acceptance Scenarios**:

1. **Given** a merge triggers rebases on two in-flight branches (one clean, one conflicted), **When** rebases complete, **Then** Slack receives a single summary: branch A rebased cleanly, branch B blocked on conflict in `file.py`.
2. **Given** the dashboard is open, **When** a rebase completes, **Then** the affected card's status tile shows the rebase outcome (clean / performer-resolved / blocked) and the new head SHA.

---

### Edge Cases

- **No other in-flight branches**: Merge completes; rebase step is a no-op (nothing to rebase). No Slack summary posted.
- **Branch already up-to-date**: The in-flight branch was created after the merge commit (e.g., a card just dispatched). Rebase is a no-op — detect via merge-base check and skip.
- **Force-push rejected (lease fails)**: Another push landed on the branch between the rebase and the force-push (e.g., a performer committed concurrently). Retry the rebase once from the new head. If it fails again, block the card.
- **Rebase during active performer session**: A performer is mid-work when the rebase fires. The rebase should wait until the performer's current dispatch completes (session is not actively running) before rebasing. Do not rebase a branch while a performer is writing to it.
- **Protected branch rules on PR branches**: Force-push-with-lease on PR head branches should work by default (GitHub allows force-push to non-default branches). If it fails due to branch protection, block the card and surface the error.
- **Merge of a non-coordinare PR**: If a human merges a PR to main outside the coordinare, the next poll cycle should still trigger rebases for all in-flight coordinare branches.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: After any PR merges to the default branch (main), the coordinare MUST identify all other active sessions with open PRs whose branches are behind the new main.
- **FR-002**: For each stale branch, the coordinare MUST rebase it onto the updated main and force-push with lease.
- **FR-003**: If the rebase produces conflicts, the coordinare MUST dispatch a performer (implementer role) to attempt automated conflict resolution before blocking the card.
- **FR-004**: The performer MUST be given the list of conflicted files and the raw conflict markers so it can read, resolve, and commit the resolution.
- **FR-005**: If the performer cannot resolve the conflicts, the card MUST be blocked with a diagnostic comment listing the conflicted files, truncated conflict content, and a clear explanation.
- **FR-006**: The coordinare MUST NOT rebase a branch while a performer is actively running against that branch (active session with a running backend). The rebase waits until the session completes or expires.
- **FR-007**: Each branch MUST be rebased independently — a failure on one branch does not prevent rebasing the others.
- **FR-008**: After a successful rebase + force-push, the card's lifecycle stage MUST remain unchanged (the rebase is transparent to the pipeline).
- **FR-009**: A Slack summary MUST be posted after the rebase round completes, listing each branch and its outcome (clean / performer-resolved / blocked).
- **FR-010**: The dashboard MUST show the rebase status per active card: last rebase timestamp, outcome, and new head SHA.

### Key Entities

- **RebaseJob**: Represents a single rebase operation for one in-flight branch. Contains: card ID, branch name, PR number, merge-base before rebase, target main SHA, outcome (clean / conflict-resolved / blocked), conflicted files (if any).
- **RebaseRound**: The set of RebaseJobs triggered by a single merge event. Used for the Slack summary and dashboard display. Contains: triggering merge PR number, timestamp, list of RebaseJobs.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: After a merge, all stale in-flight branches are rebased within 2 poll cycles (under 2 minutes at default 30s interval) when no conflicts exist.
- **SC-002**: At least 80% of rebase conflicts from adjacent-line and import-ordering changes are resolved automatically by the performer without human intervention.
- **SC-003**: When a conflict cannot be auto-resolved, the blocked-card comment contains enough information (file paths, conflict content) for a human to resolve it without running `git rebase` locally.
- **SC-004**: No in-flight branch is rebased while a performer is actively running against it (zero concurrent-write incidents).
- **SC-005**: Operator receives a single Slack summary per merge event (not per branch) within 30 seconds of all rebases completing.

## Assumptions

- Force-push-with-lease is permitted on PR head branches in the configured repository. If branch protection rules prevent this, the card is blocked (not a coordinare bug — a configuration issue).
- The performer dispatched for conflict resolution uses the same backend and persona as the implementer role. No separate "rebase specialist" persona is needed.
- "Multi-card mode" refers to coordinare operating with `max_concurrent_cards >= 2` (existing feature from spec 035). This spec does not require horizontal performer scaling (spec 048) — it works with a single performer transport per role.
- Only branches managed by the coordinare (matching the `coordinare/PVTI_...` naming convention) are rebased. External branches on the same repo are not touched.
