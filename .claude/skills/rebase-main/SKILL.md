---
name: rebase-main
description: Use when a feature branch has a merge conflict with the default branch, or when the default branch holds a fix a failing lane needs. Rebases, resolves conflicts, re-runs the affected tests, force-pushes with lease, and logs the rebase on the PR.
license: Proprietary
compatibility: Requires gh, jq, git and a repo.env in the consumer repo. Scripts are bash 3.2 compatible.
metadata:
  version: "1.0.0"
  owner: Vivi Dynamics
  requires: ci-safety
---

# Rebase Main

**Where the scripts are.** `S` is this skill's `scripts/` directory, next to this
SKILL.md. Set it once per session, for example `S=.agents/skills/rebase-main/scripts`,
then run every script as `$S/<script>`. Let `B=$($S/repo-config COORDINARE_DEFAULT_BRANCH)`.

## When not to rebase

A green PR that is merely behind `$B`, with `mergeable: MERGEABLE`, does not need a
rebase: merge it (merge-pr). Rebase only for `CONFLICTING`, or when ci-safety §5 shows
`$B` already holds a fix for a failing lane. Every rebase costs a full CI cycle.

## Steps

1. **Validate.** `git branch --show-current` must not be `$B`. `git status --porcelain`
   must be empty; otherwise stop and report uncommitted changes.

2. **Fetch, never check out the default branch.** `git fetch origin "$B"`. Checking
   out `$B` fails inside a linked worktree, and `origin/$B` is all a rebase needs.

3. **Rebase.**
   - Plain branch: `git rebase "origin/$B"`.
   - Stacked branch whose parent PR was squash-merged: the parent's commits are not
     ancestors of `origin/$B`, so a plain rebase conflicts on every one of them.
     `git log --oneline "origin/$B..HEAD"` shows them. Replay only your own commits:
     `git rebase --onto "origin/$B" <last-parent-commit>`.

4. **Resolve conflicts**, file by file from `git status`:
   - Code: keep the branch's intent and incorporate what `$B` changed.
   - Tests: keep both sides where possible.
   - Generated files and lockfiles: take `$B`'s version, then re-run the generator
     named in `COORDINARE_TEST_COMMANDS_FILE` or the house rules. Never hand-merge them.
   - Numbered migrations: never renumber the default branch's; renumber yours after
     the highest one now on `$B`.
   - `git add <file>`, `git rebase --continue`, repeat.
   If a resolution is ambiguous or risky, `git rebase --abort` and stop with the
   conflicted file list. An unattended caller treats that as a stop condition.

5. **Re-verify.** A clean rebase is not a passing build. Run `$S/preflight <issue>`
   and require `$S/preflight --check <issue>` to pass. If the rebased-over commits
   touched areas your diff does not, also run those rows by hand. Fix any break in a
   normal follow-up commit.

6. **Push.** `git push --force-with-lease`. This is the only sanctioned force-push:
   your own unmerged branch, after a rebase, with lease. Never `--force`, never on
   `$B` or someone else's branch. If the lease rejects the push, stop and report it.

7. **Log it on the PR.** If `$S/vgh pr view --json number` finds a PR, append a bullet
   to a `### Rebase log` section of its body (create the section once):

       - Rebased onto <B> at <short sha> (YYYY-MM-DD); conflicts in <files> | no conflicts

   Write it with `$S/vgh pr edit <pr> --body-file <file>`, then read the body back.

## Report

One line: `rebased onto <B>@<sha>: <no conflicts | resolved: files> ; tests: <pass | fail: area>`,
or `aborted: <files>`. The caller counts one rebase against its budget either way.
