# Contract: Pre-Dispatch Rebase Guard

Adds a rebase check at the dispatch decision in `dispatch_performer`, before `_dispatch_performer_body`. Reuses 047/096 functions unchanged. Pins *when* a performer is dispatched vs the branch rebased/blocked.

## Precondition (where the guard runs)

- Inside `dispatch_performer`, after the per-`(card,stage)` mutex + `check_inflight` (so **no performer is running** for this card/stage — FR-004 holds by construction) and after the multi-PR guard, immediately before `return await _dispatch_performer_body(state)`.

## Inputs

| Name | Source |
|---|---|
| `current_card`, `pr_url`, `pr_node_id`, `workspace_branch` | the dispatch state / session |
| `current_main_sha` | `fetch_main_sha(repo_url, token)` (per-cycle cached), else persisted `last_known_main_sha` |
| mergeability | `github.check_mergeability(pr_node_id)` → `mergeable_raw`, `merge_state_status`, `head_ref_oid` |
| `last_rebase_attempt` | per-card marker (096) |
| auto-rebase enabled | same gate as 047/096 |

## Decision table

| Condition | Action |
|---|---|
| no open published PR (first run / branch not created) | **dispatch as today** (guard N/A) |
| auto-rebase disabled | **dispatch as today** |
| mergeability `UNKNOWN` or `head_ref_oid` empty | **defer**: return state unmodified, no dispatch, no rebase (retry next cycle) |
| `MERGEABLE`/`CLEAN` (current) | **dispatch as today** (no rebase, no churn) |
| `CONFLICTING`/`BEHIND`, thrash-guarded | **block-no-dispatch**: do not re-rebase, do not dispatch onto the conflicting base |
| `CONFLICTING`/`BEHIND`, not guarded → rebase → `CLEAN`/`PERFORMER_RESOLVED` | **dispatch on rebased head** (republished with lease) |
| … → `BLOCKED` | route via `prepare_conflict_resolution`, **return held/blocked, no dispatch** |
| … → `FAILED` | **no dispatch this cycle**, return state (retry); failure isolated |

## Invariants (MUST)

1. **No *fresh* performer on a conflicting base (FR-001/002, SC-001/002):** a fresh performer dispatch happens only when the base is current — already, or after a clean rebase. A conflicting base never receives a fresh, no-feedback performer.
   - **Carve-out — bounded conflict-resolution dispatch (047 US2):** resolving a merge conflict inherently requires dispatching the implementer onto the conflicting branch *with* conflict-resolution feedback (this is the existing 047/096 mechanism; removing it would regress 096). The guard therefore lets a dispatch carrying `relay_feedback` through. This is **bounded, not the livelock**: `relay_feedback` is consumed by `_dispatch_performer_body` (one delivery), and a resolution attempt that does not move the branch head trips the anti-thrash marker (`blocked_thrash`) on a later cycle → the card is then held for an operator. So a conflict gets **at most one performer resolution attempt per head** before being held — never an unbounded churn.
2. **No rebase under a live performer (FR-004):** guaranteed by the injection point (post-`check_inflight`).
3. **No-op when not applicable (FR-006, SC-003/006):** no PR / current / disabled → dispatch behaviour byte-identical to today; zero rebase, zero churn.
4. **Lease-only publish (FR-007):** every republish via `force_push_with_lease`.
5. **No thrash (FR-008, SC-003):** a branch BLOCKED/FAILED against the same `(main, head)` is not re-rebased; reuses 096's marker.
6. **Per-card isolation (FR-009, SC-004):** a guard rebase failure for one card never blocks dispatch decisions for others; exceptions are caught.
7. **Defer on UNKNOWN (FR-005):** never rebase or dispatch on an uncomputed mergeability signal.
8. **047/096 untouched (FR-011, SC-006):** the merge-time path and 096's check_board triggers are unchanged.
9. **Secret-free (FR-010):** the `rebase.triggered` (`reason=pre_dispatch`) record + marker carry only SHAs / branch / card id / outcome.

## Observability record: `rebase.triggered` (reason=`pre_dispatch`)

```json
{
  "event": "rebase.triggered",
  "reason": "pre_dispatch",
  "card_id": "PVTI_…",
  "branch": "coordinare/PVTI_…/…",
  "prev_main_sha": "<sha|null>",
  "current_main_sha": "<sha>",
  "outcome": "clean | performer_resolved | blocked | failed"
}
```
