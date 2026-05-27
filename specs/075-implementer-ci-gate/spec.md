# Feature Specification: Implementer CI Gate

**Feature Branch**: `075-implementer-ci-gate`
**Created**: 2026-05-27
**Status**: Draft
**Input**: User description: Implementer personas sometimes declare "done" before CI is green (or before required checks have even started). Prior attempts to fix this via prompting have been unreliable. Coordinare must enforce CI status at the implementer→reviewer handoff boundary, the same way the closer pr-checks gate (spec 064) enforces it at the merge boundary. Implementer's self-reported "done" stops being load-bearing because coordinare verifies it. Depends on 074's classifier output for per-card required-checks lists.

## User Scenarios & Testing *(mandatory)*

### User Story 1 — Red CI bounces implementer back (Priority: P1)

Implementer pushes a commit, declares the card done, and hands off. CI is failing on the head commit (test failure, lint failure, type error). Today the card advances to reviewer, reviewer wastes a full pass on a broken branch, and the failure surfaces only at closer (or worse, at human review).

**Why this priority**: This is the most common, most visible, and most wasteful failure mode. Solving it alone makes the rest of the pipeline trustworthy.

**Independent Test**: Submit a card whose head commit has a failing required check; verify implementer's terminal signal does NOT advance the stage, the card is bounced back to implementer with structured feedback naming the failing check(s) and a link to the failure log, and the bounce is logged for audit.

**Acceptance Scenarios**:

1. **Given** an implementer has produced a terminal response and pushed HEAD, **When** any required check on HEAD has conclusion `failure`, `timed_out`, or `cancelled`, **Then** coordinare MUST NOT advance the lifecycle and MUST re-dispatch implementer with feedback listing each failing check by name and URL.
2. **Given** the same card after the bounce, **When** implementer pushes a fix and CI turns green, **Then** the next terminal signal advances the stage normally.
3. **Given** repeated bounces (e.g., 3+ in a row on the same failing check set), **When** the bounce limit is exceeded, **Then** the card transitions to the existing blocked path (Slack notification, human override).

---

### User Story 2 — Pending CI waits, doesn't advance (Priority: P1)

Implementer pushes and signals terminal while CI is still running. Today the card advances; reviewer either races CI or runs against a state that hasn't been validated.

**Why this priority**: Without this, the gate has a race condition where green-after-handoff and red-after-handoff are indistinguishable from "no check ran yet." Closes a correctness loophole that prompting cannot reliably handle.

**Independent Test**: Submit a card whose required checks are in `queued` or `in_progress` on HEAD at terminal time; verify coordinare waits (configurable budget) for them to complete before deciding advance vs. bounce.

**Acceptance Scenarios**:

1. **Given** required checks on HEAD are still running at implementer terminal time, **When** the gate evaluates, **Then** the card remains in `implementing` stage and coordinare re-polls on each cycle until all required checks reach a terminal conclusion or the wait budget is exceeded.
2. **Given** the wait budget is exceeded (default: matches the closer-gate timeout from 064), **When** the gate re-evaluates, **Then** the card is treated as a CI failure for bounce purposes and feedback names the timed-out checks.

---

### User Story 3 — Per-card required-checks list comes from 074's classifier (Priority: P2)

A docs-only card shouldn't wait for the full integration test suite; a runtime change must. The classifier introduced in 074 already inspects the diff and path classes; it can emit a `required_checks` list per card.

**Why this priority**: Without per-card scoping, every card waits for every check, which negates 074's throughput wins on trivial diffs. Lower than P1 only because a sensible default (wait for all required checks GitHub already marks as required) is acceptable as a fallback.

**Independent Test**: Submit a docs-only card and a runtime card on the same project; verify the gate applies a different required-checks list to each based on 074's classifier output, and that the project's GitHub branch-protection "required checks" set is used as the fallback when classifier output is absent.

**Acceptance Scenarios**:

1. **Given** 074's classifier has emitted a `required_checks` list for a card, **When** the gate evaluates, **Then** only those checks are considered required.
2. **Given** 074's classifier did not run or did not emit `required_checks` (e.g., 074 is disabled, classifier failed), **When** the gate evaluates, **Then** the gate falls back to the project's GitHub branch-protection required-checks set.
3. **Given** the project has no branch-protection required checks configured, **When** the gate evaluates, **Then** all checks present on HEAD are considered required (most conservative).

