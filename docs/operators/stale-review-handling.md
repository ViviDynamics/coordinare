# Stale / Addressed Human Review Handling (spec 128)

GitHub keeps a human **Changes Requested** verdict authoritative until that
human re-reviews or dismisses it — `dismiss_stale_reviews` only clears
*approvals* on new commits, never change-requests. So a change-request left on
an early commit keeps `reviewDecision=CHANGES_REQUESTED` even after the feedback
is addressed across later commits, QA passes, and bots re-approve. Previously
coordinare honored that verdict but left the card silently stuck.

## What coordinare now does

During PR monitoring, when the only thing gating a PR is an outstanding human
`CHANGES_REQUESTED` and nothing fresh is actionable, coordinare classifies it:

- **Stale + addressed** (review commit is behind head *and* its threads are all
  resolved, or it's a body-only review with new commits since) → coordinare
  **re-requests review from that human and moves the card to IN_REVIEW**, and
  emits one deduplicated `stale_review_surfaced` notification naming the
  reviewer, the review vs head commit, and the next action.
- **Stale + unaddressed** (threads still open) → one notification; card stays
  parked (feedback genuinely outstanding).
- **Fresh** (review on/near head) → unchanged behavior.

Coordinare **never** dismisses or approves a human review on their behalf. Every
new action (re-request, card move, notification) is fail-safe — a permission or
API error leaves the card parked and is logged, never crashing the poll cycle.

## To unblock such a card

Re-review the PR and **approve** (or **dismiss** your stale review). Coordinare
then resumes the normal merge path automatically.

## Tuning

Staleness uses a safe default: a review is stale only when its commit differs
from head **and** at least one later commit exists (never a review on the
current head). A configurable commits-behind / hours-behind threshold is the
follow-up knob (T019); the default is wired in the evaluator today.

## Related

Review threads are resolved by the reviewer stage (`resolve_pr_review_threads`)
after it verifies the implementer's fixes — this already provides the
"all threads resolved → addressed" signal the detector relies on.
