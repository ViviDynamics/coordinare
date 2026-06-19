# Feature Specification: Restart-Time Board Reconciliation for Restored Sessions

**Feature Branch**: `094-restart-session-reconcile`  
**Created**: 2026-06-18  
**Status**: Draft  
**Input**: User description: "Restart-time board reconciliation for restored sessions — close the persisted-column-vs-board divergence that wedges cards."

## Overview

When the coordinare daemon restarts, it restores its in-memory workflow state from the on-disk snapshot (a top-level "active card" focus plus a set of per-card sessions). Each restored session remembers the board column and lifecycle phase the card was in when the snapshot was written. The board itself (the GitHub Projects "Status" of each card) can change while the daemon is down or between snapshots — a human or the daemon's own requeue logic may move a card from BLOCKED to TODO.

Today the restore path trusts the *persisted* column/phase rather than the *current* board status. When the two disagree, the card is restored into a stale state — most damagingly, restored as BLOCKED/idle while the board shows TODO. Such a card is skipped on every cycle and never dispatched, yet it still occupies one of the limited concurrent work slots. The card sits inert until a human notices the "stuck in blocked for N minutes" alarm and manually edits the state file to drop the stale session.

This feature makes the board the source of truth at restore time: every restored session's column and phase are reconciled against the live board, divergences are corrected (board wins), and the correction is logged so the situation is diagnosable from observability instead of a manual state-file autopsy.

## Clarifications

### Session 2026-06-18

- Q: When a restored session's persisted column disagrees with the live board, which value wins? → A: The live board status always wins; persisted column/phase are treated as a cache to be corrected.
- Q: Should reconciliation ever discard a restored session's in-flight work (e.g. an open PR being monitored, a running performer)? → A: No. Only the column/phase (and the wedged-idle case) are corrected; valid in-flight PR/performer context is preserved.
- Q: Should a card the board genuinely shows as BLOCKED be force-dispatched? → A: No. A truly-blocked card stays blocked; reconciliation only un-wedges cards whose board status has actually moved on.

## User Scenarios & Testing *(mandatory)*

### User Story 1 - A re-opened card resumes instead of wedging (Priority: P1)

A card was BLOCKED when the daemon last persisted state. While the daemon was down (or between snapshots) the card was moved to TODO on the board. The daemon restarts.

