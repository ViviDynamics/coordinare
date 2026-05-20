# Feature Specification: QA Cycle 065

**Feature Branch**: `065-qa-cycle`
**Created**: 2026-05-17
**Status**: Draft — actively collecting QA findings
**Input**: User description: "Another QA and optimization iteration on coordinare, started during live testing after the 063/064 env-cache inference and PR-checks gate work landed. Seed bug: the dashboard's **Active Performers** panel shows status/card fields that lag what the daemon and performer actually report. More findings will be appended as testing continues."

## User Scenarios & Testing *(mandatory)*

### User Story 1 — Active Performers Panel Reflects Real Performer State (Priority: P1)

An operator watching the dashboard during a live run sees the **Active Performers** panel render a performer tile, but the tile's status fields — the `performer_stage` label ("architecting", "implementing", "reviewing", …) and, in some cases, the card-level fields (`card_title`, `issue_number`) attached to the performer — disagree with what the daemon log and the performer container itself report at the same moment. The performer has advanced one or more stages, or has moved on to a different card, but the panel is still showing the prior state.

The build path explains the lag: `active_sessions` is mutated by the daemon as the performer reports progress, but the dashboard snapshot is only rebuilt and broadcast over SSE at the end of each daemon cycle (`src/coordinare/daemon.py:1655`, `src/coordinare/dashboard.py:292`–`500`). When a cycle is long — multi-card orchestration, a slow GitHub poll, a `pr-checks` gate that waits on a 90 s timeout — the panel can be tens of seconds stale, and during a stage transition the panel can show an obsolete `performer_stage` for the full duration of the next cycle even though the daemon already knows the new value.

The desired behaviour: the panel reflects the daemon's current view of each active performer within a small, bounded delay (≤5 s under nominal load) regardless of cycle length. The operator should not be able to read a `performer_stage` from the panel and have it contradict the contemporaneous daemon log line for that session.

**Why this priority**: The Active Performers panel is the primary live-status surface during operator-supervised runs. A panel that lies — even briefly — undermines trust in the rest of the dashboard and pushes operators back to tailing logs. This is a small, contained correctness fix on a hot UI path.

**Independent Test**: Dispatch a card on a symphony whose performer takes more than one cycle to advance stages (e.g. architect → implement → review). With the dashboard open, capture the panel's reported `performer_stage` against the daemon's structured-log `performer_stage` field at 1 s intervals for the duration of the card. The maximum observed lag MUST be ≤5 s; intra-cycle stage transitions MUST be reflected without waiting for the next full daemon cycle.

**Concrete Repro (multi-card)**: Two cards A and B are simultaneously in `active_sessions`. Card A is kicked back to `TODO` (its session is removed from `active_sessions`) while card B is still being implemented and advances stage. Observed behaviour: the Active Performers panel continues to render the performer tile with card A's title / card-level fields even though the daemon's `active_sessions` now contains only B (with an updated `performer_stage`). The desired behaviour: within one SSE broadcast of the daemon mutating `active_sessions`, the panel shows exactly the surviving session(s) — no tile referencing a removed `card_id`, no `performer_stage` from the prior snapshot.

**Acceptance Scenarios**:

1. **Given** a performer transitions from `architecting` to `implementing` mid-cycle, **When** the dashboard receives the next SSE update, **Then** the rendered `performer_stage` matches the daemon's current session state and not the prior value.
2. **Given** a daemon cycle takes longer than 5 s (e.g. blocked on a GitHub call), **When** the operator views the Active Performers panel, **Then** the panel is either updated within 5 s of the underlying state change OR carries a visible "last update" indicator so the operator knows the panel itself is stale rather than the performer.
3. **Given** a session has been removed from `active_sessions` because the card completed, **When** the next snapshot is broadcast, **Then** the corresponding performer tile disappears from the panel; no completed-card tile lingers.

---

## Requirements

### Functional Requirements

