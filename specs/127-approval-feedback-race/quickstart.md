# Quickstart: Verifying the Approval/Feedback Race Fix (127)

## What changed

`monitor_pr` no longer merges a PR when the same poll batch contains unprocessed actionable reviews (CHANGES_REQUESTED/COMMENTED from a human or trusted bot) alongside a human APPROVED review. Feedback routes first; the approval merges on a later cycle once nothing actionable remains. Within a batch, each reviewer's latest review supersedes their earlier ones.

No config. No schema change. Takes effect on restart onto this build.

## Unit verification

```bash
.venv/bin/pytest tests/unit/graph/nodes/test_monitor_pr_approval_race.py -v
.venv/bin/pytest tests/unit -k monitor_pr -q   # regression: existing monitor_pr behaviour
```

## Live verification (staging card)

1. Drive a card to IN_REVIEW with a completed lifecycle.
2. Within one poll interval, have reviewer A approve and reviewer B request changes.
3. Observe logs: `monitor_pr.merge_deferred` (with both review IDs) followed by the normal `monitor_pr.actionable_reviews` relay path — and NO `merge_pr` activity.
4. Resolve B's feedback (e.g. classification dismisses it, or the bounce lands and the lifecycle completes with B re-approving).
5. Observe the standing approval merge on a subsequent cycle with no re-approval by A (provided no code-changing bounce moved the lifecycle cutoff past A's review — in that case branch protection policy governs, by design).

## Operator notes

- The deferral is self-releasing; there is nothing to un-stick. If a merge seems "stuck", check for unprocessed actionable reviews on the PR — the `merge_deferred` event names them.
- A reviewer can supersede their own change request by submitting a later APPROVED review; coordinare honours only their latest state per batch.