**Why this priority**: This is the exact failure observed live twice (most recently issue #158, idle for 120+ minutes while holding a work slot). Without this, the only recovery is a manual state-file edit. It is the core value of the feature.

**Independent Test**: Restore from a snapshot whose session has `column=BLOCKED` for a card the board now reports as TODO; confirm the card is reconciled to TODO and becomes eligible for dispatch on the next cycle (no longer skipped, no stuck-in-blocked alarm).

**Acceptance Scenarios**:

1. **Given** a persisted session marked BLOCKED for a card the board now shows as TODO, **When** the daemon restarts and re-adopts sessions, **Then** the session's column is corrected to TODO and the card is picked up for dispatch rather than skipped.
2. **Given** a persisted session stuck in an idle phase for a card the board shows as ready-to-work, **When** the daemon restarts, **Then** the session's phase is re-evaluated so the card resumes at the correct lifecycle position instead of remaining idle.
3. **Given** a card that the board genuinely still shows as BLOCKED, **When** the daemon restarts, **Then** the card remains blocked and is not force-dispatched.

---

### User Story 2 - In-flight work survives reconciliation (Priority: P1)

At restart, some restored sessions are legitimately mid-flight: one is monitoring an open pull request, another has a live performer still running. The board status for these cards has not meaningfully diverged.

**Why this priority**: Reconciliation must not become a blunt instrument. Discarding a valid in-flight session would lose PR-monitoring state or orphan a running performer — a regression worse than the bug being fixed. Equal priority to US1 because the fix is only safe if it is surgical.

**Independent Test**: Restore from a snapshot containing a `monitoring_pr` session (with PR reference) and a `monitoring_performer` session (with a live session id); confirm both retain their PR/performer context after reconciliation and are not reset to an earlier stage.

**Acceptance Scenarios**:

1. **Given** a restored session monitoring an open PR whose board column is consistent with that phase, **When** reconciliation runs, **Then** the PR reference and monitoring phase are preserved unchanged.
2. **Given** a restored session with a live performer mid-stage, **When** reconciliation runs, **Then** the performer/session linkage and stage are preserved and the card is not demoted to an earlier stage.

---

### User Story 3 - The correction is diagnosable from logs (Priority: P2)

An operator wants to know, after the fact, that a card's persisted state was corrected at restart — without inspecting the raw state file.

**Why this priority**: Observability turns a future recurrence from a silent multi-hour stall into a one-line log entry. Valuable but secondary to actually fixing the wedge.

**Independent Test**: Trigger a reconciliation correction and confirm a single structured event is emitted naming the card identifier, the old (persisted) column/phase, and the new (board-derived) column/phase — with no secret values present.

**Acceptance Scenarios**:

1. **Given** a session whose persisted column/phase is corrected at restore, **When** reconciliation applies the change, **Then** a structured event records the card identifier, prior column/phase, and corrected column/phase.
2. **Given** any reconciliation event, **When** its contents are inspected, **Then** it contains only identifiers, columns/phases, and paths — never literal secret values.

---

### Edge Cases

- **Card no longer on the board**: a restored session references a card that has been deleted/archived from the project. Reconciliation must drop or retire that session gracefully rather than crash or leave it inert.
- **Board lookup unavailable at restart**: the board status cannot be fetched for some/all cards (transient GitHub outage). Reconciliation must degrade safely — it must not delete sessions on a failed lookup, and must not block daemon startup.
- **Top-level focus vs sessions disagree**: the top-level "active card" focus points at a card whose session was corrected (or removed). The top-level focus must end up consistent with the reconciled session set (no dangling focus on a stale/blocked card).
- **Card advanced past the persisted stage**: the board shows the card in a later column (e.g. IN_REVIEW) than the persisted session (implementing). Reconciliation must move it forward to match the board, not replay the earlier stage.
- **Multiple cards corrected at once**: more than one restored session diverges. Each is reconciled independently; one card's correction does not affect another's.
- **Repeated restarts**: reconciling, then immediately restarting again, must converge (a corrected session reconciles to a no-op) rather than oscillate.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: On daemon restart / session re-adoption, the system MUST reconcile each restored session's persisted board column against the card's current board status, for both the top-level active-card focus and every entry in the restored session set.
- **FR-002**: When a restored session's persisted column diverges from the current board status, the system MUST treat the board status as authoritative and correct the session's column to match.
- **FR-003**: When a session's column is corrected, the system MUST re-evaluate the session's lifecycle phase so the card resumes from the position implied by its current board status (e.g. a card now in a ready-to-work column becomes eligible for dispatch rather than remaining in a stale blocked or idle phase).
- **FR-004**: The system MUST NOT discard or reset a restored session's still-valid in-flight context (an open pull request being monitored, or a live performer/session linkage) during reconciliation; only the column and phase (and the wedged-idle case) are subject to correction.
- **FR-005**: The system MUST NOT force-dispatch a card whose current board status is genuinely blocked; a truly-blocked card remains blocked after reconciliation.
- **FR-006**: The system MUST emit a structured observability event whenever a restored session's phase is corrected, recording the card identifier, the prior persisted phase, the live board column, and the corrected board-derived phase. The prior board column is included **when it was persisted** — only the top-level focus card's column is carried in the snapshot, so non-focus sessions (whose column is not persisted) record it as absent; the persisted divergence is fully captured by the prior phase regardless.
- **FR-007**: Reconciliation events and all persisted state MUST contain only identifiers, columns/phases, and file paths — never literal secret values.
- **FR-008**: The system MUST perform reconciliation at restore/re-adoption time, not as a repeating hot-loop operation, and MUST converge (a re-adoption of already-consistent sessions produces no corrections).
- **FR-009**: When the board status for a card cannot be determined at restart (lookup failure or card absent from the board), the system MUST degrade safely: it MUST NOT delete a session solely because of a failed lookup, MUST NOT block daemon startup, and MUST leave the existing persisted state in place for that card until a successful board read.
- **FR-010**: After reconciliation, the top-level active-card focus MUST be consistent with the reconciled session set — it MUST NOT continue to point at a card whose session was corrected away from or removed.

### Key Entities *(include if feature involves data)*

- **Restored Session**: the per-card workflow state rebuilt from the snapshot at restart. Carries the card identifier, the persisted board column, the lifecycle phase/stage, and any in-flight references (PR, live performer/session). Subject to column/phase reconciliation; in-flight references are preserved.
- **Top-Level Active-Card Focus**: the single "current card" pointer restored alongside the session set. Must be reconciled to remain consistent with the corrected sessions.
- **Board Status**: the authoritative current column of a card on the GitHub Projects board (the single-select Status field). The source of truth that reconciliation defers to.
- **Reconciliation Event**: a structured observability record emitted when a correction is applied, carrying card identifier, prior column/phase, and corrected column/phase (identifiers/columns/paths only).

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A card whose board status moved off BLOCKED while the daemon was down becomes eligible for work within one board cycle of restart — no manual state-file intervention is ever required to un-wedge it.
- **SC-002**: Zero "stuck in blocked" stalls are caused by board-vs-persisted column divergence after restart (the divergence that produced the issue #158 incident no longer strands a card).
- **SC-003**: 100% of restored in-flight sessions (open-PR monitoring and live-performer sessions) retain their in-flight context across restart — no PR-monitoring loss and no orphaned performers attributable to reconciliation.
- **SC-004**: Every reconciliation correction is attributable from logs alone — card identifier, prior phase, live board column, and corrected phase (plus the prior column for the focus card, whose column the snapshot persists) — eliminating the need to inspect the raw state file to diagnose a divergence.
- **SC-005**: A card genuinely blocked on the board is never auto-dispatched by reconciliation (no false un-blocking).
- **SC-006**: Restarting twice in succession produces no reconciliation corrections on the second restart (the operation converges; no oscillation).

## Assumptions

- The GitHub Projects "Status" single-select field is the authoritative board column for a card; coordinare already reads it during normal board polling, so reconciliation reuses that existing capability (no new external dependency).
- The state model remains the existing single-host, single-process JSON snapshot; this feature does not introduce multi-host or parallel-daemon state.
- The existing stuck-card watchdog is correct and remains unchanged — it surfaced the symptom; this feature removes the cause.
- Concurrent-work-slot capacity behavior is correct as-is; this feature does not change how many cards run at once (a reconciled card still waits its turn for a free slot — that backpressure is expected, not a wedge).

## Out of Scope

- The manual `coordinare.state.json` session-clear workaround (this feature removes the need for it).
- Any change to `max_concurrent_cards` / slot-capacity behavior.
- Multi-host or parallel-daemon state reconciliation.
- Reworking the stuck-card watchdog itself.
