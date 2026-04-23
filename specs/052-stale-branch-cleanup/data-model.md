# Data Model: Operational Visibility & Hygiene

## New Dataclasses

### SessionStats (new, src/coordinare/session.py + src/coordinare/transport/subprocess_transport.py)

| Field | Type | Description |
|-------|------|-------------|
| `title` | `str \| None` | Current session title reported by the backend |
| `files_changed` | `int` | Total files changed in the current session |
| `lines_added` | `int` | Total lines added in the current session |
| `lines_removed` | `int` | Total lines removed in the current session |

`SessionStats` appears as a TypedDict field on `CardSession` and as an SSE state payload field per active performer. `None` when the backend does not support the `/api/session` endpoint or when no session is active.

---

## New State Fields

| Field | Location | Type | Description |
|-------|----------|------|-------------|
| `backend_ui_url` | `CardSession`, `CoordinareState`, SSE payload | `str \| None` | Live URL to the performer backend's UI (e.g. opencode at `http://127.0.0.1:<port>`) |
| `session_stats` | `CardSession`, `CoordinareState`, SSE payload | `SessionStats \| None` | Latest session stats polled from the backend |

Both fields are set to `None` when the performer session ends.

---

## Config Changes

### BranchCollisionStrategy (new enum, src/coordinare/config.py)

| Value | Description |
|-------|-------------|
| `"delete"` | Delete the stale branch and create fresh (default) |
| `"suffix"` | Append `-2`, `-3`, etc. to avoid collision |

### CoordinareConfig additions

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `stale_branch_cleanup` | `bool` | `True` | Enable/disable stale branch detection before workspace creation |
| `branch_collision_strategy` | `BranchCollisionStrategy` | `"delete"` | How to handle stale branch collision |

### Config YAML example

```yaml
stale_branch_cleanup: true
branch_collision_strategy: delete   # or: suffix
```

---

## New GitHubService Methods

### `branch_exists(branch_name: str) -> bool`

- HTTP: `GET /repos/{owner}/{repo}/branches/{branch_name}`
- Returns: `True` if 200 OK, `False` if 404
- Auth: Bearer token (same as other REST calls)

### `delete_branch(branch_name: str) -> None`

- HTTP: `DELETE /repos/{owner}/{repo}/git/refs/heads/{branch_name}`
- Returns: None (logs warning on failure, does not raise)
- Auth: Bearer token

---

## State Changes

None. Stale branch cleanup is ephemeral — it happens during workspace setup, before any state mutations occur. `workspace_branch` in state will reflect the final branch name used (after suffix resolution if applicable).

## Log Events

| Event | Level | Fields |
|-------|-------|--------|
| `workspace.stale_branch_delete_attempted` | INFO | `branch`, `card_id` |
| `workspace.branch_suffix_applied` | INFO | `original_branch`, `final_branch`, `card_id` |
| `workspace.stale_branch_delete_failed` | WARNING | `branch`, `error` |
| `workspace.suffix_exhausted_delete_attempted` | WARNING | `branch`, `card_id` |
| `transport.backend_ui_discovered` | DEBUG | `url` |
| `transport.session_stats_error` | DEBUG | `error` or `status`, `url` |
