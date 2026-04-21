# Feature Specification: Card Assignment Filtering

**Feature Branch**: `050-card-assignment-filtering`
**Created**: 2026-04-21
**Status**: Draft
**Input**: Coordinare currently picks up ANY card in the TODO column regardless of assignee. During live testing, coordinare repeatedly picked up the wrong card because multiple cards were in TODO. We need a way to filter cards by assignee so coordinare only picks up cards explicitly assigned to a designated bot or user account.

## User Scenarios & Testing *(mandatory)*

### User Story 1 — Coordinare only picks up assigned cards (Priority: P1)

When `assignee_filter` is set in config, coordinare only dispatches cards assigned to the configured GitHub login. Cards in TODO with a different (or no) assignee are skipped entirely. When `assignee_filter` is not set, coordinare behaves exactly as today (picks up any TODO card).

**Why this priority**: In multi-person projects where human engineers also use the same GitHub Project board, coordinare was picking up human-owned cards and starting AI performers on them — causing confusion and wasted work.

**Independent Test**: Create two cards in TODO: one assigned to `coordinare-bot`, one assigned to `human-engineer`. Set `assignee_filter: coordinare-bot` in config. Run a poll cycle. Verify coordinare only dispatches the `coordinare-bot` card and leaves the `human-engineer` card untouched.

**Acceptance Scenarios**:

1. **Given** `assignee_filter: coordinare-bot` in config and a TODO card assigned to `coordinare-bot`, **When** coordinare runs a poll cycle, **Then** the card is picked up and dispatched normally.
2. **Given** `assignee_filter: coordinare-bot` in config and a TODO card assigned to `human-engineer`, **When** coordinare runs a poll cycle, **Then** the card is skipped (left in TODO, no performer dispatched).
3. **Given** no `assignee_filter` in config and any TODO card, **When** coordinare runs a poll cycle, **Then** coordinare behaves as before (picks up any card).
4. **Given** `assignee_filter: coordinare-bot` in config and a TODO card with NO assignee, **When** coordinare runs a poll cycle, **Then** the card is skipped.

---

### User Story 2 — Dashboard shows filter status (Priority: P2)

When `assignee_filter` is active, the dashboard idle state shows which assignee filter is in effect, so operators can immediately understand why some TODO cards are not being picked up.

**Independent Test**: Set `assignee_filter: coordinare-bot`, leave a card in TODO assigned to someone else. Open dashboard. Verify idle state mentions the assignee filter.

**Acceptance Scenarios**:

1. **Given** `assignee_filter` is configured, **When** the operator views the idle dashboard, **Then** the active filter (e.g. "Filtering by assignee: coordinare-bot") is visible.
2. **Given** no `assignee_filter` configured, **When** the operator views the idle dashboard, **Then** no filter indicator is shown.

---

## Functional Requirements

- **FR-001**: Add optional `assignee_filter: <github_login>` to `CoordinareConfig` (pydantic-settings, string or null, default null).
- **FR-002**: In `check_board.py`, after fetching TODO cards, filter the list to only cards where `assignees` contains `assignee_filter` (case-insensitive login comparison). If `assignee_filter` is null/empty, skip filtering.
- **FR-003**: Log a structured `check_board.assignee_filtered` event at INFO level when cards are skipped, including the count of skipped cards and the filter value.
- **FR-004**: Include `assignee_filter` in the SSE snapshot so the dashboard can display it.
- **FR-005**: The GitHub GraphQL board query must include `assignees { login }` in the card/issue nodes so the filter has data to work with.

## Non-Goals

- Filtering by multiple assignees (single login only for now).
- Filtering by label, milestone, or other card attributes.
- Automatically assigning cards to the bot (assignment is a manual operator action).
