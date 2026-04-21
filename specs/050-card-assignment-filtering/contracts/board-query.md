# Contract: Board Query Assignees Extension

## POLL_BOARD_QUERY — Issue assignees fragment

The `... on Issue` content fragment gains:

```graphql
assignees(first: 10) {
  nodes {
    login
  }
}
```

## poll_board() return shape extension

```json
{
  "snapshot": { "TODO": [...], "IN_PROGRESS": [...], ... },
  "titles": { "PVTI_xxx": "Card title" },
  "item_labels": { "PVTI_xxx": ["bug", "priority:high"] },
  "item_assignees": { "PVTI_xxx": ["coordinare-bot", "jsmith"] },
  "..."
}
```

`item_assignees` values are lowercase-normalized GitHub logins. Cards with no assignees have an empty list (not a missing key).

## SSE snapshot — assignee_filter field

```json
{
  "phase": "idle",
  "assignee_filter": "coordinare-bot",
  "..."
}
```

`null` when not configured.
