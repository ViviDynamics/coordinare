# Data Model: Card Dependency Detection

## Entities

### CardDependency

Represents a single dependency relationship from one card to a blocker issue.

| Field | Type | Description |
|-------|------|-------------|
| dependent_item_id | str | Project item ID (PVTI_...) of the card that has the dependency |
| blocker_issue_number | int | GitHub issue number the card depends on (#N from the syntax) |
| source | DependencySource | How the dependency was detected: EXPLICIT (parsed from description) or ASSESSOR (flagged by the assessor role) |
| status | DependencyStatus | Current satisfaction state |

### DependencyStatus (enum)

| Value | Description |
|-------|-------------|
| PENDING | Blocker is on the board and not yet in DONE |
| SATISFIED | Blocker is in DONE column or the issue is closed/merged |
| UNRESOLVABLE | Blocker issue is not on the board and not closed (or doesn't exist) |

### DependencySource (enum)

| Value | Description |
|-------|-------------|
| EXPLICIT | Parsed from card description via "Depends on #N" / "Blocked by #N" / "After #N" / "Requires #N" syntax |
| ASSESSOR | Flagged by the assessor role's semantic analysis of card titles |

### DependencyGraph

Transient in-memory structure rebuilt on each poll cycle. Not persisted.

| Field | Type | Description |
|-------|------|-------------|
| dependencies | list[CardDependency] | All dependency edges across the board |
| by_dependent | dict[str, list[CardDependency]] | Index: dependent_item_id → its dependencies |
| by_blocker | dict[int, list[CardDependency]] | Index: blocker_issue_number → cards depending on it |
| issue_to_item | dict[int, str] | Reverse lookup: issue_number → item_id on the board |
| issue_to_column | dict[int, str] | Reverse lookup: issue_number → board column name |
| cycles | list[list[str]] | Detected cycles (each is a list of item_ids forming a cycle) |

## State Extensions

### CoordinareState additions

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| blocked_by_dependencies | list[dict] | [] | For the current card: list of `{issue_number, title, column, issue_url}` dicts describing unsatisfied blockers. Populated by check_board when a card is skipped due to dependencies. Used by dashboard and notifications. |

### Card dict additions

No new fields on the card dict itself. Dependency information is derived from the description (already present) and the board snapshot (already polled). The `blocked_by_dependencies` state field carries the rendered dependency info for the active card.

### Assessor output extensions

The assessor's JSON response gains an optional field:

| Field | Type | Description |
|-------|------|-------------|
| dependencies | list[int] | Issue numbers the assessor identifies as blockers. Empty list or absent if no dependencies detected. |

## Relationships

```
CardDependency ──── dependent_item_id ──→ Board Item (PVTI_...)
       │
       └── blocker_issue_number ──→ GitHub Issue (#N)
                                       │
                                       ├── On board → lookup column from snapshot
                                       └── Not on board → API check if closed
```

## Validation Rules

- `blocker_issue_number` must be a positive integer (>0)
- `dependent_item_id` must be a non-empty string matching the PVTI_ pattern
- A card cannot depend on itself (self-loops filtered during parsing)
- Circular dependencies are detected at the graph level, not at the individual CardDependency level
- Duplicate dependencies (same dependent + same blocker) are deduplicated during graph construction

## State Transitions

```
CardDependency.status transitions:

  PENDING ──→ SATISFIED    (blocker card moves to DONE or issue closed)
  PENDING ──→ UNRESOLVABLE (blocker disappears from board and is not closed)
  UNRESOLVABLE ──→ PENDING (blocker appears on the board in a non-DONE column)
  UNRESOLVABLE ──→ SATISFIED (blocker issue is closed outside the board)
```

All transitions happen on each poll cycle (stateless rebuild). There is no persistent state machine — the status is recomputed from the current board snapshot.
