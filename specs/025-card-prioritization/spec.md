# Feature Specification: Card Prioritization

**Feature Branch**: `025-card-prioritization`
**Created**: 2026-03-24
**Status**: Draft

## Overview

When multiple cards sit in the TODO column, the coordinare today picks the first item in board position order. This feature adds support for a configurable priority strategy: the coordinare can read a GitHub Project V2 custom field (e.g. "Priority") and sort TODO cards by that field before selecting the next card to dispatch. When the priority field is absent or tied, the coordinare falls back to board position order (the existing behavior).

## Clarifications

### Session 2026-03-24

- Q: What priority values should be supported? -> A: Any string values the operator defines in the GitHub Project V2 custom field. The coordinare sorts lexicographically (ascending) by default, with a configurable mapping for semantic ordering (e.g. P0 < P1 < P2).
- Q: What happens if some cards lack the priority field? -> A: Cards without a priority value sort after cards that have one (i.e. null sorts last).
- Q: Should priority affect cards in columns other than TODO? -> A: No. Priority only determines selection order among TODO cards.

## User Scenarios & Testing *(mandatory)*

### User Story 1 -- Respect Priority Field on Cards (Priority: P1)

When multiple cards are in TODO and a priority custom field is configured, the coordinare selects the card with the highest priority (lowest sort value) instead of the first card by board position.

**Why this priority**: Without priority-aware selection, the coordinare processes cards in arbitrary board order, which may not reflect business urgency.

**Independent Test**: Can be tested by providing a board snapshot with three TODO cards carrying different priority field values and verifying the coordinare selects the highest-priority card.

**Acceptance Scenarios**:

1. **Given** three cards in TODO with priority values "P0", "P1", "P2", **When** `check_board` selects the next card, **Then** the card with "P0" is selected.
2. **Given** two cards in TODO where one has a priority value and one does not, **When** `check_board` selects, **Then** the card with a priority value is selected first.
3. **Given** two cards in TODO with the same priority value, **When** `check_board` selects, **Then** the card appearing first in board position order is selected (stable sort).
4. **Given** no `priority_field` is configured, **When** `check_board` selects, **Then** existing board-position-first behavior is preserved (backward compatible).

---

### User Story 2 -- Configurable Priority Strategy (Priority: P2)

The operator can configure the priority field name and an optional value ordering map in `config.yaml` so that semantic priority labels (e.g. "Critical", "High", "Medium", "Low") sort correctly without relying on lexicographic order.

**Why this priority**: Without configurable ordering, operators must name priority values to sort lexicographically, which is fragile and unintuitive.

**Independent Test**: Can be tested by setting `priority_field: "Urgency"` and `priority_order: ["Critical", "High", "Medium", "Low"]` in config and verifying that a card with "Critical" is selected over one with "High".

**Acceptance Scenarios**:

1. **Given** `priority_field: "Urgency"` and `priority_order: ["Critical", "High", "Low"]` in config, **When** two TODO cards have urgency "Low" and "Critical", **Then** the "Critical" card is selected.
2. **Given** `priority_order` is not set, **When** `check_board` sorts, **Then** values are compared lexicographically (ascending).
3. **Given** `priority_field` names a field that does not exist on the GitHub project, **When** `check_board` runs, **Then** it logs a warning and falls back to board position order.

---

### Edge Cases

- What if the GitHub API returns a field value not listed in `priority_order`? (Sort after known values.)
- What if `priority_field` is set but all TODO cards lack that field? (Fall back to board position order.)
- What if the priority field type is a number instead of a string? (Cast to string for comparison, or use numeric sort if all values are numeric.)

## Requirements *(mandatory)*

### Functional Requirements

- **FR-001**: `check_board` MUST sort eligible TODO cards by the configured priority field before selecting the next card to dispatch.
- **FR-002**: Cards without a priority field value MUST sort after cards that have one.
- **FR-003**: When cards share the same priority value, `check_board` MUST preserve board position order (stable sort).
- **FR-004**: When `priority_field` is not configured (None or empty), `check_board` MUST select the first eligible TODO card by board position, preserving backward compatibility.
- **FR-005**: The coordinare MUST support an optional `priority_order` list in config that defines custom sort precedence for priority values.
- **FR-006**: When `priority_order` is not set, the coordinare MUST sort priority values lexicographically (ascending).
- **FR-007**: When the configured priority field does not exist on the GitHub project, `check_board` MUST log a warning and fall back to board position order.
- **FR-008**: The priority field value MUST be read from the GitHub Project V2 field data returned by `poll_board`.

### Key Entities

- **PriorityConfig**: Configuration for priority field name and optional value ordering.
- **PriorityFieldValue**: The raw value of the priority custom field on a card (string or null).

## Success Criteria *(mandatory)*

### Measurable Outcomes

- **SC-001**: With three TODO cards at priorities P0, P1, P2, the coordinare always dispatches the P0 card first.
- **SC-002**: Removing `priority_field` from config produces identical card selection behavior to the current codebase.
- **SC-003**: A misconfigured priority field name logs a warning and does not crash the daemon.

## Assumptions

- The GitHub Project V2 API returns custom field values in the `poll_board` response (or can be extended to do so with a small change to `GitHubService`).
- Priority only affects TODO card selection; it does not reorder cards in other columns.
- The `priority_order` list is static; hot-reload of priority config is out of scope.