- **FR-001**: The dashboard SSE channel MUST broadcast an updated snapshot whenever a session's `performer_stage`, `phase`, or `card_id` changes, without waiting for the end of the daemon's processing cycle.
- **FR-002**: The broadcast pathway MUST coalesce rapid successive mutations so that the SSE stream is not flooded — multiple updates within a short window (target: 500 ms) MAY be collapsed into a single broadcast, but the final state MUST be sent.
- **FR-003**: If the implementation cannot guarantee a ≤5 s end-to-end staleness bound (e.g. due to coalescing or backpressure), the panel MUST render a visible "updated N seconds ago" indicator sourced from the snapshot's broadcast timestamp, so the operator can distinguish "performer is stuck" from "panel is stale".
- **FR-004**: When a session is removed from `daemon.state["active_sessions"]`, the next snapshot MUST omit it; no client-side caching MAY keep a removed performer visible.
- **FR-005**: A regression test under `tests/unit/` MUST exercise the mid-cycle update path: simulate a `performer_stage` change on an in-flight session and assert that a broadcast is produced before the cycle-completion broadcast.

### Success Criteria

- **SC-001**: Under live load with at least one active performer, the wall-clock delta between a `performer_stage` change in the daemon log and the corresponding update in the Active Performers panel is ≤5 s at the 95th percentile.
- **SC-002**: No completed performer tile remains visible on the panel more than one SSE broadcast after its session is removed from `active_sessions`.
- **SC-003**: Existing dashboard tests continue to pass; SSE consumers built against the pre-065 snapshot schema continue to receive valid snapshots (no breaking field changes, only timing changes).

---

## Out of Scope

- Rearchitecting the dashboard snapshot pipeline (e.g. replacing the SSE channel with WebSockets, or moving to per-field deltas). The fix in this round targets broadcast timing, not transport.
- Adding new fields to the performer tile (per-stage timestamps, token cost deltas, etc.). Those are dashboard UX work, not staleness fixes, and belong under a separate spec if surfaced.
- Server-side push from the performer container directly to the dashboard. The daemon remains the single source of truth for session state.
- Historical playback / scrubbing of past performer state. The panel is a live view only.

---

### User Story 2 — Multiple Cards Are Actually Worked Simultaneously (Priority: P1)

During live testing, the operator could not get coordinare to work more than one issue card at a time even though the symphony has multiple performers registered and the board has several `TODO` cards available. The expectation set by spec 054 (async multi-card orchestration) and 048 (horizontal performer scaling) is that a symphony with N idle performers should pick up to N eligible cards per cycle, not serialise them.

The observable behaviour: only one performer tile ever appears on the Active Performers panel; remaining idle performers are visible on `/performers` as registered but never get a card; the queue of `TODO` cards drains one at a time even with no apparent dependency or assignee filter blocking parallel dispatch.

The desired behaviour: under nominal load, with M idle eligible performers and K eligible `TODO` cards, the daemon dispatches `min(M, K)` cards within a single cycle, and the Active Performers panel shows that many concurrent tiles.

**Why this priority**: Multi-card parallelism is a load-bearing assumption of the symphony model. If it's silently serialising, throughput is far below capacity and the env-cache/RTK optimisations from 060/062 are being measured against the wrong baseline. This story is intentionally framed as *investigate and fix* — the root cause may be a dispatcher-side bug (early-exit after first dispatch), a card-eligibility predicate that double-counts in-flight work, an assignment race, or simple misconfiguration; we'll narrow it down during implementation.

**Independent Test**: On a symphony with at least two idle performers registered and at least two `TODO` cards that have no dependency or assignee restriction, start the daemon and let one full cycle complete. After the cycle, `daemon.state["active_sessions"]` MUST contain at least two entries with distinct `card_id` values; the dashboard's Active Performers panel MUST render at least two tiles. If only one card was picked up, the test fails and the daemon logs MUST include a structured reason for why each remaining idle performer was not assigned a card.

**Acceptance Scenarios**:

1. **Given** two idle performers and two eligible `TODO` cards, **When** one daemon cycle completes, **Then** both cards are in `active_sessions` with distinct `card_id`s and both performers appear on the panel.
2. **Given** an eligible performer is *not* assigned a card during a cycle, **When** the daemon logs that cycle, **Then** it MUST emit a structured `dispatcher.skip` (or equivalent) record naming the performer and the reason (e.g. `no_eligible_cards`, `assignee_filter`, `dependency_blocked`, `capacity_cap`), so the silent-serialisation failure mode is impossible.
3. **Given** the investigation determines the existing behaviour is intentional (e.g. a deliberate capacity cap configured elsewhere), **When** the spec is closed, **Then** the cap location and rationale are documented in `quickstart.md` or AGENTS.md and the panel surfaces the configured cap so the operator is not misled.

