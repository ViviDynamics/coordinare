# Phase 1 Data Model — QA Cycle 076

**Spec:** [spec.md](./spec.md) | **Plan:** [plan.md](./plan.md) | **Research:** [research.md](./research.md)

All entities are either (a) Docker labels (external), (b) value objects / enums / records in Python (in-process), or (c) new fields on the existing `PersistedSession` (snapshot-persisted via state_store.py v7). No new database, no new on-disk file.

---

## 1. PerformerContainerLabels (Docker labels — external state)

Every performer container coordinare launches MUST carry these labels at `docker run` time. They are the matching key for reconciliation (FR-002, FR-009).

| Label key | Type | Source | Example | Purpose |
|---|---|---|---|---|
| `coordinare.performer.id` | str | `PerformerEndpointConfig.id` | `claude-ephemeral` | Existing label; identifies the performer config (preserved unchanged) |
| `coordinare.session_id` | str (UUID-like, ≤64 chars) | dispatcher allocation | `9fcadf3c-93ee-…` | Matches the per-card session in coordinare's snapshot |
| `coordinare.card_id` | str (≤64 chars) | `state.active_card.id` | `PVTI_lADOBjmjsc4ApgHnzgq-8Gc` | GitHub project card node id |
| `coordinare.performer_stage` | str (enum) | `state.performer_stage` | `implementing` | Pipeline stage (one of 8) |
| `coordinare.daemon_started_at` | str (ISO8601 UTC) | `daemon.started_at` | `2026-05-28T21:14:17Z` | Daemon-process identity (helps recognise orphans from prior runs) |
| `coordinare.spec_version` | str (constant) | hard-coded | `076` | Schema marker for the label set; lets future migrations evolve labels safely |

**Validation rules:**
- All 6 labels MUST be present on every coordinare-spawned container.
- A container with `coordinare.performer.id` set but missing any of the other 5 is treated as a **pre-076 legacy orphan** and reaped on next startup (no adopt branch).
- A container with NO `coordinare.performer.id` label is NOT touched by the reconciliation pass (could be a user-spawned container that happens to share the image).

---

## 2. ReconciliationDecision (enum, in-process)

The per-card decision emitted by the reconciliation pass for each in-flight card it processes.

```python
class ReconciliationDecision(StrEnum):
    ADOPTED            = "adopted"             # Container found, job-runner healthy, registered into _active_jobs
    REAPED_AND_REPLACED = "reaped_and_replaced" # Container found but unreachable/job-mismatch; stopped, then fresh dispatch
    FRESH_DISPATCHED   = "fresh_dispatched"    # No matching container; normal dispatch
    SKIPPED_PERSISTENT = "skipped_persistent"  # Service is mode=persistent; reconciliation N/A (FR-011)
    ORPHAN_SWEPT       = "orphan_swept"        # Container had no matching session in snapshot; stopped
```

**Logged in:** `daemon.reconciliation_pass_complete` event's per-card breakdown.

---

## 3. WedgeResolution (enum, in-process)

The action taken when `reconciliation.detect_wedged_state()` finds the forbidden state combination.

```python
class WedgeResolution(StrEnum):
    RELEASED    = "released"            # active_card → None (DEFAULT per FR-020 + clarification Q1)
    BLOCKED     = "blocked"             # phase=blocked, card_blocked event, after N wedges in window
    OP_OVERRIDE = "op_override"         # Operator-configured override consulted; details in event's metadata
```

**Default:** `RELEASED`. Per clarification Q1 (recorded in spec.md `## Clarifications`).

**Promotion to BLOCKED:** with the default `wedge_block_threshold=3`, wedges 1–3 release the pin (RELEASED); the **4th** wedge in the trailing 24 h promotes to BLOCKED. Implementation: `if len(window) > wedge_block_threshold:` (the threshold is the maximum number of releases per window, not the count at which promotion fires). (Operator-configurable via `config.yaml`.)

---

## 4. IdleTimeoutRetryRecord (in-session, persisted)

Per-`(card_id, performer_stage)` rolling-window counter for idle-timeout retries (FR-019, clarification Q3).

```python
class IdleTimeoutRetryRecord(BaseModel):
    card_id: str
    performer_stage: str
    window_start_at: datetime         # UTC; rolling 24h window anchor
    attempt_count: int = 0            # incremented per idle-timeout in window
    last_at: datetime | None = None   # most recent idle-timeout
```

**Persistence:** stored on `PersistedSession.idle_timeout_retries: dict[str, IdleTimeoutRetryRecord]` keyed by `<card_id>:<stage>`. Survives daemon restart (FR-019).

**Lifecycle:**
- On first idle-timeout for a `(card, stage)`: create record with `window_start_at=now, attempt_count=1`.
- On subsequent idle-timeout within 24 h: increment `attempt_count`, update `last_at`.
- If `attempt_count` reaches `max_idle_timeout_retries` (default 2): transition card to BLOCKED.
- If `window_start_at` is older than 24 h: reset record (new window).

---

## 5. MultiPRDivergence (in-session, persisted, transient)

Surfaced when multi-PR detection (FR-024) finds > 1 open PR for a card.

```python
class MultiPRDivergence(BaseModel):
    card_id: str
    canonical_branch_prefix: str       # e.g., "coordinare/PVTI_lADO…/"
    pr_numbers: list[int]              # the >1 PRs detected
    detected_at: datetime
    detected_at_trigger: Literal["dispatch", "restart", "webhook"]
    response: Literal["dispatch_refused", "card_blocked"]
```

