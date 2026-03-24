# Feature Specification: Active Board Reconciliation

**Feature Branch**: `032-active-board-reconciliation`
**Created**: 2026-03-24
**Status**: Draft

## Overview

The coordinare assumes it is the sole actor moving cards between GitHub board columns. When a human manually drags a card to a different column (e.g., from "In Progress" back to "TODO", or from "In Review" to "Done"), the coordinare's internal state becomes inconsistent with the board. This inconsistency persists until the next `check_board` poll happens to notice the mismatch, and even then the result can be unpredictable -- the coordinare may re-dispatch a card the human already resolved, or continue monitoring a card that was moved away. This feature adds explicit board-column reconciliation: on each poll cycle, the coordinare verifies that the card's actual GitHub column matches the expected column for the current phase, and takes corrective action on mismatch.

## Clarifications

### Session 2026-03-24

- Q: Where should the reconciliation check happen? -> A: Inside `monitor_performer`, which already runs on every poll cycle for active cards. A secondary check in `check_board` covers cards in other phases.
- Q: What corrective actions are taken? -> A: Depends on the direction of the mismatch. Card moved backward (e.g., In Progress -> TODO) resets state to idle. Card moved forward (e.g., In Progress -> Done) fast-forwards the lifecycle. Card moved to Blocked sets phase to blocked.
- Q: Should this support webhooks for instant detection? -> A: P2 scope. V1 relies on poll-based detection only.
- Q: What if the coordinare moved the card and the poll sees the new column before state updates? -> A: The reconciliation uses the *expected* column derived from `phase`, not the *previous* column. If phase says "monitoring_performer" and the board says "IN_PROGRESS", that is consistent -- no action needed.

## User Scenarios & Testing *(mandatory)*

### User Story 1 -- Poll-Based Column Mismatch Detection (Priority: P1)

On each `monitor_performer` cycle, the coordinare checks the card's actual board column by looking it up in the latest `board_snapshot`. If the column does not match the expected column for the current phase, the coordinare reconciles state: backward moves reset to idle, forward moves fast-forward the lifecycle, and moves to Blocked halt the performer.

**Why this priority**: Without this, manual board moves cause silent state corruption.

**Independent Test**: Can be tested by setting `phase = "monitoring_performer"` (expected column: IN_PROGRESS) and a `board_snapshot` showing the card in TODO, then verifying the state is reset to idle.

**Acceptance Scenarios**:

1. **Given** `phase = "monitoring_performer"` and the card is in "IN_PROGRESS" on the board, **When** `monitor_performer` runs, **Then** no reconciliation occurs and monitoring continues normally.
2. **Given** `phase = "monitoring_performer"` and the card is in "TODO" on the board, **When** `monitor_performer` runs, **Then** the performer session is abandoned, state is reset to idle, and a warning is logged.
3. **Given** `phase = "monitoring_performer"` and the card is in "DONE" on the board, **When** `monitor_performer` runs, **Then** the lifecycle is fast-forwarded: `phase` is set to `"merging"` or card is marked complete.
4. **Given** `phase = "monitoring_performer"` and the card is in "BLOCKED" on the board, **When** `monitor_performer` runs, **Then** `phase` is set to `"blocked"`.
5. **Given** `phase = "monitoring_pr"` and the card is in "IN_PROGRESS" on the board, **When** `check_board` runs, **Then** the card is recognized as moved backward and state reconciles accordingly.

---

### User Story 2 -- Webhook-Based Instant Detection (Priority: P2)

A GitHub webhook notifies the coordinare when a card is moved between columns. The coordinare processes the webhook event and triggers immediate reconciliation without waiting for the next poll.

**Why this priority**: Webhooks reduce reconciliation latency from poll-interval to near-instant, but require additional infrastructure.

**Independent Test**: Can be tested by sending a simulated webhook payload to the dashboard webhook endpoint and verifying state is updated.

**Acceptance Scenarios**:

1. **Given** a webhook event indicates the card moved from "In Progress" to "TODO", **When** the webhook handler processes it, **Then** state is reconciled within 1 second.
2. **Given** a webhook event arrives for a card the coordinare is not tracking, **When** the handler processes it, **Then** the event is ignored.

---

### Edge Cases

- What if the card is not found in any board column (deleted or archived)?
- What if the board snapshot is stale (fetched before the manual move)?
- What if two manual moves happen between polls?
- What if the coordinare and a human move the card simultaneously?

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: `monitor_performer` MUST look up the current card's actual board column from `board_snapshot` on each poll cycle.
- **FR-002**: The node MUST compare the actual column against the expected column derived from `phase` using a `PHASE_TO_EXPECTED_COLUMN` mapping.
- **FR-003**: If the card is found in a column that represents a backward move (e.g., TODO when expecting IN_PROGRESS), the node MUST abandon the performer session, clear `agent_dispatch`, and set `phase = "idle"`.
- **FR-004**: If the card is found in DONE, the node MUST set `phase = "merging"` or mark the card complete (skipping remaining lifecycle roles).
- **FR-005**: If the card is found in BLOCKED, the node MUST set `phase = "blocked"`.
- **FR-006**: If the card is not found in any column, the node MUST log a warning and set `phase = "idle"`.
- **FR-007**: The reconciliation check MUST run before the performer status poll to avoid wasted API calls on cards that have been moved.
- **FR-008**: A structured log event MUST be emitted on every reconciliation action, including the expected and actual columns.

### Key Entities

- **PHASE_TO_EXPECTED_COLUMN**: A dict mapping phase names to expected board columns (e.g., `"monitoring_performer" -> "IN_PROGRESS"`, `"monitoring_pr" -> "IN_REVIEW"`).
- **ReconciliationAction**: The corrective action taken -- one of `reset_idle`, `fast_forward`, `block`, `ignore`.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A card manually moved from IN_PROGRESS to TODO is detected and state is reconciled within one poll cycle.
- **SC-002**: A card manually moved to DONE completes the lifecycle without re-dispatching any performer.
- **SC-003**: No false positives -- cards in their expected column trigger zero reconciliation actions.
- **SC-004**: Reconciliation events are visible in structured logs with expected and actual column values.

## Assumptions

- `board_snapshot` is refreshed by `check_board` on every poll cycle before `monitor_performer` runs. The snapshot is recent enough for accurate comparison.
- The six board columns (Backlog, TODO, In Progress, Blocked, In Review, Done) are fixed and do not change.
- V1 (poll-based) reconciliation latency equals the poll interval (typically 30-60s). Webhook-based detection (P2) would reduce this to near-instant.
- The coordinare is the only automated actor on the board. If other automations move cards, conflicts are out of scope.