---

## Requirements *(continued — User Story 2)*

### Functional Requirements

- **FR-006**: A structured `dispatcher.skip` log record MUST be emitted for every eligible-but-unassigned performer at the end of each cycle, with fields `{performer_id, reason, eligible_card_count, in_flight_card_count}`. Reason values are a closed enum.
- **FR-007**: A regression test MUST construct a daemon state with two idle performers and two TODO cards (no dependencies, no assignee filter), invoke one cycle, and assert that two distinct sessions are created. The test MUST also assert the absence of any `dispatcher.skip` record for the cycle.
- **FR-008**: If the implementation uncovers an explicit concurrency cap (configured or hard-coded), it MUST be surfaced on the dashboard's idle-state tile alongside the existing `Filter:` hint, so operators can see "Capacity: 1/4 in use" rather than guessing.
- **FR-008a** *(root cause identified during 065 live testing)*: When `max_concurrent_cards > 1`, the IN_PROGRESS branch of `src/coordinare/graph/nodes/check_board.py` MUST re-adopt every uncovered IN_PROGRESS card into `active_sessions` (phase=`monitoring_agent`) and then fall through to the 035 multi-card TODO pickup so remaining concurrency slots fill on the same cycle. This mirrors the 061 IN_REVIEW fall-through pattern. A per-session invocation whose `current_card` is itself one of the IN_PROGRESS cards MUST preserve `phase=monitoring_agent` and short-circuit (no fall-through), and single-card mode (`max_concurrent_cards == 1`) MUST retain the existing early-return behaviour. Regression test: with one IN_PROGRESS card, two unblocked TODO cards, and `max_concurrent_cards=3`, one `check_board` call MUST produce three distinct `active_sessions` (one monitoring_agent + two dispatching).

### Success Criteria

- **SC-004**: With the fix in place, a symphony with 4 idle performers and ≥4 unblocked TODO cards reaches 4 concurrent active sessions within two cycles of startup.
- **SC-005**: Every cycle in which a performer is left idle produces a log record naming the reason; "silent serialisation" is unreachable.

---

### User Story 3 — Cards Kicked Back to TODO Don't Strand In-Flight Performers (Priority: P1)

