# Feature Specification: Dashboard "Check Board Now" Button

**Feature Branch**: `016-force-poll`
**Created**: 2026-03-15
**Status**: Draft
**Input**: User description: "Add a Check Board Now button to the coordinare dashboard that allows an operator to manually trigger an immediate board poll cycle when the daemon is idle."

## User Scenarios & Testing *(mandatory)*

### User Story 1 - Trigger Immediate Poll from Dashboard (Priority: P1)

An operator is watching the coordinare dashboard and notices the daemon has been idle for a while — a card may be stuck or the operator wants to check for new work immediately rather than waiting for the next scheduled poll. They click the "Check Board Now" button and the daemon wakes up and runs a poll cycle right away.

**Why this priority**: This is the entire purpose of the feature. Without it, nothing else has value.

**Independent Test**: Open the dashboard while the daemon is idle, click the button, and confirm the daemon executes a poll cycle within seconds.

**Acceptance Scenarios**:

1. **Given** the daemon is idle (waiting for next scheduled poll), **When** the operator clicks "Check Board Now", **Then** the daemon begins a poll cycle immediately without waiting for the scheduled interval.
2. **Given** the operator has just clicked "Check Board Now", **When** the button registers the click, **Then** the button becomes non-interactive (disabled or shows a loading state) to prevent duplicate triggers.
3. **Given** the button was clicked and the poll cycle completed, **When** the cycle finishes and the daemon returns to idle, **Then** the button becomes interactive again.

---

### User Story 2 - Button State Reflects Daemon Activity (Priority: P2)

The operator can tell at a glance whether triggering a poll makes sense. When the daemon is already actively running a cycle, the button is disabled so the operator doesn't trigger a redundant poll.

**Why this priority**: Prevents confusion and wasted cycles; the feature still delivers value without this if the server-side is idempotent, but correct visual state is important for operator confidence.

**Independent Test**: Observe the button while the daemon cycles through idle → active → idle states and confirm the button's interactive state matches.

**Acceptance Scenarios**:

1. **Given** the daemon is idle, **When** the operator views the dashboard, **Then** the "Check Board Now" button is enabled and visually actionable.
2. **Given** the daemon is actively running a poll cycle, **When** the operator views the dashboard, **Then** the "Check Board Now" button is visually disabled and non-interactive.
3. **Given** the daemon is in a stopped or error state, **When** the operator views the dashboard, **Then** the "Check Board Now" button is disabled.

---

### Edge Cases

- What happens if the operator clicks the button just as the daemon naturally wakes up for its scheduled poll? The duplicate trigger should be harmless — the daemon runs one cycle, not two.
- What happens when the operator clicks the button but the server is unreachable? The button should show a visible error state and re-enable so the operator can retry.
- What happens if the poll interval is set to 0 (webhook-only mode)? The button should still work — it fires the same trigger mechanism used by webhooks.
- What if multiple browser tabs are open and both click simultaneously? The daemon should run at most one additional cycle; a second near-simultaneous trigger is absorbed.

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The dashboard MUST display a "Check Board Now" button visible to the operator when the daemon is running.
- **FR-002**: Clicking the button MUST cause the daemon to begin a board poll cycle immediately, interrupting any current idle wait.
- **FR-003**: The button MUST be disabled (non-interactive) while a poll cycle is already in progress.
- **FR-004**: The button MUST be disabled (non-interactive) while the button's own trigger request is in flight (between click and server acknowledgement).
- **FR-005**: After the trigger is acknowledged by the server, the button MUST provide visible feedback (e.g. brief disabled/spinner state) before returning to its interactive state.
- **FR-006**: If the trigger request fails (server unreachable or returns an error), the dashboard MUST display a brief error indication and re-enable the button so the operator can retry.
- **FR-007**: The trigger mechanism MUST be idempotent — multiple rapid clicks MUST NOT cause multiple consecutive poll cycles to be queued.
- **FR-008**: The button MUST work regardless of the configured poll interval, including when the daemon is in webhook-only mode (poll interval = 0).

### Key Entities

- **Poll Trigger**: A one-shot signal sent from the dashboard to the daemon that causes an immediate poll cycle. Fire-and-forget with acknowledgement; no persistent state required.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: From button click to daemon beginning a poll cycle takes no more than 2 seconds under normal conditions.
- **SC-002**: The button visually reflects the correct daemon state (idle vs. active) within one dashboard refresh cycle after a state change.
- **SC-003**: Clicking the button while a cycle is already running produces no additional cycle — verified by cycle counter remaining unchanged.
- **SC-004**: The button is discoverable without documentation — an operator unfamiliar with the feature can locate and use it on first visit to the dashboard.

## Assumptions

- The dashboard already has a live connection to the daemon (SSE or polling) that provides the current daemon phase; button state derives from this existing feed.
- No authentication or authorisation is required to trigger a poll — the dashboard is assumed to be operator-only (local or trusted network).
- The server-side trigger only needs to signal the existing internal wake-up mechanism; no new daemon state or persistence is required.
- A single acknowledged response from the server is sufficient feedback; the operator can watch the dashboard's existing activity indicators to confirm the cycle ran.