---

### User Story 4 — Gate state is auditable on the PR (Priority: P2)

A human reviewer (or a future operator debugging a stalled card) needs to see why the implementer stage didn't advance. "Implementer claimed done but CI was red" must be visible without reading coordinare logs.

**Why this priority**: Operational transparency. The closer gate (064) already surfaces its state on the PR; the implementer gate must too, or stalls look unexplained.

**Independent Test**: Force a bounce; verify a PR comment (or equivalent durable artifact) records the gate evaluation: required checks consulted, statuses observed, decision (bounce / wait / advance), and timestamp.

**Acceptance Scenarios**:

1. **Given** the gate decides to bounce or wait, **When** the decision is recorded, **Then** a structured artifact is posted to the PR identifying the failing/pending checks and the decision.
2. **Given** multiple consecutive evaluations on the same head commit yield the same decision, **When** the artifact would be posted again, **Then** it is deduplicated (no comment spam).

---

### Edge Cases

- **No PR open yet**: Implementer signals terminal before pushing or before the PR exists. Gate has nothing to evaluate; treated as a bounce with feedback "no PR / no HEAD to gate on — push first." This is a softer failure mode than CI-red and may be merged with existing 070 implementer-commit-floor logic.
- **Detached HEAD / force-pushed after terminal**: Gate evaluates against the current PR HEAD at evaluation time, not the HEAD claimed at terminal time. A force-push that resets HEAD mid-evaluation causes a re-evaluation, not a stale decision.
- **Check that GitHub reports as `neutral` or `skipped`**: Treated as non-blocking (not a failure, not a pending). Matches GitHub's own merge-eligibility semantics and avoids fighting CI configurations that intentionally skip irrelevant jobs.
- **CI explicitly marked as not-required (074 says "skip integration suite for this card") but the suite ran anyway and failed**: Failure is not blocking. The gate respects the per-card required-checks list, not GitHub's universal required set. Failed-but-not-required checks are surfaced to reviewer as advisory, not gated.
- **Bounce vs blocked distinction**: A bounce returns the card to implementer with actionable feedback. The blocked path (existing) is for cards that have exceeded the bounce limit or hit a non-actionable condition. The gate MUST distinguish these and not collapse bounce into block.
- **GitHub API rate limit / outage during evaluation**: Treated as transient. Card stays in `implementing` with a debug log; re-evaluated on next cycle. A sustained outage emits the same kind of warning 074's classifier-failure path uses.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: Coordinare MUST evaluate a CI gate at the implementer→reviewer handoff boundary. Implementer's terminal signal alone MUST NOT advance the lifecycle.
- **FR-002**: The gate MUST consult GitHub's check status on the current PR HEAD at evaluation time (not the HEAD claimed by the implementer at terminal time) using the existing GitHub service / `httpx` plumbing.
- **FR-003**: When any required check on HEAD has terminal conclusion of `failure`, `timed_out`, or `cancelled`, the gate MUST bounce the card back to implementer with structured feedback naming each failing check (name + URL).
- **FR-004**: When any required check on HEAD is in non-terminal state (`queued`, `in_progress`, missing), the gate MUST keep the card in `implementing` and re-evaluate on the next cycle.
- **FR-005**: The wait budget for non-terminal checks MUST be configurable (default: align with 064's closer-gate timeout). Exceeding the budget MUST be treated as a failure for bounce purposes.
- **FR-006**: The per-card required-checks list MUST be sourced in priority order: (1) 074's classifier output if present and well-formed, (2) the PR's GitHub branch-protection required-checks set, (3) all checks present on HEAD (most conservative fallback).
- **FR-007**: GitHub check conclusions of `neutral`, `skipped`, or `success` MUST NOT block the gate.
- **FR-008**: Consecutive bounces on the same head commit MUST be counted; when a configurable bounce limit is exceeded (default 3), the card MUST transition to the existing blocked path, not loop indefinitely.
- **FR-009**: The gate MUST post a durable, deduplicated artifact to the PR recording each non-pass decision (bounce or wait-timeout) including: required checks consulted, statuses observed, decision, timestamp.
- **FR-010**: Bounce feedback to the implementer MUST be structured (failing-check name + URL + last-line-of-log when retrievable) and delivered via the existing relay/feedback mechanism — not free-prose prompt injection.
- **FR-011**: GitHub API errors during evaluation MUST be treated as transient. The card MUST remain in `implementing` and re-evaluation MUST occur on the next cycle. Sustained errors MUST emit a rate-limited warning consistent with 074's classifier-failure handling.
- **FR-012**: Gate evaluation state MUST be observable in coordinare logs with the same structlog conventions used by 064's closer gate.
- **FR-013**: The gate MUST be a no-op (always advance, never bounce) when the card has no associated PR / no HEAD. In that case the gate defers to existing implementer-commit-floor logic (070).
- **FR-014**: When 074's classifier emits `required_checks` and the gate observes additional non-required checks failing on HEAD, the gate MUST advance the lifecycle but MUST attach the advisory failures to the reviewer-stage context.
- **FR-015**: The bounce counter MUST reset on a new head commit (a push that changes HEAD). A failing check that's re-pushed against gets the full bounce-budget again.

### Key Entities

- **CIGateDecision**: Output of one gate evaluation. Shape: `{ decision: advance|wait|bounce|block, required_checks_source: classifier|branch_protection|all_checks, checks: [{name, status, conclusion, url}], advisory_failures: [...], evaluated_at: timestamp, head_sha: string }`. Persisted enough to dedupe PR-artifact posts.
- **BounceCounter**: Per-card, per-head-SHA counter incremented on each bounce. Persisted on `CardSession` and round-tripped via `_SESSION_FIELDS`. Resets when head SHA changes.
- **RequiredChecksList**: Either sourced from 074's `PersonaScope` classifier output, the project's GitHub branch-protection settings, or "all checks on HEAD" as fallback. The source MUST be recorded in `CIGateDecision` for audit.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Zero cards advance from `implementing` to `reviewing` with required CI checks in a non-success terminal conclusion on HEAD. Measured by post-hoc audit over a rolling window of merged cards.
- **SC-002**: Implementer→reviewer handoff with green CI on a docs-only card (where 074's classifier narrows the required-checks list) completes within the same wall-clock budget as today's behavior plus the gate-evaluation overhead (≤2s p50 / ≤10s p95).
- **SC-003**: A simulated bounce loop converges to either green CI (advance) or the blocked path within the configured bounce limit; no card loops indefinitely on the same failure.
- **SC-004**: A reviewer reading a PR can determine, from PR-visible artifacts, every gate decision and its rationale without reading coordinare logs.
- **SC-005**: A simulated GitHub API outage during gate evaluation does not advance the card, does not bounce it, and does not stall it indefinitely — re-evaluation on the next cycle is sufficient.

## Dependencies

- **074-persona-scope-tiering**: This feature consumes 074's classifier output for the per-card `required_checks` list (FR-006). 075 MUST function with 074 disabled (fallback to branch-protection set), so the dependency is soft — but the throughput win of "docs-only cards don't wait for the integration suite" only materializes when 074 is enabled.
- **064-closer-pr-checks-gate**: Reuses 064's PR-checks rollup primitives, timeout budget conventions, and structured logging patterns. The closer gate gates merge; this gates handoff. They are parallel mechanisms at different lifecycle boundaries — they MUST NOT be merged into one.
- **070-implementer-commit-floor**: 075's "no PR / no HEAD" path defers to 070's existing logic. Where 070 ensures a commit exists, 075 ensures CI passed on it.

## Out of Scope

- **Replacing or merging with the closer pr-checks gate (064)**: Different boundary, different decision, different failure mode. Keep them parallel.
- **Self-healing CI failures by re-running flaky jobs**: 075 reports failures to implementer; it does NOT decide a job is flaky and retry it. That's a separate concern (and dangerous to automate without explicit signal).
- **Modifying the implementer's persona prompt to be "stricter" about CI**: Prompting has been tried and doesn't reliably work — that's the motivation for this spec. The fix is enforcement, not prompting. Structured feedback on bounce is allowed (FR-010); freeform "remember to check CI!" prompt additions are out of scope.
- **Cross-PR CI dependencies** (e.g., this PR depends on another PR's checks): Gate evaluates only the card's own PR.
- **Branch-protection enforcement at the GitHub level**: We're not configuring repo settings; we're observing them and consuming the required-checks list they expose.
