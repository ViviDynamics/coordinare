# Data Model: Workflow State Persistence

**Feature**: 003-state-persistence
**Branch**: `003-state-persistence`
**Date**: 2026-02-22

---

## WorkflowSnapshot

The `WorkflowSnapshot` is the canonical on-disk representation of the coordinare's current workflow position at a given moment. It is written atomically to a JSON file after every phase transition and read on startup to restore state.

### Pydantic v2 Definition

```python
from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

CURRENT_SCHEMA_VERSION: int = 1

WorkflowPhase = Literal[
    "idle",
    "dispatching",
    "monitoring_agent",
    "monitoring_pr",
    "merging",
    "relay_feedback",
    "blocked",
    "recovery",
]


class WorkflowSnapshot(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    # Compatibility — must equal CURRENT_SCHEMA_VERSION; mismatches are discarded
    schema_version: int = Field(default=CURRENT_SCHEMA_VERSION)

    # When this snapshot was written (UTC)
    snapshot_at: datetime

    # Current workflow phase
    phase: WorkflowPhase

    # Active card — None when phase is "idle"
    active_card_id: str | None = None
    active_card_title: str | None = None
    active_card_column: str | None = None  # Board column name (e.g. "In Progress")

    # PR reference — present in monitoring_pr, merging, relay_feedback phases
    pr_url: str | None = None
    pr_node_id: str | None = None

    # Agent session — present in monitoring_agent, relay_feedback phases
    agent_session_id: str | None = None

    # Open questions — present only in blocked phase
    open_questions: list[str] = Field(default_factory=list)
```

### Validation Rules

| Field | Rule | On Violation |
|---|---|---|
| `schema_version` | Must equal `CURRENT_SCHEMA_VERSION` (1) | Raise `StateLoadError(reason="schema_mismatch")` |
| `snapshot_at` | Valid ISO 8601 UTC datetime | Pydantic `ValidationError` → treated as `StateLoadError(reason="corrupt")` |
| `phase` | One of the 8 enumerated values | Pydantic `ValidationError` → treated as `StateLoadError(reason="corrupt")` |
| `active_card_id` | Non-None when `phase != "idle"` | Log structured warning; do not discard (graceful degradation) |
| `pr_url` / `pr_node_id` | Both non-None in `monitoring_pr`, `merging`, `relay_feedback` | Log structured warning; do not discard |
| JSON bytes | Valid UTF-8 JSON | `json.JSONDecodeError` → `StateLoadError(reason="corrupt")` |
| File encoding | UTF-8 | `UnicodeDecodeError` → `StateLoadError(reason="corrupt")` |

### On-Disk JSON Example (monitoring_agent phase)

```json
{
  "schema_version": 1,
  "snapshot_at": "2026-02-22T14:30:00.000000Z",
  "phase": "monitoring_agent",
  "active_card_id": "PVT_kwDOABCDEF",
  "active_card_title": "Implement user login flow",
  "active_card_column": "In Progress",
  "pr_url": null,
  "pr_node_id": null,
  "agent_session_id": "sess_abc123",
  "open_questions": []
}
```

### On-Disk JSON Example (blocked phase)

```json
{
  "schema_version": 1,
  "snapshot_at": "2026-02-22T15:10:00.000000Z",
  "phase": "blocked",
  "active_card_id": "PVT_kwDOABCDEF",
  "active_card_title": "Implement user login flow",
  "active_card_column": "In Progress",
  "pr_url": null,
  "pr_node_id": null,
  "agent_session_id": null,
  "open_questions": [
    "Should authentication use OAuth or email/password?",
    "Which identity provider is preferred?"
  ]
}
```

### On-Disk JSON Example (idle phase)

```json
{
  "schema_version": 1,
  "snapshot_at": "2026-02-22T16:00:00.000000Z",
  "phase": "idle",
  "active_card_id": null,
  "active_card_title": null,
  "active_card_column": null,
  "pr_url": null,
  "pr_node_id": null,
  "agent_session_id": null,
  "open_questions": []
}
```

---

## StateLoadError

