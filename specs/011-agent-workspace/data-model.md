# Data Model: Agent Workspace Management (011)

## Entities

### WorkspaceInfo (value object)

Transient. Created by `WorkspaceManager.prepare()`, consumed by `dispatch_card` node and `AgentService`. Never persisted to disk or state store.

| Field | Type | Description |
|-------|------|-------------|
| `path` | `Path \| None` | Absolute path to the cloned workspace directory. `None` for K8s transport (performer self-clones). |
| `branch` | `str` | Deterministic branch name (e.g., `coordinare/ITEM_42/add-retry-logic`). |
| `repo_url` | `str` | Plain HTTPS URL (no token): `https://github.com/{org}/{project}.git` |

**Lifecycle**: Created during `dispatch_card`. Path stored in `CoordinareState["workspace_path"]` for teardown reference. Object itself is not stored in state — only the path string.

### WorkspaceManager (service)

One instance per coordinare process. Initialized in `__main__.py` with `ProjectConfiguration`.

| Field | Type | Source |
|-------|------|--------|
| `_github_org` | `str` | `config.github_org` |
| `_project_name` | `str` | `config.project_name` |
| `_github_token` | `SecretStr` | `config.github_token` |
| `_workspace_root` | `Path \| None` | `config.workspace_root` |
| `_agent_transport` | `str` | `config.agent_transport` |

### CoordinareState additions

Two new optional fields added to `CoordinareState` TypedDict:

| Field | Type | Set by | Cleared by |
|-------|------|--------|------------|
| `workspace_manager` | `WorkspaceManagerProtocol \| None` | `__main__.py` at startup | Never (lifetime = process) |
| `workspace_path` | `Path \| None` | `dispatch_card` on success | `monitor_agent` after teardown |
| `workspace_branch` | `str \| None` | `dispatch_card` on success | `monitor_agent` after teardown |

### ProjectConfiguration additions

Two new optional fields on `ProjectConfiguration` (`config.py`):

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `workspace_root` | `Path \| None` | `None` | Parent directory for all workspace containers. When `None`, `tempfile.gettempdir()` is used. Maps to env var `COORDINARE_WORKSPACE_ROOT`. |
| `performer_image` | `str` | `""` | Container image name for K8s Jobs (e.g., `ghcr.io/vividynamics/coordinare-performer:full`). Only read when `agent_transport == "kubernetes"`. Maps to env var `COORDINARE_PERFORMER_IMAGE`. |

## State Transitions

### Workspace lifecycle

```
[card selected for dispatch]
        │
        ▼
  prepare() called
        │
   ┌────┴────┐
   │ success │──────► WorkspaceInfo created
   │         │        workspace_path stored in state
   └────┬────┘        dispatch proceeds
        │ failure
        ▼
  WorkspaceSetupError raised
  card moved to BLOCKED
  no workspace_path in state

[session terminal state: pr_opened / blocked / error / session_expired]
        │
        ▼
  teardown() called with workspace_path
        │
        ▼
  workspace_path cleared from state
```

## Branch Name Format

Pattern: `coordinare/{card_id}/{title_slug}`

| Component | Source | Rules |
|-----------|--------|-------|
| `coordinare/` | Static prefix | Always present |
| `{card_id}` | `card["id"]` | Raw value from GitHub project board (e.g., `PVTI_abc123`) |
| `{title_slug}` | `card["title"]` | NFKD → ASCII → lowercase → `[^a-z0-9_]+` → `-` → strip edges → max 50 chars → fallback `"untitled"` |

**Examples**:
- Card `PVTI_abc` title `"Add retry logic"` → `coordinare/PVTI_abc/add-retry-logic`
- Card `PVTI_xyz` title `"Café ☕ feature"` → `coordinare/PVTI_xyz/cafe-feature`
- Card `PVTI_123` title `"漢字タスク"` → `coordinare/PVTI_123/untitled`
- Card `PVTI_456` title `""` → `coordinare/PVTI_456/untitled`
