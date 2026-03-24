# Feature Specification: Card Cancellation

**Feature Branch**: `026-card-cancellation`
**Created**: 2026-03-24
**Status**: Draft

## Overview

The coordinare currently has no way to cancel a card that is in progress. If a card is dispatched to a performer and the operator realises it should be stopped (wrong card, stale requirement, emergency), the only option is to kill the performer process manually. This feature adds two cancellation paths: an explicit API endpoint on the dashboard (`POST /api/cancel`) and automatic detection of a card moved to a "Cancelled" column on the GitHub board. Both paths stop the active performer session, clean up the workspace, and move the card to a terminal state.

## Clarifications

### Session 2026-03-24

- Q: Should cancellation be immediate or graceful? -> A: Graceful with a timeout. The coordinare sends a cancel signal to the performer transport and waits up to 10 seconds for clean shutdown before force-killing.
- Q: Where does the card go after cancellation? -> A: Back to TODO by default (so it can be re-prioritised), unless the operator moved it to a "Cancelled" column (in which case it stays there).
- Q: Should the coordinare support a "Cancelled" column natively? -> A: No. The coordinare detects cards in any column not in its known set (Backlog, TODO, In Progress, Blocked, In Review, Done) and treats them as cancelled if they were previously in progress.
- Q: Should cancellation be idempotent? -> A: Yes. Cancelling a card that is not in progress returns success with no side effects.

## User Scenarios & Testing *(mandatory)*

### User Story 1 -- Cancel via Dashboard API (Priority: P1)

An operator sends `POST /api/cancel` to the dashboard while a card is in progress. The coordinare stops the performer, cleans up the workspace, and moves the card back to TODO.

**Why this priority**: This is the primary operator-facing cancellation path; without it, there is no clean way to stop a runaway performer.

**Independent Test**: Can be tested by setting up state with `phase = "monitoring_performer"` and a mock performer service, calling the cancel endpoint, and verifying the performer session is terminated, workspace is cleaned up, and state resets to idle.

**Acceptance Scenarios**:

1. **Given** a card is in progress with an active performer session, **When** `POST /api/cancel` is called, **Then** the performer session is stopped, the workspace is cleaned up, and the card is moved to TODO on the board.
2. **Given** no card is in progress (`phase = "idle"`), **When** `POST /api/cancel` is called, **Then** the response is 200 with `{"status": "no_active_card"}` and no side effects occur.
3. **Given** a card is in progress, **When** `POST /api/cancel` is called and the performer fails to stop within 10 seconds, **Then** the coordinare force-kills the performer process, cleans up, and returns 200.
4. **Given** a cancel request is in flight, **When** a second `POST /api/cancel` arrives concurrently, **Then** the second request returns 200 without double-cancelling.

---

### User Story 2 -- Cancel via Board Column Detection (Priority: P2)

When the operator moves a card from "In Progress" to an unrecognised column (e.g. "Cancelled") on the GitHub board, `check_board` detects that the active card is no longer in any known column and triggers cancellation.

**Why this priority**: Provides a GitHub-native cancellation path for operators who prefer to manage cards directly on the board.

**Independent Test**: Can be tested by setting state with an active card and providing a board snapshot where that card's ID does not appear in any known column, then verifying cancellation is triggered.

**Acceptance Scenarios**:

1. **Given** a card is in progress and the next board poll shows the card is no longer in any known column, **When** `check_board` runs, **Then** the coordinare cancels the performer session and resets to idle.
2. **Given** a card is in progress and the board poll shows the card moved to "Done", **When** `check_board` runs, **Then** normal merge/done handling occurs (not cancellation).

---

### Edge Cases

- What if the performer transport does not support cancellation? (Best-effort: log a warning and proceed with cleanup.)
- What if workspace cleanup fails? (Log the error, continue with state reset, and emit a notification.)
- What if the GitHub API call to move the card back to TODO fails? (Retry once, then log and proceed.)

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The dashboard MUST expose a `POST /api/cancel` endpoint that cancels the currently active card.
- **FR-002**: Cancellation MUST stop the active performer session via the transport layer.
- **FR-003**: Cancellation MUST clean up the workspace associated with the cancelled card.
- **FR-004**: After API-initiated cancellation, the coordinare MUST move the card to TODO on the GitHub board and reset `phase` to `"idle"`.
- **FR-005**: The cancel endpoint MUST be idempotent: calling it when no card is active returns success.
- **FR-006**: `check_board` MUST detect when the active card has disappeared from all known columns and trigger cancellation.
- **FR-007**: Cancellation MUST complete within a configurable timeout (default 10 seconds); if the performer does not stop, the coordinare MUST force-kill it.
- **FR-008**: Cancellation MUST emit a `card_cancelled` notification event.

### Key Entities

- **CancelRequest**: The API request body (empty; cancels the current card).
- **CancelResult**: The API response indicating success, no active card, or error details.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: `POST /api/cancel` while a performer is running returns 200 and the card appears in TODO within one poll cycle.
- **SC-002**: Moving a card to a "Cancelled" column on the GitHub board triggers performer shutdown within one poll cycle.
- **SC-003**: Cancellation of an idle coordinare returns 200 with no side effects.

## Assumptions

- The performer transport layer supports a `cancel` or `terminate` method (or the coordinare can kill the subprocess).
- Only one card is active at a time (single-card-at-a-time model from the current architecture).
- The "Cancelled" column detection relies on the card not appearing in any of the six known columns; the coordinare does not need to know the column name.
