# Quickstart: Auto-Rebase Restart Resilience

Replays the 2026-06-20 incident (#168/#169/#171) as an acceptance walkthrough. Each scenario maps to a user story + success criteria.

## Prerequisites

- Spec 047 enabled (auto-rebase machinery present).
- An in-flight card with a published PR branch cut from an older main.

## Scenario A — US1: restart after a merge heals the stale branch (SC-001)

1. Card C's branch is based on `main@A`. A sibling/baseline PR merges → `main@B`.
2. Stop the coordinare **before** it observes `main@B`. Restart it.
3. **Expected (today, the bug):** coordinare baselines `last_known_main_sha = B` on cycle 1, sees no edge, never rebases C → C stays `CONFLICTING`, no CI.
4. **Expected (096):** the persisted baseline restores as `A`; cycle 1 sees `B != A` → `run_rebase_round` rebases C onto `B`:
   - clean → branch republished with lease, CI re-runs (`outcome=clean`);
   - conflict → performer conflict-resolution path (`performer_resolved`), else card blocked for an operator (`blocked`).
5. **Verify:** within a bounded number of cycles C is rebased or operator-blocked, **with no operator action**; a `rebase.triggered` record with `reason=restart_drift` (or `main_moved`) is emitted.

## Scenario B — US2: conflicting branch healed even with no observed edge (SC-002)

1. Coordinare's `last_known_main_sha` already equals live main (e.g. its first-ever observation of this repo's main was already post-merge — the exact #168/#169/#171 case).
2. Card C's branch is nonetheless `CONFLICTING` (cut from an even-older main, never rebased).
3. **Expected (096):** the proactive trigger reads C's mergeability = `CONFLICTING`, initiates a rebase for C independent of any edge (`reason=proactive_conflict`).
4. **Verify:** C is rebased/resolved/blocked within a bounded number of cycles; an in-flight PR that is `CURRENT` is untouched (no churn).

## Scenario C — no churn / no thrash (SC-003)

1. Card D's branch is already `CURRENT` with main.
2. **Verify:** across restarts and cycles, D incurs zero rebases and zero republishes.
3. Card E hits an unresolvable conflict → `BLOCKED`.
4. **Verify:** E is not re-rebased every cycle; it is re-attempted only after main moves or E's head changes (the `last_rebase_attempt` marker).

## Scenario D — per-card isolation (SC-004)

1. Three in-flight branches; one rebase `FAILED` (e.g. transient git error).
2. **Verify:** the other two are still rebased in the same cycle.

## Scenario E — observability + secret-free (SC-005, FR-009)

1. Trigger any of the above.
2. **Verify:** each triggered rebase emits a `rebase.triggered` record with `card_id`, `branch`, `prev_main_sha`, `current_main_sha`, `outcome`, and contains **no** secret values.

## Scenario F — backward compatibility (FR-010)

1. Load a pre-096 snapshot (no `last_known_main_sha`, no `last_rebase_attempt`).
2. **Verify:** it loads cleanly with both defaulting to `None`/absent; the first reconciliation uses live main as the comparison point and heals any conflicting branch via the proactive trigger — no crash, no skipped healing.

## Regression — 047 untouched (SC-006)

- Run the existing rebase + merge_pr test suites; all pass unchanged.
