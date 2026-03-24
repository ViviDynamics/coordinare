# Feature Specification: Cost & Token Tracking

**Feature Branch**: `034-cost-token-tracking`
**Created**: 2026-03-24
**Status**: Draft

## Overview

The coordinare currently receives `performer_metrics` (including `tokens_processed`) from each performer's status response but does not accumulate or expose this data. This feature adds per-card and per-role token usage tracking, estimated cost calculation, Prometheus metrics for token consumption, and a dashboard panel showing cost per card. Operators can optionally configure a per-card cost budget with alert notifications when the budget is exceeded.

## Clarifications

### Session 2026-03-24

- Q: Where do token counts come from? -> A: The performer's `check_status` response includes a `metrics` dict with a `tokens_processed` field (integer). This is already stored in `state["performer_metrics"]` by `monitor_performer`.
- Q: How is cost estimated? -> A: A configurable `cost_per_million_tokens` rate (default $3.00) applied to total tokens. This is a rough estimate; per-model pricing is out of scope for V1.
- Q: Should budget enforcement stop the performer? -> A: No. Budget alerts are advisory only. The card is not blocked or cancelled when the budget is exceeded -- only a notification is sent.
- Q: Is per-role breakdown needed? -> A: Yes, tracked in state but V1 dashboard shows only per-card totals. Per-role breakdown is available via Prometheus labels.

## User Scenarios & Testing *(mandatory)*

### User Story 1 -- Accumulate Token Counts per Card (Priority: P1)

As a coordinare operator, I want to see the total token usage for each card so I can understand AI resource consumption. When `monitor_performer` receives a `performer_metrics.tokens_processed` value, it accumulates into `card_tokens_total` on `CoordinareState`. The counter resets when a new card is dispatched.

**Why this priority**: Token accumulation is the foundation for all downstream features (cost display, budget alerts, Prometheus metrics).

**Independent Test**: Mock `check_status` to return `metrics.tokens_processed` values across multiple polling cycles. Verify `card_tokens_total` accumulates correctly and resets on new card dispatch.

**Acceptance Scenarios**:

1. **Given** a performer returns `tokens_processed: 1500` on poll 1 and `tokens_processed: 2000` on poll 2, **When** `monitor_performer` processes both, **Then** `card_tokens_total` is 3500.
2. **Given** a card finishes and a new card is dispatched, **When** `dispatch_performer` runs for the new card, **Then** `card_tokens_total` is reset to 0.
3. **Given** a performer returns no `tokens_processed` field, **When** `monitor_performer` processes the status, **Then** `card_tokens_total` is unchanged.

---

### User Story 2 -- Dashboard Cost Display (Priority: P2)

The web dashboard shows a "Token Usage" panel for the active card, displaying total tokens consumed and estimated cost. Cost is calculated as `card_tokens_total / 1_000_000 * cost_per_million_tokens`.

**Why this priority**: Operators need a human-readable cost display, but it depends on the accumulation logic from P1.

**Independent Test**: Render the dashboard with a known `card_tokens_total` and `cost_per_million_tokens` config value. Verify the HTML includes the expected dollar amount.

**Acceptance Scenarios**:

1. **Given** `card_tokens_total = 500_000` and `cost_per_million_tokens = 3.0`, **When** the dashboard renders, **Then** it displays "$1.50" as the estimated cost.
2. **Given** `card_tokens_total = 0`, **When** the dashboard renders, **Then** it displays "$0.00".

---

### User Story 3 -- Cost Budget Alerts (Priority: P3)

Operators can configure `cost_budget_per_card` (float, dollars). When the accumulated cost exceeds this budget, the coordinare sends a notification via the existing notification service. The alert fires once per card (not on every poll cycle).

**Why this priority**: Budget enforcement is optional and builds on P1 and P2.

**Independent Test**: Set `cost_budget_per_card = 1.0` and accumulate tokens until cost exceeds $1.00. Verify a notification event is dispatched exactly once.

**Acceptance Scenarios**:

1. **Given** `cost_budget_per_card = 2.0` and tokens accumulate to a cost of $2.50, **When** the budget is first exceeded, **Then** a notification with severity "warning" is dispatched.
2. **Given** the budget alert has already fired for a card, **When** tokens continue accumulating, **Then** no duplicate alert is sent.
3. **Given** `cost_budget_per_card` is not set (None), **When** tokens accumulate, **Then** no budget check is performed.

---

### Edge Cases

- What if `tokens_processed` is negative or non-integer? (Clamp to 0; log a warning.)
- What if the performer returns `tokens_processed` as a cumulative total rather than a delta? (V1 assumes delta per poll. Document this contract.)
- What if multiple roles contribute tokens to the same card? (All tokens accumulate into the same `card_tokens_total`; per-role tracking uses Prometheus labels.)

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: `CoordinareState` MUST include `card_tokens_total` (int, default 0) and `card_cost_estimate` (float, default 0.0) fields.
- **FR-002**: `monitor_performer` MUST accumulate `performer_metrics.tokens_processed` into `card_tokens_total` on each poll cycle.
- **FR-003**: `dispatch_performer` MUST reset `card_tokens_total` and `card_cost_estimate` to 0 when dispatching the first role for a new card.
- **FR-004**: `card_cost_estimate` MUST be recalculated as `card_tokens_total / 1_000_000 * cost_per_million_tokens` after each accumulation.
- **FR-005**: A Prometheus counter `coordinare_card_tokens_total` MUST be incremented with labels `{role=<performer_stage>}` on each accumulation.
- **FR-006**: A Prometheus gauge `coordinare_card_cost_estimate_dollars` MUST reflect the current card's estimated cost.
- **FR-007**: The dashboard MUST display current card token usage and estimated cost.
- **FR-008**: When `cost_budget_per_card` is configured and the estimated cost exceeds it, a notification event MUST be dispatched exactly once per card.
- **FR-009**: Configuration MUST include `cost_per_million_tokens` (float, default 3.0) and `cost_budget_per_card` (float | None, default None) under a `cost_tracking` section.

### Key Entities

- **CostTrackingConfig**: New Pydantic model with `cost_per_million_tokens` and `cost_budget_per_card`.
- **card_tokens_total**: Accumulated token count on CoordinareState.
- **card_cost_estimate**: Derived cost on CoordinareState.
- **card_budget_alert_sent**: Boolean flag to prevent duplicate alerts.

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: Token counts from all performer roles accumulate correctly into a single per-card total across the full lifecycle.
- **SC-002**: The dashboard displays a non-zero cost estimate within one poll cycle of the performer reporting tokens.
- **SC-003**: A budget alert fires exactly once when the threshold is crossed, with no duplicates on subsequent polls.
- **SC-004**: Prometheus metrics for token usage are queryable with per-role labels.

## Assumptions

- `tokens_processed` in the performer status response is a delta (tokens used since last poll), not a cumulative total. This contract must be documented.
- Cost estimation uses a single flat rate. Per-model pricing (different rates for input vs. output tokens, different models) is out of scope for V1.
- Token tracking state (`card_tokens_total`, etc.) is not persisted by StateStore. It resets on daemon restart like other in-memory fields.
