# Feature Specification: Async Multi-Card Orchestration Eligibility

**Feature Branch**: `054-async-multi-card-orchestration`  
**Created**: 2026-04-24  
**Status**: Draft  
**Input**: Coordinare should work on multiple cards asynchronously when they are not in the BLOCKED column and not blocked by issue/card dependencies.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Eligible cards advance concurrently (Priority: P1)

When several cards are simultaneously eligible, coordinare should process their session ticks concurrently instead of one-by-one.

**Why this priority**: Multi-card mode exists, but session graph invocation is still sequential, which limits throughput.

**Independent Test**: Run with `max_concurrent_cards=3` and three independent TODO cards; verify the three sessions are invoked in the same cycle and progress in parallel.

**Acceptance Scenarios**:

1. **Given** 3 eligible active sessions, **When** one cycle runs, **Then** all 3 are invoked asynchronously in that cycle.
2. **Given** one session invocation errors, **When** other sessions are eligible, **Then** they still complete their invocation in the same cycle.
3. **Given** `max_concurrent_cards=1`, **When** coordinare runs, **Then** behavior remains equivalent to single-session mode.

---

### User Story 2 - BLOCKED/dependency-blocked cards are not worked (Priority: P1)

Cards in BLOCKED, or cards with unresolved dependencies, should not consume performer/session execution time until they become eligible.

**Why this priority**: Prevents wasted cycles, token spend, and noise from repeatedly touching work that cannot proceed.

**Independent Test**: Prepare active sessions where one card is in BLOCKED and one has pending dependency blockers; verify neither is invoked while eligible sessions continue.

**Acceptance Scenarios**:

1. **Given** an active session whose card is in BLOCKED, **When** cycle runs, **Then** that session is skipped with a blocked reason.
2. **Given** an active session with unresolved dependency blockers, **When** cycle runs, **Then** that session is skipped with a dependency-blocked reason.
3. **Given** a previously blocked dependency becomes satisfied, **When** next cycle runs, **Then** session becomes eligible and resumes automatically.

---

### User Story 3 - Post-merge rebases are dispatched for in-flight PR branches (Priority: P1)

When any PR is merged into `main`, coordinare should trigger rebase handling for open in-flight feature PR branches so active work stays aligned with latest main.

**Why this priority**: In-flight branches can drift immediately after squash/merge and produce stale QA/review outcomes if rebases are delayed or skipped.

**Independent Test**: With multiple active sessions that have open PRs, merge another PR into `main`; verify coordinare dispatches rebase handling for eligible in-flight branches and records outcomes.

**Acceptance Scenarios**:

1. **Given** `main` SHA changes due to a merged PR, **When** next poll cycle runs, **Then** coordinare initiates rebase handling for eligible sessions with open PR branches.
2. **Given** a rebase conflict occurs for one branch, **When** rebase round completes, **Then** conflicted branch is marked blocked and non-conflicting branches continue.
3. **Given** no active open PR branches exist, **When** `main` changes, **Then** no rebase dispatch is attempted.

---

### User Story 4 - Operator can see why sessions were skipped (Priority: P2)

Dashboard/state should clearly show which sessions were skipped and why (blocked column, dependency blockers, at-capacity).

**Why this priority**: Without skip reasons, async orchestration becomes hard to diagnose.

**Independent Test**: Trigger one blocked skip and one dependency skip; verify snapshot/dashboard exposes per-card skip reasons.

**Acceptance Scenarios**:

1. **Given** a session was skipped for BLOCKED status, **When** snapshot is rendered, **Then** skip reason includes `blocked_column`.
2. **Given** a session was skipped for dependencies, **When** snapshot is rendered, **Then** skip reason includes blocker issue numbers.
3. **Given** no sessions were skipped, **When** snapshot is rendered, **Then** skip-reason map is empty.

---

### User Story 5 - QA validates branch freshness against latest main (Priority: P1)

QA for in-flight/open-PR work should explicitly check whether the branch contains latest `main` updates, so stale branches are caught before final completion.

**Why this priority**: Guarantees work that stayed in-flight during squash/merge events is verified against the latest baseline.