**Persistence:** stored on `PersistedSession.multi_pr_divergence: MultiPRDivergence | None`. Cleared when an operator resolves the divergence (mechanism out of scope; the field is just an audit surface).

---

## 6. CanonicalBranchName (value object, in-process)

```python
@dataclass(frozen=True)
class CanonicalBranchName:
    card_node_id: str
    title_slug: str

    @property
    def full_name(self) -> str:
        return f"coordinare/{self.card_node_id}/{self.title_slug}"
```

**Construction:** via `dispatch_guard.canonical_branch_name(card: dict) -> CanonicalBranchName`, which extracts `card["id"]` and `card["title"]` and applies the slug algorithm from R-08.

**Determinism:** Same `(card_node_id, title)` → byte-identical `full_name` on every host, every process, every time. Required for FR-022.

---

## 7. ReconciliationReport (in-process, returned from startup pass)

The summary object returned by `run_startup_reconciliation`.

```python
class ReconciliationReport(BaseModel):
    started_at: datetime
    completed_at: datetime
    wall_clock_seconds: float
    cards_considered: int
    decisions: dict[str, ReconciliationDecision]   # keyed by card_id
    orphans_swept: list[str]                       # container IDs
    docker_unreachable: bool                       # set True if FR-012 triggered
```

**Logged in:** `daemon.reconciliation_pass_complete` event body.

---

## 8. New fields on `PersistedSession` (schema v7)

Added to `src/coordinare/session.py` and `src/coordinare/state_store.py`. All default to safe empty values so v6 snapshots load cleanly.

| Field | Type | Default | Purpose |
|---|---|---|---|
| `idle_timeout_retries` | `dict[str, IdleTimeoutRetryRecord]` | `{}` | FR-019 retry counter, keyed by `<card_id>:<stage>` |
| `pr_artefacts_recorded_at` | `datetime \| None` | `None` | FR-015/016 audit timestamp; non-None proves PR fields were written-through after the last successful turn |
| `multi_pr_divergence` | `MultiPRDivergence \| None` | `None` | FR-024 surfaced record |
| `wedge_count_window` | `dict[str, list[datetime]]` | `{}` | FR-020 wedge-promotion threshold; per-card rolling list, trimmed to last 24 h |
| `reconciliation_decisions_last_startup` | `dict[str, str]` | `{}` | Audit trail: which decision was made for which card at the most recent startup (helps post-mortem) |

`_SESSION_FIELDS` tuple in `session.py` is extended with these 5 fields so they round-trip through `state_to_session` / `session_to_state`.

---

## 9. New entries in coordinare config (configurable tunables)

Under a new `dispatcher_dedup:` block in `config.yaml` (validated by Pydantic in `config.py`):

```yaml
dispatcher_dedup:
  enabled: true                            # master switch; default True
  reconciliation_budget_seconds: 30        # SC-002
  drain_budget_seconds: 5                  # FR-007 / clarification Q5
  reap_budget_seconds: 5                   # FR-007 / clarification Q5
  idle_timeout_retries: 2                  # FR-019 / clarification Q3
  idle_timeout_window_hours: 24            # rolling window
  wedge_block_threshold: 3                 # FR-020 promotion threshold
  wedge_block_window_hours: 24
  multi_pr_check_triggers:                 # FR-024 / clarification Q4
    - dispatch
    - restart
    - webhook
```

Defaults are coded to match the clarifications; operators can override per deployment.

---

## 10. State transitions (lifecycle of a card under 076)

This is the merged-FR view of what each terminal performer outcome MUST cause. Compare with FR-018, FR-019, FR-020, FR-007.

| Performer outcome | Action | New state |
|---|---|---|
| `DONE` with new PR artefacts | Record PR fields (FR-015), advance `performer_stage` to next in `lifecycle_sequence`, queue next dispatch | `phase=dispatching, performer_stage=<next>` |
| `DONE` when `lifecycle_sequence` exhausted | Move card to terminal board column (IN_REVIEW or DONE), clear `active_card`, clear session | `phase=idle, active_card=None` |
| `PARTIAL_PROGRESS` | Drain prior container (FR-007), increment relay counter, re-dispatch same stage with relay feedback | `phase=dispatching, performer_stage=<same>` |
| `BLOCKED` (operator-actionable) | Move card to BLOCKED queue, emit `card_blocked`, retain `active_card` | `phase=blocked` |
| `IDLE_TIMEOUT` (within retry budget) | Increment IdleTimeoutRetryRecord, drain+reap prior container, re-dispatch same stage | `phase=dispatching, performer_stage=<same>` |
| `IDLE_TIMEOUT` (retry budget exhausted) | Move card to BLOCKED with `idle_timeout_exhausted` reason | `phase=blocked` |
| `TRANSPORT_ERROR` (existing) | Existing path unchanged; FR-008 ensures reconciliation runs first | (existing behaviour) |

The forbidden state from FR-020 (`active_card != None ∧ active_sessions[id] missing ∧ performer_stage is None ∧ phase ∈ {None, idle}`) is unreachable through this table.

---

## 11. Existing entities affected (not redefined, just touched)

| Entity | Where | What changes |
|---|---|---|
| `_EphemeralJob` (http_performer_service.py:84) | unchanged shape; new constructor path during re-adoption | — |
| `PerformerEndpointConfig` | extra optional `extra_labels` accepted by `start_ephemeral` | small |
| `CoordinareState` (graph/state.py) | no schema change; just new field reads/writes via existing `state[…]` | none |
| `PersistedSession` | adds 5 fields (above) | bumped to v7 |
