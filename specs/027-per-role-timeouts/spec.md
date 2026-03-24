# Feature Specification: Per-Role Timeouts

**Feature Branch**: `027-per-role-timeouts`
**Created**: 2026-03-24
**Status**: Draft

## Overview

The coordinare currently uses a single global `transport_timeout_seconds` for all performer roles. Different roles have very different expected durations: an implementer may need 30 minutes, while a reviewer may need only 2 minutes. This feature wires the existing `PerformerRoleConfig.timeout_seconds` field through to transport construction and performer dispatch so that each role can have its own timeout. The dashboard is also updated to display the active role's timeout alongside the elapsed time.

## Clarifications

### Session 2026-03-24

- Q: Does `PerformerRoleConfig` already have `timeout_seconds`? -> A: Yes, it exists in `config.py` as `timeout_seconds: int | None = None` with None meaning "use global default".
- Q: Should the timeout apply to the transport layer, the monitor loop, or both? -> A: Both. The transport timeout governs the subprocess/SSH/K8s session. The monitor loop additionally enforces a hard ceiling so the daemon can recover even if the transport hangs.
- Q: What happens when a role times out? -> A: The card moves to Blocked with a timeout reason, same as the existing global timeout behavior.
- Q: Should the dashboard show a countdown? -> A: It should show elapsed time and the configured timeout for the active role, but not a live countdown (to avoid misleading precision).

## User Scenarios & Testing *(mandatory)*

### User Story 1 -- Per-Role Timeout in Config (Priority: P1)

An operator configures `performers.reviewer.timeout_seconds: 120` and `performers.implementer.timeout_seconds: 1800` in `config.yaml`. When the reviewer role is active, the coordinare enforces a 120-second timeout; when the implementer is active, it enforces 1800 seconds.

**Why this priority**: Without per-role timeouts, short-running roles like reviewer share the same generous timeout as the implementer, delaying detection of stuck performers.

**Independent Test**: Can be tested by configuring two roles with different timeouts, dispatching to each via a mock transport, and verifying the correct timeout is passed to each transport and enforced by the monitor loop.

**Acceptance Scenarios**:

1. **Given** `performers.reviewer.timeout_seconds: 120`, **When** the reviewer role is dispatched, **Then** the transport is constructed with a 120-second timeout.
2. **Given** `performers.implementer.timeout_seconds` is not set and global `transport_timeout_seconds: 1800`, **When** the implementer role is dispatched, **Then** the transport uses the global 1800-second timeout.
3. **Given** the reviewer role has been running for 121 seconds with `timeout_seconds: 120`, **When** `monitor_performer` checks, **Then** the card moves to Blocked with a timeout reason.
4. **Given** `performers.qa.timeout_seconds: 300`, **When** the QA role completes in 60 seconds, **Then** no timeout is triggered and the lifecycle advances normally.

---

### User Story 2 -- Timeout Visible in Dashboard (Priority: P2)

When a performer role is active, the dashboard displays the role name, elapsed time, and configured timeout so the operator can gauge progress.

**Why this priority**: Without visibility, the operator cannot tell whether a performer is close to timing out or has plenty of time remaining.

**Independent Test**: Can be tested by rendering the dashboard with a mock state where `performer_stage = "reviewing"`, `agent_dispatch_at` is 45 seconds ago, and the role timeout is 120 seconds, then verifying the HTML/SSE payload includes all three values.

**Acceptance Scenarios**:

1. **Given** the reviewer is active with 45s elapsed and 120s timeout, **When** the dashboard SSE sends a status update, **Then** the payload includes `performer_stage: "reviewing"`, `elapsed_seconds: 45`, and `timeout_seconds: 120`.
2. **Given** no card is active, **When** the dashboard renders, **Then** no timeout information is shown.

---

### Edge Cases

- What if `timeout_seconds` is set to 0? (Treat as "no timeout" -- the global default applies.)
- What if the transport does not support timeout enforcement? (The monitor loop is the fallback enforcer.)
- What if the role timeout is shorter than the poll interval? (The monitor will catch it on the first check after expiry; the transport timeout handles the actual kill.)

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: `_build_transport_for_role` MUST pass the role-specific `timeout_seconds` to the transport constructor when set.
- **FR-002**: When `PerformerRoleConfig.timeout_seconds` is None, the transport MUST use the global `transport_timeout_seconds` from `ProjectConfiguration`.
- **FR-003**: `monitor_performer` MUST compare elapsed time (`now - agent_dispatch_at`) against the active role's timeout and move the card to Blocked if exceeded.
- **FR-004**: The role timeout MUST be resolvable at runtime from `performer_services` or `config.performers` without requiring a config reload.
- **FR-005**: The dashboard SSE status payload MUST include the active role's `timeout_seconds` and `elapsed_seconds` when a performer is running.
- **FR-006**: A `timeout_seconds` value of 0 or None MUST be treated as "use global default", not "no timeout".

### Key Entities

- **PerformerRoleConfig.timeout_seconds**: Existing field; this feature wires it through to transport and monitor.
- **RoleTimeoutInfo**: Dashboard payload fragment containing `performer_stage`, `elapsed_seconds`, `timeout_seconds`.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A reviewer with `timeout_seconds: 120` that stalls is moved to Blocked within one poll cycle after 120 seconds.
- **SC-002**: An implementer with no role-specific timeout uses the global default and behaves identically to the pre-feature codebase.
- **SC-003**: The dashboard displays the correct timeout for each active role.

## Assumptions

- `_build_transport_for_role` already reads `timeout_seconds` from `PerformerRoleConfig` and falls back to the global value. This feature ensures that fallback is correct and that `monitor_performer` also enforces it.
- The existing `SubprocessTransport` already accepts a timeout parameter in its constructor.
- Only one performer role is active at a time (sequential lifecycle model from 019).
