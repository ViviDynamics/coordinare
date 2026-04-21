# Data Model: Card Assignment Filtering

## Config Change

### CoordinareConfig (src/coordinare/config.py)

New field added to existing model:

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `assignee_filter` | `str \| None` | `None` | GitHub login to filter TODO cards by. If set, only cards assigned to this login are dispatched. |

### Config YAML example

```yaml
assignee_filter: coordinare-bot
```

### Env var

`COORDINARE_ASSIGNEE_FILTER=coordinare-bot`

---

## Board Dict Extension

`GitHubService.poll_board()` return dict gains one new key:

| Key | Type | Description |
|-----|------|-------------|
| `item_assignees` | `dict[str, list[str]]` | Maps project item ID → list of assignee logins (lowercased). Empty list if no assignees. |

---

## SSE Snapshot Extension

`DashboardStore.build_snapshot()` return dict gains one new key:

| Key | Type | Description |
|-----|------|-------------|
| `assignee_filter` | `str \| None` | The configured filter value, or null if unset. |

---

## State Changes

None — assignee filtering is stateless (applied per poll cycle from config + board dict). No new fields added to `CoordinareState`.
