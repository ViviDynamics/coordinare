---
name: copilot-review
description: Use only when the developer explicitly asks for a GitHub Copilot code review, or when ship-issue's review gate is copilot and the local review is clean. Requests one review, waits for it, and fixes or answers each comment. Never the first reviewer, and never re-requested to check its own fixes.
license: Proprietary
compatibility: Requires gh, jq, git and a repo.env in the consumer repo. Copilot code review must be enabled for the repository. Scripts are bash 3.2 compatible.
metadata:
  version: "2.0.0"
  owner: Vivi Dynamics
  requires: ci-safety
---

# Copilot Review

**Every request is billed.** Each Copilot review spends AI credits on the whole pull
request, not just the latest commit, so a re-request after a fix pays for the entire
diff again. Copilot is a second opinion, not the reviewer of record:

- **Local review comes first.** Before requesting, run the harness's own reviewer on
  the PR (Claude Code's `code-review` skill, Codex `/review`, or a self-review against
  the issue scope and house rules when the harness has none) and apply its Critical and
  Important findings. Skip this only when the developer explicitly says to.
- **Only on request.** Run this skill when the developer asks for a Copilot review or
  ship-issue's `copilot` gate calls it. Never request Copilot on your own initiative,
  from a loop, or as a substitute for the local review.
- **One request per PR by default.** Verify fixes to Copilot's comments with the local
  reviewer, not by asking Copilot again. Request again only when the developer
  explicitly asks for more rounds.

**A request is not a review.** Copilot can accept a review request and never deliver.
The outcome passes only when a review object arrives with `submitted_at` after the
request. "Requested, nothing arrived" is `review_received: false`, and a caller using
this as a gate treats it as unsatisfied, never as a clean review.

**Where the scripts are.** `S` is this skill's `scripts/` directory, next to this
SKILL.md. Set it once per session, for example `S=.agents/skills/copilot-review/scripts`,
then run every `gh` call as `$S/vgh`. Let `R=$($S/repo-config COORDINARE_REPO)`.

## Inputs

- PR number. If not given: `$S/vgh pr view --json number --jq .number`.
- Rounds: 1 unless the developer explicitly asks for more, and never more than 3.

## Round (default: 1)

1. **Request, reliably.** Adding Copilot silently no-ops when it is already requested,
   so clear the slot first, then add, then read back:

       $S/vgh api -X DELETE "repos/$R/pulls/<pr>/requested_reviewers" -f 'reviewers[]=copilot-pull-request-reviewer[bot]' || true
       $S/vgh api -X POST   "repos/$R/pulls/<pr>/requested_reviewers" -f 'reviewers[]=copilot-pull-request-reviewer[bot]'
       $S/vgh api "repos/$R/pulls/<pr>" --jq '[.requested_reviewers[].login]'

   If `copilot-pull-request-reviewer[bot]` is absent, stop with
   `review_received: false`, reason "request did not land (is Copilot review
   enabled?)". Record the UTC request time.

2. **Wait for the review.** Every 2 minutes, up to 20 minutes, read
   `$S/vgh api "repos/$R/pulls/<pr>/reviews"` for a review whose `user.login` contains
   `copilot` and whose `submitted_at` is after the request time. The reviews API lags,
   so also check `repos/$R/pulls/<pr>/comments` for Copilot comments created after the
   request. Use the harness's scheduled wait if it has one, otherwise `sleep 120`.
   Nothing after 20 minutes: stop with `review_received: false`.

3. **Collect this round's comments:** Copilot-authored, `created_at` after the request
   time (`$S/vgh api "repos/$R/pulls/<pr>/comments" --paginate`). Keep `id`, `path`,
   `line`, `body`, `diff_hunk`. None: the review is clean; stop.

4. **Judge each comment** against the code and the PR's intent:
   - Valid (a bug, missing validation, a security issue, a clear improvement within
     scope): fix it minimally, test first where the fix changes behavior.
   - Not applicable (false positive, out of scope, contradicts the house rules in
     `COORDINARE_HOUSE_RULES_FILE`, a repeat of something already handled): do not change code.
   Reply to each in one or two sentences
   (`$S/vgh api -X POST "repos/$R/pulls/<pr>/comments/<id>/replies" -f body=...`),
   then resolve its thread with the GraphQL `resolveReviewThread` mutation, finding the
   thread id from `pullRequest.reviewThreads`.

5. **Push fixes.** Stage only the files you changed (never `git add -A`), commit
   "Address Copilot review (round N)", run `$S/preflight <issue>`, and push only when
   `$S/preflight --check <issue>` passes. Never amend or force-push.

6. **Verify the fixes locally.** Run the local reviewer on the new head and apply its
   Critical and Important findings. Go back to step 1 only when the developer asked for
   more than one round and rounds remain. Only dismissals this round: stop.

## Report

End with one machine-readable line:

    COPILOT_RESULT review_received: <true|false> rounds: <n> fixed: <n> dismissed: <n> unresolved: <n>

Rounds used up with comments still open: list them with their links.
