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

## The solo-author approval trap (#101)

coordinare opens its PRs as its own GitHub identity, and GitHub never lets a PR's
author approve it. So when the target branch requires an approving review, from a
ruleset or classic branch protection, or a code owner's review, merges can never
happen without a human approver. Without this note the closer waits silently.

coordinare checks each symphony's target branch (`base_branch`, default `main`)
about every 15 minutes, and at its first cycle after start:

- **Requirement found.** The Symphonies page's Status column says "Merges need an
  approving review from someone other than <app>. Add a human approver to the loop
  or relax the rule." One warning goes through the notification layer
  (`merge_review_required`) and one entry to the activity feed. It is not repeated
  while the condition holds. Cards waiting on it show the wait reason
  `external_review_required`.
- **Could not read.** The token could not read the protection (classic or
  rulesets). The Status column says "could not read branch protection", and nothing
  is blocked or dispatched because of it.
- **Cleared.** One activity entry, no notification.

To fix it, either add a human approver to the loop, or relax the rule for the
branch. coordinare does not change merge behaviour itself.

The read calls GitHub's branch protection and branch rules endpoints for the target branch.
A token that cannot read them gets a 403, and the condition reads "could not read branch
protection". The classic `repo` scope reads both. A fine-grained token needs repository
access that includes branch protection, so check its permissions if the condition reads
unreadable.

## Related

Review threads are resolved by the reviewer stage (`resolve_pr_review_threads`)
after it verifies the implementer's fixes — this already provides the
"all threads resolved → addressed" signal the detector relies on.
