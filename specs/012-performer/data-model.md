# Data Model: 012-performer

**Branch**: `012-performer` | **Date**: 2026-03-03

All entities are in-memory for the duration of a single performance. No persistence layer exists; state is cleared when the performer process exits.

---

## Entities

### Score

The dispatch payload received from the coordinare. Extracted from `ProtocolMessage.payload` on a `dispatch` action.

| Field | Type | Required | Description |
|---|---|---|---|
| `title` | `str` | Yes | Card title — used as the PR title |
| `description` | `str` | No | Card description — used in the PR body |
| `acceptance_criteria` | `list[str]` | No | Acceptance criteria — appended to PR body |
| `repo_url` | `str` | Yes | Full GitHub HTTPS URL of the target repository |
| `branch` | `str` | Yes | Branch name to clone, check out, and push to |
| `github_token` | `str` | Yes | Token with repo read/write + PR creation permissions |
| `base_branch` | `str` | No | Target branch for the PR (default: repo's default branch) |

**Validation rules**:
- `repo_url` MUST match `https://github.com/{owner}/{repo}(.git)?`
- `branch` MUST be a valid git ref name (no spaces, no `..`)
- `github_token` MUST be non-empty

---

### Stand

The ephemeral local workspace for a single performance. Lives for the duration of one `dispatch` → terminal state cycle.

| Field | Type | Description |
|---|---|---|
| `path` | `Path` | Absolute path to the cloned repository directory (OS temp dir) |
| `branch` | `str` | The checked-out branch name |
| `created_at` | `datetime` | When the stand was set up (for timeout calculation) |

**Lifecycle**: Created during `dispatch` handling; deleted via `shutil.rmtree` in the performance `finally` block.

---

### Performance

Represents one complete end-to-end session. Held in-memory in the performer's message loop for the duration of that loop.

| Field | Type | Description |
|---|---|---|
| `session_id` | `str` | UUID4 — generated on dispatch acceptance |
| `stand` | `Stand` | The active workspace |
| `score` | `Score` | The dispatch payload |
| `state` | `PerformanceState` | Current lifecycle state (see State Transitions) |
| `backend` | `BackendAdapter` | Active AI backend adapter instance |
| `pr_url` | `str \| None` | Set when state transitions to `pr_opened` |
| `pr_node_id` | `str \| None` | Set when state transitions to `pr_opened` |
| `open_questions` | `list[str]` | Set when state transitions to `blocked` |
| `error_reason` | `str \| None` | Set when state transitions to `error` |
| `started_at` | `datetime` | Dispatch acceptance time — used for timeout enforcement |

**State Transitions**:
```
[start]
  → accepted       (dispatch received; session created; backend starting)
  → working        (backend actively running)
  → blocked        (backend raised questions requiring human input)
  → working        (relay_feedback received; backend resumed)
  → pr_opened      (backend finished; branch pushed; PR created) [terminal]
  → error          (unrecoverable failure or timeout) [terminal]
  → session_expired (status/relay_feedback for unknown session_id) [terminal]
```

---

### BackendStatus

Internal value object returned by a `BackendAdapter` when the performer polls the running backend.

| Field | Type | Description |
|---|---|---|
| `state` | `Literal["working", "blocked", "done", "error"]` | Backend's current state |
| `questions` | `list[str]` | Non-empty when state is `blocked` |
| `error_reason` | `str \| None` | Non-empty when state is `error` |
| `tokens_processed` | `int \| None` | Best-effort token count from the backend |

---

### PerformerMetrics

Optional telemetry attached to status responses (FR-016). All fields are best-effort.

| Field | Type | Description |
|---|---|---|
| `pid` | `int` | Performer process PID |
| `child_pids` | `list[int]` | Active child process PIDs (backend + any subprocesses) |
| `memory_bytes` | `int \| None` | Current RSS of the entire process tree in bytes |
| `cpu_percent` | `float \| None` | Current CPU utilisation of the entire process tree |
| `tokens_processed` | `int \| None` | Token count from the backend adapter, if available |

---

### PerformerMessage (wire protocol — inbound)

Mirrors `coordinare.protocol.ProtocolMessage` exactly. Must stay schema-compatible.

| Field | Type | Default | Description |
|---|---|---|---|
| `action` | `"dispatch" \| "status" \| "relay_feedback" \| "health"` | required | Message type |
| `session_id` | `str` | `""` | Required for `status` and `relay_feedback` actions |
| `payload` | `dict[str, Any]` | `{}` | Action-specific data |

---

### PerformerResponse (wire protocol — outbound)

Extends `coordinare.protocol.ProtocolResponse` with an optional `metrics` field. The `metrics` field is not present in the current coordinare model; coordinare ignores it (pydantic extra fields are silently dropped on deserialization).

| Field | Type | Default | Description |
|---|---|---|---|
| `status` | `StatusType` | required | Current state |
| `session_id` | `str` | `""` | Echoed from the request |
| `reason` | `str \| None` | `None` | Error description |
| `questions` | `list[str]` | `[]` | Backend questions (when `blocked`) |
| `pr_url` | `str \| None` | `None` | GitHub PR URL (when `pr_opened`) |
| `pr_node_id` | `str \| None` | `None` | GitHub GraphQL node ID (when `pr_opened`) |
| `progress` | `str \| None` | `None` | Optional progress note (when `working`) |
| `metrics` | `PerformerMetrics \| None` | `None` | Best-effort runtime telemetry |

---

## BackendAdapter Protocol (strategy interface)

Each AI backend is a self-contained adapter implementing this interface.

```
BackendAdapter:
  start(stand: Stand, score: Score) → None
    # Launch the backend process in the stand directory with the task from score.

  get_status() → BackendStatus
    # Non-blocking: return the backend's current state from internal tracking.

  relay_feedback(feedback: str) → None
    # Deliver human feedback to the running backend via the adapter's input channel.

  stop() → None
    # Terminate the backend process tree; called in finally blocks.
```

**OpenCodeAdapter** implements this protocol using `opencode acp` mode:
- `start()`: launches `opencode acp` subprocess, sends initial task message as nd-JSON, begins reading event stream in a background task
- `get_status()`: returns current state based on most recent event parsed from the event stream
- `relay_feedback()`: writes new nd-JSON message to the running `opencode acp` process's stdin
- `stop()`: kills process group via `os.killpg`; fallback to psutil kill_tree
