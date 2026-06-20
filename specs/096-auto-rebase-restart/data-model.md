# Data Model: Auto-Rebase Restart Resilience

No new store. Single-host single-process JSON snapshot via `state_store.py`. This feature adds one top-level persisted field and one per-card persisted marker, and reuses the existing rebase models unchanged.

## 1. `last_known_main_sha` (top-level, persisted) — NEW

The main-branch SHA the coordinare last reconciled the rebase trigger against.

| Property | Value |
|---|---|
| Location | Top-level `WorkflowSnapshot` (run-global, not per-card) |
| Type | `str \| None` |
| Default (pre-feature snapshots) | `None` ("unknown") |
| Written by | `check_board` after each reconciliation; persisted in the snapshot |
| Restored by | snapshot restore on startup → seeds `state["last_known_main_sha"]` before cycle 1 |
| Secret-safe | yes — a git SHA only |

**Behavior change:** today this lives only in in-memory graph state and is `None` on the first post-restart cycle, so `check_board` initializes it to the live main *without* rebasing. Persisting + restoring it means cycle 1 compares the persisted (pre-restart) value against live main and fires the existing edge on genuine drift (FR-001).

## 2. Per-card rebase-thrash marker (on `PersistedSession`) — NEW

Guards against re-attempting the same rebase every cycle (FR-007).

| Field | Type | Meaning |
|---|---|---|
| `last_rebase_attempt` | `dict \| None` | `{ "main_sha": str, "head_sha": str, "outcome": str }` for the most recent rebase attempt on this card, or `None` |

- Default `None` (pre-feature snapshots load unchanged — FR-010).
- Skip rule: do not re-attempt a rebase for a card when its branch `head_sha` **and** the target `main_sha` both match `last_rebase_attempt` and the recorded `outcome` is terminal-for-now (`BLOCKED`/`FAILED`). A `CLEAN`/`PERFORMER_RESOLVED` outcome changes the head, so the branch is up-to-date next round and `rebase_branch` SKIPs it regardless.
- Round-trips with the session (parallel to the 090 `inheritance_repair_counter` / 095 `env_blocked` per-card fields).
- Secret-safe: SHAs + an outcome enum string only.

## 3. Reused (unchanged) models

- **`RebaseOutcome`** (`models/rebase.py`): `CLEAN | PERFORMER_RESOLVED | BLOCKED | SKIPPED | FAILED`. Used as the marker `outcome` and the observability `outcome`.
- **`RebaseJob` / `RebaseRound`** (`models/rebase.py`): produced by `run_rebase_round`; unchanged. The new triggers call `run_rebase_round` and read its `RebaseJob`s for the marker + observability.
- **Stale-branch descriptor** (`detect_stale_branches` return): `{card_id, branch, pr_number, phase, skipped?}` — unchanged.

## 4. Schema version

Bump `CURRENT_SCHEMA_VERSION` by one (the v10→v11 step). `MIN_SUPPORTED_SCHEMA_VERSION` unchanged. Old snapshots load with `last_known_main_sha = None` and `last_rebase_attempt = None`; the first post-upgrade reconciliation treats live main as the comparison point and heals any conflicting branch via the proactive trigger (FR-010). Add a v10→v11 contract test mirroring the v9→v10 precedent.

## 5. State transitions (per in-flight card, per reconciliation)

```
                       ┌─ branch up-to-date with main ──────────────► SKIPPED (no-op)
                       │
needs-rebase? ─────────┼─ behind, clean rebase ─────────────────────► CLEAN (force-push-with-lease)
 (drift edge OR        │
  mergeable=CONFLICTING│─ conflict, performer resolves ─────────────► PERFORMER_RESOLVED
  /BEHIND, AND not     │
  thrash-guarded)      │─ conflict, unresolvable ───────────────────► BLOCKED (operator)
                       │
                       └─ git/push error ──────────────────────────► FAILED (isolated to this card)

mergeable = UNKNOWN ──────────────────────────────────────────────► defer (re-check next cycle)
```

Each non-SKIPPED transition writes the per-card marker and emits a `rebase.triggered` observability record (`reason ∈ {restart_drift, proactive_conflict, main_moved}`).
