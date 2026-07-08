# Quickstart: Stale / Addressed Human Review Handling

## What this delivers

A card whose PR is gated by a human `CHANGES_REQUESTED` review that's already been addressed no longer sits silently in BLOCKED. Coordinare re-requests the reviewer and moves the card to IN_REVIEW, with one clear notification. The implementer resolves inline threads as it fixes them. Coordinare never clears a human verdict itself.

## Unit-test the decision (no GitHub needed)

```bash
.venv/bin/pytest tests/unit/test_review_staleness.py -q
```
Covers: FRESH (review on head / unresolved threads), STALE_ADDRESSED (behind head + all threads resolved), STALE_ADDRESSED body-only (#111 shape: behind head + no threads + new commits), STALE_UNADDRESSED (behind head + some threads open), threshold boundaries.

```bash
.venv/bin/pytest tests/unit/graph/nodes/test_monitor_pr.py tests/unit/services/test_github_reviews.py tests/unit/test_notify.py -q
```
Covers: `monitor_pr` routes STALE_ADDRESSED → re-request + IN_REVIEW (once, deduped); FRESH → unchanged; extended `get_pr_reviews` shape (commit oid + thread `isResolved`); one `stale_review_surfaced` notification per situation; fail-safe on mocked permission error.

## Live smoke (the #111/#134 class)

1. On a test repo, open a PR; as a human submit **Changes Requested** on commit C.
2. Push several more commits (head advances past C); optionally resolve the inline threads.
3. Point coordinare at the board card and run one poll cycle.
4. **Expect**: card transitions BLOCKED → IN_REVIEW, a re-review request appears for the human reviewer, exactly one `stale_review_surfaced` notification fires, and **no** review is dismissed/approved by the bot.
5. Re-run the cycle unchanged → **no** duplicate notification / re-request.
6. As the human, approve (or dismiss) → card proceeds through the normal merge path.

## Regression guard

On a PR with a human Changes Requested on the **current head** with **unresolved** threads, one cycle MUST leave existing behavior intact (no re-request, no premature IN_REVIEW).

## Config

Staleness threshold (commits-behind / hours-behind) is configurable; default only treats a review as stale when its commit differs from head AND later commits exist — never a review on the current head.

## Success signals

- SC-001: addressed card reaches IN_REVIEW within one cycle of the fixes landing.
- SC-002: exactly one notification per situation.
- SC-003: zero human reviews auto-dismissed/approved.
- SC-006: the #111/#134 shape reaches IN_REVIEW instead of silent BLOCKED.