**Independent Test**: Run QA on an open PR branch that is behind `main`; verify QA fails freshness criterion. Rebase branch and rerun QA; verify criterion passes.

**Acceptance Scenarios**:

1. **Given** QA stage runs for a branch behind latest `main`, **When** QA evaluates freshness, **Then** it reports a failure requiring rebase/update.
2. **Given** branch includes latest `main` commit, **When** QA evaluates freshness, **Then** freshness criterion passes.
3. **Given** freshness data cannot be determined (API/git failure), **When** QA runs, **Then** QA reports an explicit environment/freshness blocker rather than silently passing.

---

### Edge Cases

- Dependency status changes during cycle: eligibility is re-evaluated on next poll; no manual restart required.
- Card moved out of BLOCKED externally: session should become eligible in next cycle.
- Mixed active set with blocked, dependency-blocked, and eligible cards: only eligible cards execute.
- One async session crashes: failure isolation must preserve execution of other sessions.
- Duplicate active session IDs: dedupe by card ID before scheduling.
- Multiple merges in quick succession: rebase rounds should coalesce against latest observed `main` SHA.
- QA freshness check runs while rebase is in progress: QA should defer/fail with explicit freshness-not-ready signal.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: Coordinare MUST compute per-session eligibility each cycle using board column status and dependency graph state.
- **FR-002**: Sessions whose cards are in `BLOCKED` MUST NOT be invoked during that cycle.
- **FR-003**: Sessions with unresolved dependencies (`pending` or `unresolvable`) MUST NOT be invoked during that cycle.
- **FR-004**: Eligible sessions MUST be invoked asynchronously in the same cycle with failure isolation.
- **FR-005**: Session invocation fanout MUST respect configured concurrency limits and MUST NOT exceed `max_concurrent_cards`.
- **FR-006**: A failed session invocation MUST NOT cancel or block other in-flight session invocations.
- **FR-007**: When a skipped session becomes eligible again, coordinare MUST resume it automatically without manual intervention.
- **FR-008**: State snapshot MUST expose per-card session skip reasons for observability (`blocked_column`, `dependency_blocked`, etc.).
- **FR-009**: Existing single-card compatibility (`max_concurrent_cards=1`) MUST remain intact.
- **FR-010**: When `main` advances (PR merged), coordinare MUST trigger rebase handling for eligible active sessions with open PR branches.
- **FR-011**: Rebase handling MUST be failure-isolated per branch: one branch conflict/failure MUST NOT stop rebase attempts for other branches.
- **FR-012**: QA stage MUST include a branch freshness check against latest known `main` SHA for open PR work.
- **FR-013**: QA freshness check MUST fail (or explicitly block) when branch is behind `main` or freshness cannot be verified.

### Key Entities

- **SessionEligibility**: Derived per-card status for a cycle. Fields include `card_id`, `eligible`, `reason`, and optional blocker details.
- **SessionSkipReasonMap**: Snapshot-safe map of `card_id -> reason/details` for sessions not invoked in the last cycle.
- **AsyncSessionTickResult**: Per-session invocation outcome capturing success, error, duration, and resulting phase.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: With 3 eligible active sessions, all 3 are invoked within one cycle iteration (no sequential-only fallback).
- **SC-002**: BLOCKED/dependency-blocked sessions receive zero performer status/dispatch calls while ineligible.
- **SC-003**: If one session invocation fails, at least one other eligible session still advances in the same cycle.
- **SC-004**: A card unblocked by dependency completion resumes within 2 poll cycles.
- **SC-005**: Dashboard/snapshot surfaces skip reason coverage for 100% of skipped sessions in test scenarios.
- **SC-006**: After a merged PR updates `main`, all eligible active open-PR sessions are evaluated for rebase within 2 poll cycles.
- **SC-007**: QA freshness criterion correctly fails for behind-main branches and passes for up-to-date branches in automated tests.

## Assumptions

- Current dependency graph services remain the source of truth for dependency status.
- Async session execution is bounded within one daemon process (no distributed worker redesign in this spec).
- No new persistence backend is required; skip-reason state can be in-memory and snapshot-derived.
