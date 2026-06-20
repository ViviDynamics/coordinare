# Contract: Rebase Trigger Conditions

This feature adds **triggering** conditions to the existing spec-047 rebase. It does not change `run_rebase_round`, `rebase_branch`, `force_push_with_lease`, or the conflict-resolution path. This contract pins *when* a rebase is initiated.

## Inputs (per reconciliation cycle, per symphony)

| Name | Source | Notes |
|---|---|---|
| `current_main_sha` | `fetch_main_sha(repo_url, token)` | cached per cycle via `_main_sha_cache` |
| `last_known_main_sha` | persisted snapshot field (restored on startup) | `None` = unknown |
| in-flight cards | `active_sessions` | filtered by `detect_stale_branches` (coordinare branch prefix + open PR; active-performer skipped) |
| per-PR mergeability | `github_service` | `CURRENT \| BEHIND \| CONFLICTING \| UNKNOWN` |
| per-card `last_rebase_attempt` | persisted `PersistedSession` field | anti-thrash marker |

## Trigger decision table

| Condition | Action | `reason` tag |
|---|---|---|
| `last_known_main_sha` is None (first run / post-upgrade) | set baseline = current; run proactive check (below) | `proactive_conflict` (if any fire) |
| `current_main_sha != last_known_main_sha` | `run_rebase_round(active_sessions, current_main_sha, …)`; update baseline | `main_moved` |
| baseline unchanged, but an in-flight PR is `CONFLICTING` or `BEHIND` **and** not thrash-guarded | rebase that card (via `run_rebase_round` / `rebase_branch`) | `proactive_conflict` |
| in-flight PR is `CURRENT` | no-op | — |
| in-flight PR mergeability is `UNKNOWN` | defer; re-check next cycle | — |
| card thrash-guarded (`last_rebase_attempt` head+main match, outcome `BLOCKED`/`FAILED`) | skip until head or main changes | — |
| card has no open PR / active performer | skip (existing `detect_stale_branches` behavior) | — |

## Invariants (MUST)

1. **Baseline restore (FR-001):** after startup, `state["last_known_main_sha"]` equals the persisted value (not `None`) whenever a prior run persisted one. The first cycle compares against it rather than re-baselining blindly.
2. **No-op on current (FR-004, SC-003):** a branch already on `current_main_sha` is never rebased or republished. Zero churn.
3. **Lease-only publish (FR-005):** every republish goes through `force_push_with_lease`. No unconditional force-push.
4. **Per-card isolation (FR-006, SC-004):** a `FAILED`/`BLOCKED` card never prevents other in-flight cards from being processed in the same cycle.
5. **No thrash (FR-007, SC-003):** a card whose `(head_sha, main_sha)` matches its `last_rebase_attempt` with a non-progressing outcome is not re-attempted until head or main changes.
6. **Defer on UNKNOWN:** never rebase on an uncomputed mergeability signal.
7. **047 untouched (FR-011, SC-006):** the merge-time rebase path and all existing rebase tests behave identically.
8. **Secret-free (FR-009):** the `rebase.triggered` record and persisted fields carry only SHAs, branch names, card ids, outcomes — never secret values.

## Observability record: `rebase.triggered`

```json
{
  "event": "rebase.triggered",
  "reason": "restart_drift | proactive_conflict | main_moved",
  "card_id": "PVTI_…",
  "branch": "coordinare/PVTI_…/…",
  "prev_main_sha": "<sha|null>",
  "current_main_sha": "<sha>",
  "outcome": "clean | performer_resolved | blocked | skipped | failed"
}
```

(The existing `RebaseRound` Slack/dashboard summary continues to fire alongside.)
