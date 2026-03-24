# Feature Specification: Human Override Controls

**Feature Branch**: `031-human-override-controls`
**Created**: 2026-03-24
**Status**: Draft

## Overview

The coordinare's performer lifecycle is fully automated -- once a card enters the pipeline, humans cannot intervene except by posting PR comments that trigger feedback classification. This feature adds explicit override controls: dashboard API endpoints to skip a role or restart from a specific role, and PR comment commands (`/coordinare skip-<role>`, `/coordinare restart-from <role>`) that are parsed during the monitoring phase. These controls give operators and reviewers fine-grained power to steer the lifecycle without restarting the daemon.

## Clarifications

### Session 2026-03-24

- Q: Should overrides apply immediately or on next poll? -> A: On the next poll cycle. The dashboard API sets a pending override in state; the graph applies it on the next iteration.
- Q: Can a veto abort the entire card? -> A: Yes. `/coordinare veto` moves the card to BLOCKED and halts all performer activity.
- Q: Who is authorized to issue overrides? -> A: V1 does not enforce authorization -- any dashboard user or PR commenter can issue commands. Authorization is a future concern.
- Q: What happens if the target role for restart-from is not in the lifecycle? -> A: The override is rejected with a 400 error (API) or an error comment on the PR.

## User Scenarios & Testing *(mandatory)*

### User Story 1 -- Dashboard API Overrides (Priority: P1)

An operator uses the dashboard API to skip the current performer role or restart the lifecycle from a specific role. The API validates the request and stores a pending override that the graph processes on the next cycle.

**Why this priority**: The dashboard API is the most reliable control plane -- it does not depend on GitHub comment parsing.

**Independent Test**: Can be tested by calling the API endpoint and verifying the pending override is stored in state and processed by the graph on the next cycle.

**Acceptance Scenarios**:

1. **Given** a card is in `monitoring_performer` with `performer_stage = "reviewing"`, **When** `POST /api/skip-role` is called, **Then** the next graph cycle advances `performer_stage` to the role after `reviewing` in the lifecycle sequence.
2. **Given** a card is in any performer phase, **When** `POST /api/restart-from/architect` is called, **Then** the next graph cycle sets `performer_stage = "architecting"` and `phase = "dispatching"`.
3. **Given** `POST /api/restart-from/nonexistent` is called, **When** the role is not in `lifecycle_sequence`, **Then** the API returns HTTP 400 with an error message.
4. **Given** `POST /api/veto` is called, **When** the next graph cycle runs, **Then** the card is moved to BLOCKED and the performer is halted.

---

### User Story 2 -- PR Comment Commands (Priority: P2)

A human reviewer posts a comment on the PR containing a coordinare command (e.g., `/coordinare skip-reviewer`, `/coordinare restart-from architect`). The coordinare parses this during `classify_human_feedback` or `monitor_pr` and applies the override.

**Why this priority**: PR-based commands are convenient for reviewers but require parsing logic and are less reliable than the API.

**Independent Test**: Can be tested by adding a mock PR comment with a `/coordinare` command and verifying the state is updated.

**Acceptance Scenarios**:

1. **Given** a PR comment contains `/coordinare skip-reviewer`, **When** `classify_human_feedback` runs, **Then** `performer_stage` advances past the reviewer role.
2. **Given** a PR comment contains `/coordinare restart-from architect`, **When** `classify_human_feedback` runs, **Then** `performer_stage` is set to `"architecting"` and `phase = "dispatching"`.
3. **Given** a PR comment contains `/coordinare veto`, **When** `classify_human_feedback` runs, **Then** the card moves to BLOCKED.
4. **Given** a PR comment contains regular text with no `/coordinare` prefix, **When** `classify_human_feedback` runs, **Then** normal classification proceeds (no override).

---

### Edge Cases

- What if both a dashboard override and a PR comment override arrive in the same cycle?
- What if skip-role is called on the last role in the lifecycle?
- What if restart-from targets a role earlier than the first role?
- What if the card is in `idle` phase when an override arrives?

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The dashboard MUST expose `POST /api/skip-role` that stores a `pending_override = {"action": "skip"}` in coordinare state.
- **FR-002**: The dashboard MUST expose `POST /api/restart-from/{role}` that validates the role against `lifecycle_sequence` and stores `pending_override = {"action": "restart", "target_stage": <stage>}`.
- **FR-003**: The dashboard MUST expose `POST /api/veto` that stores `pending_override = {"action": "veto"}`.
- **FR-004**: All override API endpoints MUST return HTTP 400 if the card is not currently in an active phase (`monitoring_performer`, `monitoring_pr`, or `dispatching`).
- **FR-005**: `classify_human_feedback` MUST detect `/coordinare <command>` patterns in PR comments before running concern classification.
- **FR-006**: Recognized commands: `skip-<role>`, `restart-from <role>`, `veto`.
- **FR-007**: The graph MUST check for `pending_override` at the start of `dispatch_performer` and `monitor_performer` and apply it before proceeding.
- **FR-008**: After applying an override, the graph MUST clear `pending_override` from state.
- **FR-009**: If `skip-role` is called on the final lifecycle role, the card MUST transition to `monitoring_pr` (same as normal lifecycle completion).
- **FR-010**: Dashboard override takes precedence over PR comment override if both arrive in the same cycle.

### Key Entities

- **PendingOverride**: A TypedDict with `action: Literal["skip", "restart", "veto"]` and optional `target_stage: str`.
- **COMMAND_PATTERN**: Regex pattern for `/coordinare <command>` parsing.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A skip-role override via the dashboard advances the lifecycle within one poll cycle.
- **SC-002**: A restart-from override via the dashboard re-dispatches the specified role within two poll cycles.
- **SC-003**: A veto override moves the card to BLOCKED within one poll cycle.
- **SC-004**: PR comment commands produce identical outcomes to dashboard API overrides.
- **SC-005**: Invalid role names in restart-from return clear error messages.

## Assumptions

- The dashboard `FastAPI` app has access to the daemon's `CoordinareState` via the existing `_daemon` reference pattern (used by force-poll and persona endpoints).
- Override commands are case-insensitive but role names must match `lifecycle_sequence` entries exactly.
- V1 does not include authorization or audit logging for overrides; these are future concerns.
