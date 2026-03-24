# Feature Specification: Live Requirement Sync

**Feature Branch**: `030-live-requirement-sync`
**Created**: 2026-03-24
**Status**: Draft

## Overview

When a GitHub issue card's description or acceptance criteria are updated while a performer is actively working on it, the coordinare does not detect the change. The dispatched performer continues working against stale requirements, leading to wasted effort or incorrect implementations. This feature adds requirement-change detection during the monitoring phase: the coordinare re-fetches the card's issue details from GitHub, compares them against the originally dispatched context, and takes a configurable action (ignore, warn, or re-dispatch).

## Clarifications

### Session 2026-03-24

- Q: When should the check happen? -> A: During `monitor_performer`, on each status poll cycle. This avoids adding a new node.
- Q: What fields are compared? -> A: The issue `description` (body) and parsed `acceptance_criteria`. Title changes are logged but do not trigger re-dispatch.
- Q: What is the default behavior? -> A: `warn` -- log a warning and add a state field indicating requirements changed. Operators can configure `re-dispatch` to automatically restart the current role.
- Q: Should the performer be notified mid-execution? -> A: No. If `re-dispatch` is chosen, the current performer session is abandoned and a fresh dispatch occurs with updated context.

## User Scenarios & Testing *(mandatory)*

### User Story 1 -- Detect Requirement Changes (Priority: P1)

While a performer is working (phase = `monitoring_performer`), the coordinare re-fetches the issue description from GitHub on each poll cycle and compares it against the description stored in `current_card`. If the description or acceptance criteria differ, the coordinare records the change in state and logs a warning.

**Why this priority**: Detection is the foundation -- without it, no corrective action is possible.

**Independent Test**: Can be tested by setting `current_card.description` to one value and mocking `github.get_issue_details` to return a different description, then verifying the warning is logged and the state flag is set.

**Acceptance Scenarios**:

1. **Given** a card was dispatched with description "Build login page", **When** the issue is updated to "Build login page with OAuth", **Then** `monitor_performer` logs a warning and sets `requirements_changed = True` in state.
2. **Given** a card's acceptance criteria change from 3 items to 5, **When** `monitor_performer` polls, **Then** the change is detected and logged.
3. **Given** the issue description has not changed, **When** `monitor_performer` polls, **Then** `requirements_changed` remains `False` and no warning is logged.

---

### User Story 2 -- Configurable Response Behavior (Priority: P2)

Operators can set `requirement_change_policy` in `config.yaml` to one of: `ignore`, `warn`, or `re-dispatch`. On `ignore`, changes are silently recorded. On `warn` (default), a structured log warning is emitted. On `re-dispatch`, the coordinare resets `phase` to `dispatching` with the updated card context, abandoning the current performer session.

**Why this priority**: Different teams have different tolerances for mid-flight requirement changes.

**Independent Test**: Can be tested by setting each policy value in config and verifying the corresponding behavior when a requirement change is detected.

**Acceptance Scenarios**:

1. **Given** `requirement_change_policy = "ignore"`, **When** requirements change, **Then** state is updated silently with no log warning.
2. **Given** `requirement_change_policy = "warn"`, **When** requirements change, **Then** a structured warning is logged and the performer continues.
3. **Given** `requirement_change_policy = "re-dispatch"`, **When** requirements change, **Then** the performer session is abandoned and `phase` is set to `"dispatching"` with the updated card.

---

### Edge Cases

- What if the GitHub API call to re-fetch the issue fails?
- What if the description changes multiple times between polls?
- What if the performer has already opened a PR when requirements change?
- What if the card is in a non-monitoring phase when the change occurs?

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: `monitor_performer` MUST re-fetch the issue description from GitHub on each poll cycle using `github.get_issue_details`.
- **FR-002**: The node MUST compare the fetched `description` and `acceptance_criteria` against the values in `current_card`.
- **FR-003**: If a difference is detected, the node MUST set `requirements_changed = True` in state and store the updated description in `requirements_changed_details`.
- **FR-004**: The `requirement_change_policy` config field MUST accept values: `ignore`, `warn` (default), `re-dispatch`.
- **FR-005**: On `re-dispatch` policy, the node MUST update `current_card` with the new description, reset `phase` to `"dispatching"`, and clear `agent_dispatch`.
- **FR-006**: On `warn` policy, the node MUST log a structured warning but allow the performer to continue.
- **FR-007**: If the GitHub API call fails, the node MUST log a warning and skip the comparison (no crash).

### Key Entities

- **RequirementChangePolicy**: Literal type `"ignore" | "warn" | "re-dispatch"`, added to `ProjectConfiguration`.
- **requirements_changed**: Boolean field on `CoordinareState`.
- **requirements_changed_details**: Dict field on `CoordinareState` holding the diff summary.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A requirement change is detected within one poll cycle of the GitHub issue being edited.
- **SC-002**: With `re-dispatch` policy, the performer is re-dispatched with updated context within two poll cycles.
- **SC-003**: With `warn` policy, the performer completes normally and the warning is visible in structured logs.
- **SC-004**: GitHub API failures during re-fetch do not crash the monitoring loop.

## Assumptions

- `github.get_issue_details` is already available on `GitHubServiceProtocol` and returns the issue body.
- The poll interval for `monitor_performer` is unchanged (typically 30-60s); the additional API call per cycle is acceptable.
- Acceptance criteria are parsed from the description using the existing `parse_acceptance_criteria` utility.
