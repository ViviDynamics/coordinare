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

coordinare checks the `main` branch of each symphony's repository about every 15
minutes, and at its first cycle after start. (The daemon has no per-symphony base
branch setting. Rebase and the CI gate assume `main` the same way, so a repository
whose default branch has another name is not checked.)

The check reads the classic branch protection, the branch's rules, and the detail of
each ruleset those rules name. A ruleset counts only when it is `active`
(evaluate-mode and disabled rulesets do not block a merge), and only when coordinare's
App is not a bypass actor for it. An organisation admin or team bypass is not taken as
coordinare, because the API does not say whether coordinare is a member. A classic
protection that lists coordinare's App in its pull request bypass allowances is treated the
same way. A ruleset whose detail cannot be read is kept, so the condition still applies, and
its id is named in the activity entry for the change.

- **Requirement found.** The Symphonies page's Status column says "Merges need an
  approving review from someone other than <app>. Add a human approver to the loop
  or relax the rule." This applies to a required approving review, a code owner's
  review, or a last-push approval. Elm's `main` ruleset is one: it requires no
  approving review but requires the last push to be approved. One warning goes
  through the notification layer (`merge_review_required`) and one entry to the
  activity feed. It is not repeated while the condition holds. Cards waiting on it
  show the wait reason `external_review_required` when their PR is blocked and its
  checks have settled. While checks are failing or still running, the wait is CI, so
  the card is not tagged.
- **Bypassed.** coordinare's App is a bypass actor for the requirement, so merges are not
  held for a review. The Status column says nothing, no notification is sent, and one
  activity entry records the change. A PR that GitHub reports as needing a review is not
  tagged external in this state.
- **Could not read.** The token could not read the protection (classic or
  rulesets). The Status column says "could not read branch protection", and nothing
  is blocked or dispatched because of it.
- **Cleared.** One activity entry, no notification.

The condition is held in memory. A restart of the daemon reads it again at each
symphony's first cycle, and because the warning is deduplicated only against the
condition seen since the restart, a standing requirement is notified once more after
each restart.

The Status column shows the condition on an Active symphony in place of its usual
reason. On a Paused or Error symphony it is added after the usual reason, so the
condition is still visible. A Disabled symphony is not checked, so it shows nothing
here.

To fix it, either add a human approver to the loop, or relax the rule for the
branch. coordinare does not change merge behaviour itself.

The read calls GitHub's branch rules, branch protection and ruleset endpoints for
`main`. A token that cannot read them gets a 403, and the condition reads "could not
read branch protection". The classic `repo` scope reads all of them. A fine-grained
token needs repository access that includes branch protection, so check its
permissions if the condition reads unreadable.

## Related

Review threads are resolved by the reviewer stage (`resolve_pr_review_threads`)
after it verifies the implementer's fixes — this already provides the
"all threads resolved → addressed" signal the detector relies on.
