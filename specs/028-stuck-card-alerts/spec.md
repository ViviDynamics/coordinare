# Feature Specification: Stuck Card Alerts

**Feature Branch**: `028-stuck-card-alerts`
**Created**: 2026-03-24
**Status**: Draft

## Overview

The coordinare can silently stall when a card remains in the same non-idle phase for an extended period -- for example, a performer that hangs without producing output, or a PR stuck in review with no human activity. This feature adds a stuck-card detection mechanism: the coordinare tracks when the current phase was entered (`phase_entered_at` timestamp) and checks the elapsed duration on every daemon cycle. When a configurable threshold is exceeded, a notification is sent to alert the operator. Thresholds can be set globally or per-phase.

## Clarifications

### Session 2026-03-24

- Q: Which phases should be monitored? -> A: All non-idle phases: `dispatching`, `monitoring_agent`, `monitoring_performer`, `monitoring_pr`, `blocked`, `relay_feedback`, `merging`. The `idle` and `system_error` phases are excluded.
- Q: Should the alert repeat? -> A: Yes, repeat at the configured interval (e.g. every 30 minutes) until the phase changes. A dedup key prevents notification spam within the repeat window.
- Q: Should per-phase thresholds override the global threshold or supplement it? -> A: Override. If a per-phase threshold is set, it replaces the global default for that phase.
- Q: Should the stuck alert trigger any automatic recovery? -> A: No. V1 is notification-only. Automatic recovery (e.g. restart performer, move card) is out of scope.

## User Scenarios & Testing *(mandatory)*

### User Story 1 -- Alert After N Minutes in Same Phase (Priority: P1)

When a card has been in the same non-idle phase for longer than the global threshold (default: 30 minutes), the coordinare sends a notification to all configured channels.

**Why this priority**: Without stuck-card alerts, the operator has no way to know that the coordinare is stalled unless they actively monitor the dashboard.

**Independent Test**: Can be tested by setting `phase_entered_at` to 31 minutes ago, `phase = "monitoring_performer"`, and a 30-minute threshold, then running the stuck-card check and verifying a notification is emitted.

**Acceptance Scenarios**:

1. **Given** `phase = "monitoring_performer"` and `phase_entered_at` is 31 minutes ago with a 30-minute threshold, **When** the daemon cycle runs, **Then** a `card_stuck` notification is emitted.
2. **Given** `phase = "monitoring_performer"` and `phase_entered_at` is 15 minutes ago with a 30-minute threshold, **When** the daemon cycle runs, **Then** no notification is emitted.
3. **Given** `phase = "idle"`, **When** the daemon cycle runs, **Then** no stuck-card check is performed regardless of `phase_entered_at`.
4. **Given** a stuck alert was sent 10 minutes ago and the alert repeat interval is 30 minutes, **When** the daemon cycle runs, **Then** no duplicate alert is sent.
5. **Given** a stuck alert was sent 31 minutes ago and the phase has not changed, **When** the daemon cycle runs, **Then** a repeat alert is sent.

---

### User Story 2 -- Configurable Per-Phase Thresholds (Priority: P2)

The operator configures different stuck thresholds per phase in `config.yaml`. For example, `monitoring_pr` might tolerate 4 hours (waiting for human review) while `monitoring_performer` should alert after 30 minutes.

**Why this priority**: Without per-phase thresholds, phases with naturally long durations (PR review) generate noisy false-positive alerts.

**Independent Test**: Can be tested by configuring `stuck_thresholds.monitoring_pr: 14400` (4 hours) and `stuck_thresholds.monitoring_performer: 1800` (30 minutes), then verifying each phase triggers at the correct threshold.

**Acceptance Scenarios**:

1. **Given** `stuck_thresholds.monitoring_pr: 14400` and `phase = "monitoring_pr"` for 2 hours, **When** the daemon cycle runs, **Then** no alert is sent.
2. **Given** `stuck_thresholds.monitoring_pr: 14400` and `phase = "monitoring_pr"` for 5 hours, **When** the daemon cycle runs, **Then** a `card_stuck` alert is sent.
3. **Given** `stuck_thresholds.monitoring_performer: 1800` and `phase = "monitoring_performer"` for 35 minutes, **When** the daemon cycle runs, **Then** a `card_stuck` alert is sent.
4. **Given** no per-phase threshold for `dispatching` and a global default of 1800, **When** `dispatching` phase exceeds 1800 seconds, **Then** the global default triggers the alert.

---

### Edge Cases

- What if `phase_entered_at` is None (state migration from older version)? (Set it to `now` on first encounter and skip the check for that cycle.)
- What if the phase changes and then returns to the same phase within one cycle? (Reset `phase_entered_at` on every phase transition.)
- What if the notification service itself is stuck? (The stuck-card check does not block the daemon loop; notification dispatch is fire-and-forget.)
- What if the operator sets a threshold of 0? (Treat as disabled for that phase.)

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: The coordinare MUST maintain a `phase_entered_at` timestamp in `CoordinareState` that records when the current phase was entered.
- **FR-002**: `phase_entered_at` MUST be updated every time the `phase` field changes.
- **FR-003**: On each daemon cycle, the coordinare MUST check whether `now - phase_entered_at` exceeds the threshold for the current phase.
- **FR-004**: When the stuck threshold is exceeded, the coordinare MUST emit a `card_stuck` notification event with the phase name, elapsed time, and card title.
- **FR-005**: The notification MUST use a dedup key of `stuck:{card_id}:{phase}` to prevent spam within the notification dedup window.
- **FR-006**: The coordinare MUST support a global `stuck_threshold_seconds` config option (default: 1800 seconds / 30 minutes).
- **FR-007**: The coordinare MUST support per-phase threshold overrides via `stuck_thresholds` config map.
- **FR-008**: A threshold value of 0 MUST disable stuck detection for that phase.
- **FR-009**: The `idle` and `system_error` phases MUST be excluded from stuck detection.
- **FR-010**: `card_stuck` MUST be added to the `EventType` enum in the notification model.

### Key Entities

- **StuckThresholdsConfig**: Mapping of phase name to threshold in seconds.
- **phase_entered_at**: Timestamp field on `CoordinareState`.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: A card stuck in `monitoring_performer` for 31 minutes with a 30-minute threshold triggers exactly one notification.
- **SC-002**: A card in `monitoring_pr` for 2 hours with a 4-hour per-phase threshold does not trigger a notification.
- **SC-003**: Removing all `stuck_thresholds` config falls back to the 1800-second global default for all phases.
- **SC-004**: Phase transitions reset `phase_entered_at`, preventing false alerts after a legitimate phase change.

## Assumptions

- The notification service (006) is already functional and supports dispatching arbitrary `NotificationEvent` objects.
- The daemon loop runs at `poll_interval_seconds` frequency, so stuck detection granularity is bounded by the poll interval.
- `phase_entered_at` is persisted in the state store so it survives daemon restarts.
- V1 is notification-only; automatic recovery actions are out of scope.
