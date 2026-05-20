# Quickstart: 066 Unify Card Pickup — Operator Rollout

**Spec**: [./spec.md](./spec.md)

## What changes for operators

**Nothing visible.** This is a structural refactor:

- No `config.yaml` change required.
- Dashboard SSE payload schema unchanged (top-level `current_card` still emitted).
- v1 snapshots (pre-Fix 7) auto-migrate on first daemon start post-merge.
- The CLI surface, GitHub interactions, and notification routing are unchanged.

## What changes under the hood

- `max_concurrent_cards: 1` now uses the same pickup code path as
  `max_concurrent_cards: 3`; the legacy single-card branch is removed.
- Card state lives on `active_sessions[<card_id>]`; the top-level
  `state.current_card` is a single-site derived mirror.
- IN_REVIEW re-adopt and IN_PROGRESS re-adopt share one
  status-parameterised function.

## Rollout checklist

1. Merge the 066 PR into `main`.
2. On the next daemon restart, observe the log line
   `state_store.v1_snapshot_rehydrated` if a v1 snapshot was loaded.
   Absence of the line means there was no v1 state to migrate (most
   deployments post-065).
3. Verify dashboard `/` and `/events` SSE payloads are unchanged
   (visual diff against a pre-merge capture if needed).
4. Run a single TODO card end-to-end at `max_concurrent_cards=1` to
   confirm the unified path reaches `IN_REVIEW`.
5. Run two concurrent cards at `max_concurrent_cards=3` to confirm
   un-block reset still fires under multi-card mode (FR-003 surfaced
   as US4 in 065).

## Rollback

The 066 changes are confined to the daemon. To roll back:

1. Revert the 066 PR.
2. Restart the daemon. v2 snapshots produced under 066 remain
   readable by the pre-066 code path (no schema change).

## Signals to watch for in production

| Log key | What it means |
|---|---|
| `state_store.v1_snapshot_rehydrated` | v1 → v2 in-memory synthesis fired (one-time, on daemon start) |
| `dispatcher.feedback_cycle_reset` | US4 un-block reset; MUST appear for both N=1 and N>1 (FR-003) |
| `check_board.unified_pickup` (new) | Per-cycle marker that the unified path ran. Absence is a bug. |

If `dispatcher.feedback_cycle_reset` is missing on a confirmed
un-block under `max_concurrent_cards=1`, that is the same class of
bug that 065 US4 fixed for `N>1` — file an issue against 066.
