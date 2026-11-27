---
name: merge-pr
description: Use when a pull request looks ready to merge, when mergeStateStatus shows BLOCKED or BEHIND with green checks, or when deciding whether a stacked child PR can merge ahead of its parent.
license: Proprietary
compatibility: Requires gh, jq, git and a repo.env in the consumer repo. Scripts are bash 3.2 compatible.
metadata:
  version: "1.2.0"
  owner: Vivi Dynamics
  requires: ci-safety
---

# Merge PR

Merge on the first genuinely green state. Waiting buys nothing: CI does not get greener.
Rules: ci-safety §1 and §2.

**Where the scripts are.** `S` is this skill's `scripts/` directory, next to this
SKILL.md. Set it once per session, for example `S=.agents/skills/merge-pr/scripts`
(or `.claude/skills/merge-pr/scripts` where that is the installed copy), then run every
script as `$S/<script>`.

## Inputs

- PR number. If not given: `$S/vgh pr view --json number --jq .number`.

## The freshness rule

A PR merges only when its green CI run tested the default branch as it is NOW. A
`pull_request` run tests the head merged into the base as it stood when the run was
created; green says nothing about a base that moved afterwards. A sweep that merges every
new default branch into every open PR keeps them fresh, but cancels their CI on every
merge (ViviDynamics/elm#3867). This check, at merge time, does the same job for one PR,
when it is ready.

`merge-verified` enforces it with exact SHAs, never timestamps. `COORDINARE_MERGE_FRESHNESS`
(ADOPTING.md) says how fresh is proven: `record` (default), `contains`, or `off`.

- **Fresh:** the head already contains the current default branch, or (`record`) the run
  recorded the merge it tested, in a CI step named `Record the tested merge <sha>`, and
  that merge's first parent, its base side, is the current head of the default branch
  (its second parent must be the PR head). A `pull_request` run of another workflow
  without the step counts only when the same event created it; one that started
  earlier may have tested an older base and makes the head stale. Merge.
- **Stale:** the tested base is an older default branch (`contains`: the head does not
  contain the default branch). Not merged. `merge-verified` calls `update-branch` once,
  pinned to the head it checked (`expected_head_sha`), and exits 5 with
  `state: updated` and the new `head_sha`. Watch CI on that head
  (`watch-ci --expect-head <head_sha>`), then run `merge-verified` again. With
  `--wait-min N` it watches that run itself and starts over when it is green, at most
  `COORDINARE_FRESH_ROUNDS` (default 3) times while the default branch keeps moving.
- **Unknown:** the tested base cannot be determined (no run recorded it, an unreadable
  commit, a merge that is not head-into-base). Refused with exit 4. Push or rebase onto
  the default branch to get a run that records it. Never merge around it.

## Preconditions (check all five, report any that fails)

1. Open: `$S/vgh pr view <pr> --json state --jq .state` is `OPEN`. If `MERGED`, run
   `$S/merge-verified <pr>` anyway; it is idempotent and prints the result.
2. Green for the current head: `$S/ci-runs <pr>` prints `state: green`, meaning every
   workflow run on the head concluded success or a documented skip, not only the main
   PR workflow. `pending` means wait (use the watch-ci skill). `failed` means stop; do
   not merge. `parked` means runs concluded `action_required` or executed zero jobs:
   CI never ran, so this is never green; release it per ci-safety §7.
   If `state` is `failed`, read `failed_jobs[].conclusion`. If `all_cancelled` is true
   it is a cancellation cascade (ci-safety §2), not a failure: wait for the run for the
   current head with the watch-ci skill instead of stopping.
   `none` means no CI ran for this head; stop and report unless the consumer's
   `repo.env` sets `COORDINARE_ALLOW_NO_CI=true` because the repo has no CI.
   `merge-verified` re-checks this itself and refuses anything but green.
3. Review gate satisfied, checked by either of:

       $S/vgh pr view <pr> --json reviewDecision --jq .reviewDecision   # APPROVED satisfies the gate
       $S/vgh pr view <pr> --json body --jq .body | grep -c '^### Code review gate'   # 1 or more satisfies the gate

   If neither holds, stop and report `preconditions_failed: review gate missing`;
   never merge silently past a missing gate.
4. No quality gate weakened: `$S/quality-guard --pr <pr>` exits 0 (`clean`, or
   `justified` by a `### Quality gate changes` section in the PR body naming each
   file). Exit 1 means a lint, type, test, coverage or CI config changed, or a
   suppression marker was added, without a justification: stop and report
   `preconditions_failed: quality gate weakened` with the `hits`. Never add the
   justification yourself to get past this; it must come from the issue's scope.
5. Nothing private disclosed: `$S/disclosure-guard --pr <pr>` exits 0 (`clean`,
   `not_public`, or `justified` by a `### Disclosure review` section). Exit 1 means the
   PR would publish a private address, cluster name, home directory or private term:
   stop and report `preconditions_failed: disclosure blocked` with the `hits` (file,
   line and kind only). `private_terms: false` on a public repository is also a stop:
   `preconditions_failed: disclosure terms unavailable`.

## BLOCKED and BEHIND are not red

`mergeStateStatus: BLOCKED` with green checks means a ruleset the author cannot satisfy
alone (for example, last-push approval on a solo PR). The consumer's `COORDINARE_MERGE_FLAGS`
already carry whatever bypass that repo needs. Judge CI by `ci-runs`, never by
`mergeStateStatus`.

`mergeStateStatus: BEHIND` with green checks and `mergeable: MERGEABLE` is not red
either: run `merge-verified`. Behind is not stale. If the green run tested the current
default branch it merges now; if the base moved after the run started it updates the
branch and you merge after that run (see "The freshness rule"). Do not rebase by hand
first. Check the actual `mergeable` field:

    $S/vgh pr view <pr> --json mergeable --jq .mergeable

- `MERGEABLE`: run `merge-verified` (it merges, or updates a stale branch first).
- `CONFLICTING`: rebase, push, and re-run CI instead.
- `UNKNOWN`: re-read (transient API state).

## Stacked PRs

`$S/repo-config COORDINARE_DEFAULT_BRANCH` prints the consumer's default branch as a bare
string, so it drops straight into these commands.

    git fetch origin "$($S/repo-config COORDINARE_DEFAULT_BRANCH)"
    git fetch origin "pull/<pr>/head"
    git log --oneline "origin/$($S/repo-config COORDINARE_DEFAULT_BRANCH)..FETCH_HEAD"

This never changes your checkout. If the log shows this PR contains another open PR's
commits and this PR is green, merge this one now. The parent then needs
`git rebase --onto origin/$($S/repo-config COORDINARE_DEFAULT_BRANCH) <old-child-head>` or
becomes empty; report that, but never wait on the parent.

## Merge

    $S/merge-verified <pr>

- exit 0: merged. The JSON has `merge_commit`.
- exit 1: not merged. The JSON `reason` carries the merge command's output. Report it
  verbatim. Do not retry with different flags; flags are the consumer's decision.
- exit 4: preconditions failed. The JSON `reason` says why (for example the PR is
  CLOSED, CI on the head is `parked`, `pending`, `failed` or `none`, with the run ids,
  or the base the green run tested cannot be determined). Report it; do not merge.
  `merge-verified` pins the merge to the head it checked with `--match-head-commit`,
  so a push after the check makes the merge fail rather than merge untested code.
- exit 5: updated. The base moved after the green run started, so the branch was
  updated with the default branch instead of merged. `head_sha` is the new head.
  Watch its CI with `watch-ci --expect-head <head_sha>`, then run `merge-verified`
  again. This is the normal path when the default branch is busy, not a failure.

## Report

Print the `MERGE_RESULT` JSON and validate it: `$S/validate merge_result <file>`.
Say `merged` only when the block says `merged`. A merge command's own success line is
not evidence; the read-back is.

## Red flags

- "BLOCKED, so CI must be failing": run `ci-runs`.
- "BEHIND, so I'll wait for the run to be fresh": run `merge-verified`; it decides from the SHA the run tested.
- "Green, so it is safe to merge with `gh pr merge` directly": green for which base? Only `merge-verified` checks that the run tested the current default branch.
- "Exit 5 means the merge failed": it means the branch was updated; watch the new head and merge after it.
- "I'll wait for the parent PR first": if this PR is green and contains the parent's
  commits, that wait is pure loss.
- "The merge command printed success": the block says `merged` or it did not merge.
