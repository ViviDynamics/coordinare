# Data Model: Pre-Dispatch Rebase Guard

**No new persisted state and no schema change.** This feature invokes spec-096's rebase machinery at the dispatch decision point; it reuses 096's fields and the 047/096 models unchanged.

## 1. Reused persisted state (from 096 — unchanged)

| Field | Location | Use here |
|---|---|---|
| `last_known_main_sha` | top-level `WorkflowSnapshot` | source/compare for the current main the guard rebases onto |
| `last_rebase_attempt` (`{main_sha, head_sha, outcome}`) | per-card `PersistedSession` | anti-thrash gate (`should_attempt_rebase`) + written after each guard rebase |

Schema stays at v11 (096). No new field, no version bump.

## 2. Reused models (from 047/096 — unchanged)

- **`RebaseOutcome`**: `CLEAN | PERFORMER_RESOLVED | BLOCKED | SKIPPED | FAILED` — drives the dispatch decision and the marker/observability `outcome`.
- **`RebaseJob` / `RebaseRound`**: produced by `run_rebase_round({card_id: session}, …)`.
- **mergeability descriptor** (from `github.check_mergeability`): `{mergeable_raw, merge_state_status, head_ref_oid, …}`.

## 3. Transient decision (not persisted)

- **Pre-dispatch decision**: computed each time `dispatch_performer` is about to start a performer for an in-flight card with an open PR — one of `{proceed-current, rebase-then-proceed, block-no-dispatch, defer}` (see contracts). Lives only for the node invocation.

## 4. Dispatch decision flow (per in-flight card with an open PR, at the dispatch point)

```
check_inflight passed (no performer running) ─┐
                                              ▼
        card has open published PR? ──no──► dispatch as today (guard N/A)
                  │ yes
                  ▼
        check_mergeability(pr_node)
                  │
   mergeable_raw / merge_state_status / head_ref_oid
                  │
   ┌──────────────┼───────────────────────────────────────────────┐
   │              │                                                 │
 UNKNOWN or     CURRENT (MERGEABLE/CLEAN)                  CONFLICTING or BEHIND
 empty head        │                                                 │
   │               ▼                                       thrash-guarded? ──yes──► block-no-dispatch
 defer        dispatch as today (no rebase)                          │ no
 (no dispatch,                                              run_rebase_round({card})
  retry next cycle)                                                   │
                                              ┌──────────────┬────────┴───────┬───────────┐
                                           CLEAN/        BLOCKED            FAILED       (write marker
                                        PERFORMER_RESOLVED   │                 │          for every case)
                                              │          route conflict-     defer
                                       dispatch on        resolution /     (no dispatch,
                                       rebased head        block card       retry)
                                                          (no dispatch)
```

Every non-current branch that reaches a rebase writes the `last_rebase_attempt` marker and emits a `rebase.triggered` (`reason=pre_dispatch`) record. A performer is dispatched **only** on a current base.
