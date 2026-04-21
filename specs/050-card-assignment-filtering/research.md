# Research: Card Assignment Filtering

## Decision: Add assignees to existing POLL_BOARD_QUERY

- **Decision**: Extend the single `POLL_BOARD_QUERY` GraphQL call to also return `assignees(first:10){ nodes { login } }` for Issue content nodes.
- **Rationale**: Zero additional API calls. GitHub ProjectV2 items already resolve their content node in one query; adding assignees to the fragment is free. The `first: 10` limit is sufficient — cards with >10 assignees are extremely uncommon in practice.
- **Alternatives considered**: Separate `GET_ISSUE_DETAILS` call per card — rejected (N+1 calls per poll cycle, O(board_size) API hits).

## Decision: Single-login string filter (not list)

- **Decision**: `assignee_filter: str | None` — single GitHub login string, not a list.
- **Rationale**: The primary use case is "assign to the coordinare bot account." Supporting multiple logins adds complexity (AND vs OR semantics, UI display) without a validated need.
- **Alternatives considered**: `assignee_filter: list[str]` — deferred; can be expanded later without breaking change (str → list migration is backward-compatible if treated as "any of these").

## Decision: Case-insensitive login comparison

- **Decision**: Normalize both the config value and fetched logins to lowercase before comparison.
- **Rationale**: GitHub logins are case-insensitive. Config entry `Coordinare-Bot` should match GitHub login `coordinare-bot`.

## Decision: Filter applied after advocate-label filter, before priority sort

- **Decision**: Assignee filter runs after advocate-label filtering (which removes support tickets) but before priority sorting (which is O(n log n)).
- **Rationale**: Keeps the filter chain readable — each filter operates on the already-narrowed eligible_todo list. Filtering before sort avoids sorting cards that will be discarded.

## Decision: No board_snapshot changes needed

- **Decision**: Store `item_assignees` in the existing board dict (returned by `poll_board()`), not in `board_snapshot`. The board dict already holds `item_labels`, `item_field_values`, etc. as parallel dicts.
- **Rationale**: `board_snapshot` only holds the column→[item_id] mapping. Per-item metadata (labels, assignees, field values) lives in the board dict. Consistent with existing patterns.
