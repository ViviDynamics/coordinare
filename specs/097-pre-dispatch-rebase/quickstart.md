# Quickstart: Pre-Dispatch Rebase Guard

Replays the 2026-06-20 #169/#171 livelock as an acceptance walkthrough. Each scenario maps to a user story + success criteria.

## Prerequisites

- Specs 047 + 096 present (rebase machinery + mergeability read + anti-thrash marker).
- An in-flight card with a published PR, about to be (re)dispatched to a performer.

## Scenario A — US1: conflicting branch is rebased before dispatch (SC-001/002)

1. Card C has an open PR whose branch is `CONFLICTING` with main; the coordinare reaches the dispatch decision (no performer currently running).
2. **Expected (today, the bug):** a performer is dispatched onto the conflicting branch; it can't fix the stale-base conflict; the card stays `monitoring_performer`; 096 skips it forever → livelock.
3. **Expected (097):** the guard reads mergeability = `CONFLICTING` → rebases first:
   - clean → branch republished (lease), performer dispatched on the rebased head;
   - conflict → routed to conflict-resolution / blocked, **no performer dispatched**.
4. **Verify:** no performer ever starts on the conflicting base; a `rebase.triggered` (`reason=pre_dispatch`) record is emitted; the card converges (clean → CI runs) or is operator-visible (blocked) — **zero futile performer dispatches**.

## Scenario B — US1: behind-but-clean branch rebased then dispatched

1. Card C's branch is `BEHIND` main but cleanly rebasable.
2. **Verify:** the guard rebases onto current main, republishes, then dispatches the performer on the up-to-date head.

## Scenario C — US1/edge: current branch dispatches unchanged (SC-003)

1. Card C's branch is already current with main.
2. **Verify:** no rebase, no republish; the performer is dispatched exactly as before this feature.

## Scenario D — edge: first run / no PR yet

1. A brand-new card with no published PR is dispatched (first implementer run that will *create* the branch).
2. **Verify:** the guard is a no-op; dispatch proceeds normally (nothing to rebase).

## Scenario E — US2: never clobber a live performer (FR-004)

1. A performer is already mid-run on a conflicting branch.
2. **Verify:** the guard does not rebase it (the guard only runs post-`check_inflight`, i.e. when no performer is running); the rebase happens only at the *next* dispatch decision, before a new performer starts.

## Scenario F — US2/edge: defer on unknown mergeability (FR-005)

1. The platform has not computed C's mergeability (`UNKNOWN`/empty head).
2. **Verify:** the dispatch decision is deferred one cycle — no rebase, no dispatch.

## Scenario G — US3: no thrash + isolation + observability (SC-003/004/005)

1. C is `BLOCKED` on an unresolvable conflict.
2. **Verify:** not re-rebased next cycle until C's head or main changes; a guard rebase failure on one card doesn't block other cards' dispatch decisions; every guard rebase emits a secret-free record.

## Regression — 047/096 untouched (SC-006)

- With auto-rebase disabled, dispatch behaviour + all existing dispatch/rebase tests are byte-identical.
- The 096 check_board sweep and 047 merge-time path are unchanged.
