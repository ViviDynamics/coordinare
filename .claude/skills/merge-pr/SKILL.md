---
name: merge-pr
description: Use when a coordinare PR looks ready to merge, when mergeStateStatus shows BLOCKED despite green checks, or when deciding whether a stacked child PR can merge ahead of its parent.
user-invocable: true
argument-hint: [pr-number]
allowed-tools: Bash(gh *) Bash(git *) Read Grep Glob
effort: medium
---

# Merge PR, merge discipline for ViviDynamics/coordinare

**REQUIRED BACKGROUND:** read `.claude/skills/ci-safety/SKILL.md` first. Token resolution
(section 1), head-SHA truth (section 2), conclusion semantics (section 3) and the
required-vs-advisory split (section 4) all apply here.

Core principle: **merge on the first genuinely-green state.** Waiting has a cost and buys
nothing, because CI will not get greener.

## Inputs

- `$ARGUMENTS`, optional PR number. If omitted, detect from the current branch.

## Preconditions, verify each and report any that fails

1. **PR is open.** `gh pr view <N> --json state,mergedAt`. If already `MERGED`, report it
   and exit success. A second invocation is not an error.
2. **The three required checks are green for the PR's current head**, judged by the
   Actions API per ci-safety section 2: `Lint`, `Test`, `Build Success`. Cancelled or
   superseded runs for older SHAs are noise, not blockers. Advisory jobs (Chart, Coverage,
   E2E Browser Tests, Docker Images, and the rest) do not gate the merge, but read a red
   one once before dismissing it.
3. **Zero checks is not green.** A bot version-bump can strand the head with no checks at
   all. Push a real commit after `git pull --ff-only` and wait for a real run.
4. **The review gate is satisfied or its substitution is recorded in the PR body.** If
   neither, stop. Do not merge silently past a missing gate.

## Reading `mergeStateStatus`

coordinare's `main` requires exactly three status checks and **no approving review**, so a
solo-authored PR reaches `MERGEABLE` on its own. This is the opposite of the in-house scheme, where a
`require_last_push_approval` ruleset makes `BLOCKED` permanent and `--admin` mandatory.

If coordinare shows `BLOCKED`, that is a signal to investigate, not to reach for `--admin`.
Read the actual run conclusions first (ci-safety section 3). The usual causes are a
stranded head with zero checks, or a required check still queued.

```bash
TOKEN_FILE="$HOME/.gh_token"; [ -f "$TOKEN_FILE" ] || TOKEN_FILE="$HOME/Workspaces/ViviDynamics/.gh_token"
GH_TOKEN=$(cat "$TOKEN_FILE") gh pr merge <N> --squash --delete-branch
```

Squash is the repo convention. Reach for `--admin` only when you can state which specific
rule is blocking and why bypassing it is correct, and say so in the output block.

## Stacked PRs, merge the green child

If this PR is stacked on another PR's branch or contains its commits:

```bash
git fetch origin main
git log --oneline origin/main..HEAD
```

If the child is green and that log shows it contains the parent's commits, the child is
self-sufficient, so merge it now. Do not wait for the parent, whose red check may be an
unrelated advisory flake. After the child squash-merges, the parent usually needs
`git rebase --onto origin/main <old-child-head>` or becomes empty. Handle or report that.

## Postconditions, verify before claiming

```bash
GH_TOKEN=$(cat "$TOKEN_FILE") gh pr view <N> --json state,mergedAt,mergeCommit
GH_TOKEN=$(cat "$TOKEN_FILE") gh run list --branch main --limit 5
```

Only `state == MERGED` earns the word merged. A command's own log line claiming success is
not evidence.

`Main Branch Build` never runs on PRs, so it first executes after the merge. Check it. A
green PR followed by a red main build is the arch-roulette signature (ci-safety section 8).

## Output contract

```
MERGE_RESULT
pr: <url>
state: merged | blocked:<reason> | preconditions-failed:<which>
merge_commit: <sha or none>
main_build: <green | red:<job> | pending | not-checked>
stacked_parent_action: <none | parent #N needs rebase --onto | parent #N now empty>
```

## Red flags, stop and re-read this skill

- "BLOCKED, so CI must be failing." Check run conclusions for the current head.
- "I will merge with --admin to get past it." coordinare needs no review, so BLOCKED means
  something else is wrong. Find it.
- "All checks green" when the head has zero checks. Zero is not green.
- "The merge command ran, so it merged." Verify `state == MERGED` or say it did not.
- "PR merged, done." Check `Main Branch Build`.
