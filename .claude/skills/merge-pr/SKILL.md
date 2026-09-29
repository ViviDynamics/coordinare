---
name: merge-pr
description: Use when a pull request looks ready to merge, when mergeStateStatus shows BLOCKED or BEHIND with green checks, or when deciding whether a stacked child PR can merge ahead of its parent.
license: Proprietary
compatibility: Requires gh, jq, git and a repo.env in the consumer repo. Scripts are bash 3.2 compatible.
metadata:
  version: "1.0.0"
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

## Preconditions (check all four, report any that fails)

1. Open: `$S/vgh pr view <pr> --json state --jq .state` is `OPEN`. If `MERGED`, run
   `$S/merge-verified <pr>` anyway; it is idempotent and prints the result.
2. Green for the current head: `$S/ci-runs <pr>` prints `state: green`.
   `pending` means wait (use the watch-ci skill). `failed` means stop; do not merge.
   If `state` is `failed`, read `failed_jobs[].conclusion`. If `all_cancelled` is true
   it is a cancellation cascade (ci-safety §2), not a failure: wait for the run for the
   current head with the watch-ci skill instead of stopping.
   `none` means no CI ran for this head; stop and report unless the consumer's
   `repo.env` documents that the repo has no CI.
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

## BLOCKED and BEHIND are not red

`mergeStateStatus: BLOCKED` with green checks means a ruleset the author cannot satisfy
alone (for example, last-push approval on a solo PR). The consumer's `COORDINARE_MERGE_FLAGS`
already carry whatever bypass that repo needs. Judge CI by `ci-runs`, never by
`mergeStateStatus`.

`mergeStateStatus: BEHIND` with green checks and `mergeable: MERGEABLE` means the branch
needs updates but the code is green: merge now, do not wait for a fresh run or rebase.
Check the actual `mergeable` field:

    $S/vgh pr view <pr> --json mergeable --jq .mergeable

- `MERGEABLE`: use COORDINARE_MERGE_FLAGS to merge now.
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
  CLOSED). Report it; do not merge.

## Report

Print the `MERGE_RESULT` JSON and validate it: `$S/validate merge_result <file>`.
Say `merged` only when the block says `merged`. A merge command's own success line is
not evidence; the read-back is.

## Red flags

- "BLOCKED, so CI must be failing": run `ci-runs`.
- "BEHIND, so I'll wait for the run to be fresh": mergeable MERGEABLE with green CI means merge now, do not wait.
- "I'll wait for the parent PR first": if this PR is green and contains the parent's
  commits, that wait is pure loss.
- "The merge command printed success": the block says `merged` or it did not merge.