During the same live testing session that produced US1, the operator observed that the card whose tile lingered on the Active Performers panel (the one that had been kicked back to `TODO`) also stopped making any observable progress — no new commits, no further performer logs, no stage advance, no completion. It is not yet known whether this is a behavioural side-effect of the US1 staleness (the daemon dropping the performer's results because the session was already removed from `active_sessions`) or an independent bug in the kick-back path that fails to cancel / re-target the running performer.

The desired behaviour: when a card is moved from `IN_PROGRESS` back to `TODO`, the daemon MUST take an explicit, observable action against the performer that owned it — either cancel the performer (preferred when the work is to be redispatched) or, if cancellation is not yet implemented, emit a structured log record naming the orphaned performer and the card so the operator knows the performer is stranded. Silent stranding — performer alive, results discarded, no log — is unacceptable.

**Why this priority**: A stranded performer is a real resource leak (container running, tokens being charged, no work landing) on top of the UI lie from US1. Even if the root cause turns out to be a single bug shared with US1, this story exists to ensure the fix covers behaviour as well as display.

**Independent Test**: With two cards in flight (A, B), move card A from `IN_PROGRESS` back to `TODO` on the project board. Within one daemon cycle, EITHER (a) the performer container that was working A is observably stopped (no further log lines, exit recorded), OR (b) a structured log record `dispatcher.performer_orphaned` (or equivalent) is emitted naming the performer id and the card id. The performer working B MUST continue to make progress (commits, log lines) unaffected.

**Acceptance Scenarios**:

1. **Given** card A is being worked by performer P1 and is moved to `TODO`, **When** the next daemon cycle completes, **Then** P1 is either terminated cleanly or marked orphaned in the structured log; in neither case may P1 silently continue running with its results being discarded.
2. **Given** performer P2 is concurrently working card B, **When** P1 is handled per AC-1, **Then** P2 continues to make progress and its session in `active_sessions` is untouched.
3. **Given** the root-cause investigation concludes US3 is a strict side-effect of US1, **When** the US1 fix is implemented, **Then** the AC-1 test still passes (the side-effect is gone) and US3 may be closed with a reference to the US1 commit rather than a separate fix.

---

## Requirements *(continued — User Story 3)*

### Functional Requirements

- **FR-009**: When a card transitions out of `IN_PROGRESS` while it is present in `active_sessions`, the daemon MUST detect the transition within the next cycle and either (a) cancel the performer that owns the session, or (b) emit a `dispatcher.performer_orphaned` structured log record with `{performer_id, card_id, reason}`.
- **FR-010**: An integration test MUST simulate the kick-back path (card removed from the IN_PROGRESS board column while its session is in `active_sessions`) and assert that one of the two outcomes in FR-009 is observed.
- **FR-011**: If the implementation determines US3 is fully resolved by the US1 fix, the integration test MAY remain as a regression guard rather than driving a separate code change; the PR description MUST state the conclusion explicitly so the spec is closed with intent rather than by accident.

### Success Criteria

- **SC-006**: Zero stranded performer containers observable in a one-hour live session that includes at least three kick-back events. Stranded is defined as: container alive, no results landing in `active_sessions`, no structured log record explaining why.

---

### User Story 4 — Un-Blocking a Card Restarts Its Feedback Budget (Priority: P1)

During the same live testing session, two cards (issue #101 with PR #133, and issue #111 with PR #134) repeatedly entered `feedback_cycle_exhausted` even after the operator manually addressed each round of feedback on the PR / issue and moved the card back to `TODO`. The daemon picked the card up, ran one feedback cycle, and immediately re-blocked it for triage — the operator's intervention bought one round, not a fresh budget.

Root cause: `feedback_cycle_count` is only reset in `src/coordinare/graph/nodes/check_board.py` when the *picked-up card id changes* between cycles. When the operator un-blocks the **same** card (BLOCKED → TODO), the counter carries over from the prior exhausted run and the next feedback signal trips `feedback_cycle_exhausted` again. The operator has no observable way to tell that the system considers this card "already over budget" — the dashboard tile shows no feedback-cycle counter, and the `triage` block reason in the structured log is the only signal, easy to miss in a busy log stream.

The desired behaviour: when an operator un-blocks a card (BLOCKED → any non-BLOCKED column on the project board), the daemon MUST treat the next pickup as a fresh attempt — `feedback_cycle_count` reset to 0. Independently, the system MUST retain monotonic stats per card so the operator can still see how much work has actually gone into the card over its lifetime: `total_feedback_cycles` (never reset, incremented every feedback cycle regardless of un-block) and `triage_blocks` (never reset, incremented on every `feedback_cycle_exhausted` block).

**Why this priority**: Un-blocking is the operator's primary recovery action when a card stalls. If un-blocking doesn't reset the budget, the operator's intervention has effectively no effect and the card grinds back into BLOCKED on the next feedback round. The current behaviour silently defeats operator agency. The stats are not strictly required to fix the bug, but they prevent the opposite failure mode (resetting hides genuine pathological cards from oversight).

**Independent Test**: Place a card in BLOCKED with `feedback_cycle_count >= max_feedback_cycles` in `daemon.state["active_sessions"]` (or the equivalent persisted state). Move the card to `TODO` on the project board. Within one daemon cycle, the session's `feedback_cycle_count` MUST be 0; the card MUST be eligible for dispatch; `total_feedback_cycles` MUST retain its prior value; `triage_blocks` MUST retain its prior value (≥1 from the previous exhaustion).

**Acceptance Scenarios**:

1. **Given** a card whose session has `feedback_cycle_count = 5` and `triage_blocks = 1`, **When** the operator moves it from BLOCKED to TODO, **Then** the next cycle resets `feedback_cycle_count` to 0 but leaves `triage_blocks = 1` and `total_feedback_cycles` unchanged.
2. **Given** a card has been un-blocked once and exhausts feedback again, **When** the card is re-blocked, **Then** `triage_blocks` increments to 2 and `total_feedback_cycles` reflects the cumulative number of feedback rounds across both attempts.
3. **Given** the operator views the dashboard tile or `/cards` view for an un-blocked card, **When** the card has any prior triage history, **Then** the tile/row surfaces both `total_feedback_cycles` and `triage_blocks` (or equivalent labelled fields), so the operator can see "this card has gone through triage twice and 7 feedback rounds total" without grepping logs.

---

## Requirements *(continued — User Story 4)*

### Functional Requirements

- **FR-012**: When a session's card transitions out of the BLOCKED column on the project board (whether to TODO, BACKLOG, or any non-BLOCKED column), the daemon MUST reset that session's `feedback_cycle_count` to 0 before the next dispatch eligibility check. The reset MUST fire on the SAME card id — the existing card-id-change reset path is insufficient.
- **FR-013**: Session state MUST gain two monotonically increasing counters: `total_feedback_cycles` (incremented each time a feedback cycle is consumed, never reset for the lifetime of the card) and `triage_blocks` (incremented each time `feedback_cycle_exhausted` causes the card to be moved to BLOCKED, never reset). Both fields persist across un-block / re-block cycles.
- **FR-014**: The dashboard's card tile and the `/cards` API response MUST include `total_feedback_cycles` and `triage_blocks` whenever they are > 0. Cards with no triage history MAY omit the fields or render them as zero.
- **FR-015**: A `dispatcher.feedback_cycle_reset` structured log record MUST be emitted whenever FR-012 fires, carrying `{card_id, prior_count, total_feedback_cycles, triage_blocks}`, so operator un-blocks are auditable.
- **FR-016**: A regression test MUST seed a session with `feedback_cycle_count >= max_feedback_cycles` and `triage_blocks >= 1`, simulate the BLOCKED → TODO transition, run one cycle, and assert (a) `feedback_cycle_count == 0`, (b) `triage_blocks` retained, (c) `total_feedback_cycles` retained, (d) the reset log record was emitted.

### Success Criteria

- **SC-007**: An operator un-blocking a card observes the card make at least one full feedback round before re-blocking, in 100% of un-block events (no silent immediate re-block).
- **SC-008**: For any card with `triage_blocks > 0`, the dashboard surfaces the count without the operator needing to consult logs.

---

### User Story 5 — Performer CI Ownership: Verify Build Before Handoff (Priority: P1)

The same live testing session that produced US4 surfaced a deeper structural issue: the implementer (and every other code-touching performer) commits and hands off without verifying that the repository's CI build passes locally. The closer waits on remote CI via the 064 `pr-checks` gate, but by that point the card is far downstream — failures cause feedback rounds, which combined with the US4 counter-not-reset bug push cards into repeated `triage` blocks.

This story incorporates the **full scope of spec 043 (Performer CI Ownership)** into the 065 cycle. The core principle: any performer that commits code MUST verify that CI passes (lint, type-check, tests, whatever the repo's CI definition runs) before handing off to the next stage. The coordinare enforces this with a server-side gate at the `monitoring_performer → monitoring_pr` transition; the performer personas are updated to make the expectation explicit; and a new `ci_detection` service auto-detects the repo's CI command from standard build files (Gemfile + Rakefile, package.json, pyproject.toml, Makefile, etc.) so performers don't have to be told what command to run.

The desired behaviour: a card that fails CI never advances to `monitoring_pr`; instead the daemon routes it back to the implementer with the CI output as feedback, exactly like a failed review. Performers that are aware of CI ownership will run CI themselves before committing; the coordinare-side gate is the backstop for performers that forget or for personas (reviewer, closer) that touch code without going through the full implementer flow.

**Why this priority**: This is upstream of every "card blocked for triage" failure mode we have seen. Catching CI failures at the performer stage prevents wasted reviewer / closer rounds, prevents triage churn, and means the un-block reset in US4 actually has a chance to land cards that were genuinely stuck on a single CI miss rather than circling indefinitely. Bringing 043 into 065 is a deliberate scope expansion (per direct operator instruction) because the two stories are mechanically coupled.

**Independent Test**: Seed a repository with a deliberately broken test (`expect(true).toBe(false)`) and dispatch a card to the implementer. The performer commits the change. Within one daemon cycle, the daemon MUST detect the failing CI command (auto-detected from the repo's build files) and either (a) route the card back to the implementer with the CI output captured as feedback, or (b) block the card with a clear `ci_failed` reason — in no case may the card advance to `monitoring_pr` with a red build.

**Acceptance Scenarios**:

1. **Given** an implementer commits code that fails the repo's CI command, **When** the daemon evaluates the stage transition to `monitoring_pr`, **Then** the transition is blocked and the card is routed back to the implementer with the CI output appended as feedback.
2. **Given** a repository whose CI command is defined in `package.json` as `npm test`, **When** the `ci_detection` service inspects the repo, **Then** it returns `npm test` (or the resolved variant) as the CI command without operator configuration.
3. **Given** a reviewer or closer makes a commit that touches code (e.g. lint fix), **When** they hand off, **Then** the same CI gate fires — reviewer/closer are not exempt from CI ownership.
4. **Given** a performer's persona is loaded, **When** the persona text is rendered, **Then** it contains the explicit directive that the performer MUST verify CI passes before committing/handing off, with the auto-detected CI command available in the persona context.
5. **Given** CI passes, **When** the implementer hands off, **Then** the card advances to `monitoring_pr` exactly as it does today — the gate is invisible on the happy path.

---

## Requirements *(continued — User Story 5)*

### Functional Requirements

- **FR-017**: A new `src/coordinare/services/ci_detection.py` service MUST auto-detect a repository's CI command by inspecting standard build files in priority order: `Gemfile` + `Rakefile` (Ruby), `package.json` `scripts.test` (Node), `pyproject.toml` `[tool.pytest]` or `[project.scripts]` (Python), `Makefile` `check`/`test` target, `.github/workflows/*.yml` aggregated commands as a last-resort fallback. The service MUST return a single shell command string and the rationale (which file matched).
- **FR-018**: The coordinare MUST add a CI gate at the `monitoring_performer → monitoring_pr` stage transition in `src/coordinare/graph/nodes/monitor_performer.py` `_advance_stage`. When the gate fires, it runs the auto-detected CI command in the performer's working directory; on non-zero exit the card MUST NOT advance — it MUST be routed back to the implementer with the CI output captured as `ci_failed` feedback (consuming a feedback cycle).
- **FR-019**: The personas for every code-committing performer (`implementer`, `qa`, `tech_writer`, `security`, `reviewer`, `closer`) MUST be updated by `src/coordinare/services/persona_service.py` to include (a) the explicit CI-ownership directive, and (b) the auto-detected CI command for the current repo as part of the rendered persona context. Reviewer and closer specifically MUST also gain an explicit lint instruction.
- **FR-020**: Performer-side execution in `agent/performer/src/performer/main.py` MUST attempt the auto-detected CI command before declaring its work complete; failures MUST be reported back as part of the performer's structured output so the coordinare's gate has the result without re-running CI redundantly when possible.
- **FR-021**: When the CI gate routes a card back to implementer, the failure MUST emit a `performer.ci_failed` structured log record carrying `{card_id, performer_stage, ci_command, exit_code, output_excerpt}` for operator observability.
- **FR-022**: Regression tests MUST cover: (a) each `ci_detection` priority rule with a fixture repo containing the matching build file; (b) the `_advance_stage` gate routes a failing-CI session back to implementer; (c) the gate is invisible (no extra latency, no extra log noise beyond an info-level "ci_passed" record) on the happy path; (d) reviewer and closer personas contain the CI-ownership directive after rendering.

### Success Criteria

- **SC-009**: No card reaches `monitoring_pr` with a red CI build under the gate. Verified by an integration test that seeds a broken test and asserts the gate blocks the transition.
- **SC-010**: Operators report ≥50% reduction in `feedback_cycle_exhausted` blocks on the next live testing session vs. the pre-fix baseline, because CI failures are caught at the performer stage instead of in review.
- **SC-011**: `ci_detection` returns a non-null command for ≥90% of repos in the active symphony catalogue (measured at fix-ship time across the workspaces under `~/Workspaces/`).

---

## Spec 043 Disposition

Spec 043 (Performer CI Ownership) is **subsumed by 065 US5**. Once US5 lands, 043 SHOULD be marked superseded with a pointer to 065's commit/PR. The mechanical scope of 043 is preserved verbatim in US5; this is a re-homing rather than a re-scoping.

---

## Additional QA Findings *(append as testing continues)*

> Append each subsequent QA finding here as a new **User Story N** section above and a matching FR / SC group, keeping the same independence-of-test discipline used in 058/062.