```python
class StateLoadError(ValueError):
    """Raised by StateStore.load() when a state file exists but cannot be safely used."""

    def __init__(self, reason: str, detail: str = "") -> None:
        self.reason = reason  # "corrupt" | "schema_mismatch" | "validation_error"
        self.detail = detail
        super().__init__(f"state load failed [{reason}]: {detail}")
```

---

## StateStore

The `StateStore` encapsulates all I/O for `WorkflowSnapshot`. It is the single point of interaction with the on-disk state file.

### Interface

```python
class StateStore:
    def __init__(self, path: Path, metrics: CoordinareMetrics) -> None:
        """
        path    — absolute path to coordinare.state.json (from config.state_file_path)
        metrics — CoordinareMetrics instance for recording write duration, failures, timestamp
        """

    def verify_writable(self) -> None:
        """
        Called once at startup (before graph compilation and daemon start).
        Creates a zero-byte probe file in path.parent, then removes it.
        Raises OSError if the directory is not writable.
        The caller (.__main__) must catch OSError, log a structured error, and call sys.exit(1).
        """

    async def save(self, snapshot: WorkflowSnapshot) -> None:
        """
        Atomically writes snapshot to self.path via temp-file + os.replace().

        Procedure:
          1. Serialise snapshot to JSON bytes (UTF-8) via snapshot.model_dump_json()
          2. Write to NamedTemporaryFile(dir=path.parent, delete=False, suffix=".tmp")
          3. os.replace(tmp_path, self.path)
          4. Record coordinare_state_write_duration_seconds and
             coordinare_state_last_written_timestamp

        On OSError (e.g. disk full):
          - Increment coordinare_state_write_failures_total
          - Log structured warning: "state write failed; running without persistence"
          - Do not raise (daemon continues in-memory)
        """

    async def load(self) -> WorkflowSnapshot | None:
        """
        Reads and validates the state file.

        Returns:
          - None if self.path does not exist (first run or deleted)
          - WorkflowSnapshot if the file is valid and schema_version matches

        Raises StateLoadError if:
          - File exists but contains invalid JSON  (reason="corrupt")
          - File exists but fails Pydantic validation  (reason="validation_error")
          - schema_version != CURRENT_SCHEMA_VERSION  (reason="schema_mismatch")

        The caller (daemon startup) catches StateLoadError, logs a structured warning,
        and begins a fresh idle cycle.
        """
```

---

## Snapshot-to-CoordinareState Mapping

When a valid `WorkflowSnapshot` is loaded on startup, the following fields are restored into `CoordinareState`:

| WorkflowSnapshot field | CoordinareState field |
|---|---|
| `phase` | `phase` |
| `active_card_id`, `active_card_title`, `active_card_column` | `current_card` dict (id, title, status) |
| `open_questions` | `open_questions` |
| `pr_url`, `pr_node_id` | `current_card` dict (pr_url, pr_node_id) |
| `agent_session_id` | `agent_dispatch["session_id"]` |

Fields **not** restored from snapshot (recomputed on first cycle):
- `board_snapshot` — always refreshed from live board
- `pending_reviews` — refreshed from GitHub on first `monitor_pr` cycle
- `last_poll_at` — set to `None` until first successful board poll
- `error_count` — reset to 0

---

## Config Extension

Add to `ProjectConfiguration` in `src/coordinare/config.py`:

```python
state_file_path: Path = Field(default=Path("./coordinare.state.json"))
```

Environment variable override: `COORDINARE_STATE_FILE_PATH`

---

## Health Response Extension

The `GET /health` response is extended with two new top-level fields:

| Field | Type | Source | Description |
|---|---|---|---|
| `phase` | `string \| null` | `StateStore` last snapshot | Current workflow phase; null if no snapshot loaded yet |
| `snapshot_at` | `string (ISO 8601) \| null` | `StateStore` last snapshot | When state was last persisted; null if never written |

These fields are populated from the `StateStore`'s most recently loaded or saved snapshot, making them available immediately after startup before the first poll cycle completes (FR-007).
