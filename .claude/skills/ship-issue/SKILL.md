---
name: ship-issue
description: Use when a coordinare GitHub issue should go all the way to a merged PR without a human in the loop, including when invoked by a subagent or an unattended session.
user-invocable: true
argument-hint: <issue-number>
allowed-tools: Bash(gh *) Bash(git *) Bash(.venv/bin/*) Bash(uv *) Bash(python3 *) Bash(find *) Bash(grep *) Bash(set *) Read Edit Write Grep Glob Agent Skill ScheduleWakeup
effort: high
---

# Ship Issue, issue number in, merged PR out

Composed entry point chaining this repo's workflow skills end-to-end:

```
implement (Stage 1) -> watch-ci -> [rebase-main when stale] -> review gate -> merge-pr
```

**REQUIRED BACKGROUND:** `.claude/skills/ci-safety/SKILL.md`. Every CI interaction in this
chain follows it.

Ported from an in-house project and re-grounded in coordinare's conventions. The substantive
differences from the in-house scheme: coordinare's `main` needs no approving review and no `--admin`, the
implement stage forks between spec-kit and a direct fix branch, and Copilot review does
work in this repo.

**This skill never waits for a human.** It makes every decision from stated rules and ends
with a machine-readable summary. A question you want to ask the user is a **stop
condition**, reported in the final summary, not asked.

## Inputs

- `$ARGUMENTS`, required issue number. Everything else is derived.

## Retry budget (global, fixed)

| Resource | Budget | On exhaustion |
| --- | --- | --- |
| Verified retries per CI job (ci-safety section 5) | 2 | stop: classify and report |
| Same flake signature on this PR (ci-safety section 6) | 1 retry, then root-cause | fix or stop with classification |
| Rebases onto main | 2 | stop: report churn, main is moving under the PR |
| Review-fix rounds | 2 | proceed with unresolved items listed in the PR body |
| Watch wall-clock per CI cycle | 60 min | re-check once, then stop: `watching-timed-out` |

Spent budget is never restored by "it looks different this time". Track it explicitly.

## Stage 0: Resume detection (idempotency)

Safe to invoke twice. Discover what already exists, then enter the chain at the right
stage.

```bash
TOKEN_FILE="$HOME/.gh_token"; [ -f "$TOKEN_FILE" ] || TOKEN_FILE="$HOME/Workspaces/ViviDynamics/.gh_token"
GH_TOKEN=$(cat "$TOKEN_FILE") gh issue view $ARGUMENTS --json state,title,body,labels
GH_TOKEN=$(cat "$TOKEN_FILE") gh pr list --search "closes #$ARGUMENTS" --state all --json number,state,url,headRefName
git branch -a --list "*$ARGUMENTS*"
```

- PR exists and `MERGED`: report done (Stage 5 output), exit success.
- PR exists and `OPEN`: verify its branch and worktree, enter Stage 2.
- Branch exists, no PR: push it, open the PR, enter Stage 2.
- Nothing exists: Stage 1.

**Worktree discipline is a standing rule here.** Multiple sessions share one checkout and
a branch can switch under you mid-task. Before writing any file at any stage, and again
before every commit and push, run `git branch --show-current` and
`git rev-parse --show-toplevel`. Work has previously been written into the wrong branch's
worktree.

## Stage 1: Implement

Pick the lane from the issue's shape. Say which lane you chose in the Stage 5 summary.

**Bug-shaped** (a defect with a reproduction, a wrong value, a missing observation):
a direct TDD fix branch. This is the established pattern for coordinare fixes, and recent
merged history is plain `fix:` PRs, not spec branches.

1. Branch off current `origin/main` (never work on `main`, never push to it).
2. Reproduce the defect first, by execution, and keep the evidence for the PR body.
3. Write the failing regression test, watch it fail, then fix. Use
   `superpowers:test-driven-development`.
4. **Mutation-test the test.** A test that enforces a rule must be broken once per
   instance of that rule. Mutate in the real tree, because editable installs make `/tmp`
   copies lie. A MISSED result usually means the mutation never applied.
5. `.venv/bin/pytest` and `.venv/bin/ruff check`. Run the coordinare and performer test
   trees separately. Never use `-o addopts=""` on the coordinare tree.

**Feature-shaped** (new capability, needs a spec): `Skill("work-issue-speckit")`, which
runs specify, clarify, plan, tasks, analyze, implement. `speckit.analyze` before
`speckit.implement` is mandatory.

Either lane:

- **Stage explicit paths. Never `git add -A <dir>`.** That once swept in-flight untracked
  specs onto main via a squash merge. Check `git status` for `??` before the first commit
  and diff the staged set against your intent every time.
- **A follow-up ticket is a deferral wearing a suit** (see Scope below).
- Both the **issue and the PR** must end up assigned. They are separate objects, and
  assigning one does not assign the other. Verify each with a read-back
  (`gh pr view --json assignees`), because `gh pr edit` exits 0 on a no-op. Treat an
  empty array as the step not having happened.
- The PR body carries the plan, the reproduction evidence, and `Closes #<n>`.

Output of this stage: an open PR URL.

## Stage 2: CI, watch-ci under ci-safety rules

Follow `watch-ci`. Decisions specific to this chain:

- The three **required** checks (`Lint`, `Test`, `Build Success`) green for the current
  head: go to Stage 3. Advisory jobs do not gate, but read a red one once.
- Failure classified **infra/resource**: verified retry within budget.
- Failure classified **known flake, first occurrence**: check base freshness FIRST
  (ci-safety section 7). If `main` touches the failing lane, go to Stage 2b instead, since
  a retry on a stale base can never pass. Otherwise verified retry.
- Failure classified **known flake, second occurrence, or real**: investigate and fix on
  the branch, then re-enter Stage 2. If the fix needs decisions outside the issue's scope,
  that is a stop condition.
- Head has **zero checks**: not green. `git pull --ff-only` and push a real commit.

### Stage 2b: Rebase when stale

Trigger: base freshness shows `origin/main` ahead with commits touching the failing lane,
or the PR has conflicts. Follow `rebase-main` (worktree-safe fetch, `--onto` if stacked on
a squash-merged parent), then re-enter Stage 2. Budget: 2 rebases.

## Stage 3: Review gate, never pass silently

Satisfied by exactly one of, in preference order:

1. `Skill("code-review")` at high effort against the PR. Check availability first, and do
   not pretend to have run a skill the session does not list.
2. **Adversarial review.** This is a standing rule in coordinare: run an adversarial review
   over the **full diff** before merge. Reviews are a net, not a clearance, so re-verify
   every claim by executing it. Three separate runs have each missed the most severe item.
3. `/copilot-review`. Copilot does deliver reviews in this repo (unlike the in-house scheme), via
   `copilot-pull-request-reviewer[bot]`. Zero threads after a trigger is not a pass, only
   a review object that actually arrived counts.
4. **Recorded substitution**, if none of the above produced a real review. Append to the
   PR body:

   ```
   ### Code review gate
   <which gates were unavailable and why>. Substituted: self-review against the issue's
   acceptance criteria, by execution, with <findings or "no findings">.
   ```

Silence is the only forbidden outcome. A missing gate that is not recorded in the PR body
is a failure of this skill even if the code is perfect. Apply review fixes (budget: 2
rounds), pushing and re-entering Stage 2 after each push. When receiving review feedback,
use `superpowers:receiving-code-review`: verify each point technically rather than
agreeing performatively.

## Stage 4: Merge

Follow `.claude/skills/merge-pr/SKILL.md`: squash, `--delete-branch`, **no `--admin`**
(coordinare requires no approving review, so BLOCKED here means something else is wrong),
merge on the first genuine green, and verify `state == MERGED` before claiming it.

Then check `Main Branch Build`, which never runs on PRs:

```bash
GH_TOKEN=$(cat "$TOKEN_FILE") gh run list --branch main --limit 5
```

## Scope: shipping means closing, not cataloguing

**A follow-up ticket is a deferral wearing a suit.** If a gap is one you could close in
this PR, close it. Filing it instead converts finishable work into backlog and burns a
session producing a document. Fix nits now and update tests alongside. Never defer with
"follow-up" language.

File only when the work is genuinely owned by another active ticket, or needs a decision
or action only the user can take (a secret, a runner, a host with logs you cannot reach, a
pricing or legal call, a sign-off). In that case assign it to them and set the board to
Blocked so it is visible rather than buried in a comment.

If you do defer, the Stage 5 `deferred:` line must say what you filed and why closing it
was not possible. "It felt out of scope" is not a reason.

## Stop conditions (report, do not loop)

Stop immediately and emit the Stage 5 summary when:

- A failure is classified **real**, or the same flake signature hits twice and
  root-causing needs decisions beyond the issue's scope.
- Any budget row is exhausted.
- The review gate can neither be satisfied nor recorded.
- `merge-pr` preconditions fail for a reason no stage above can fix.
- The issue or board state contradicts the premise (issue closed, PR by someone else).
- The issue's actual ask needs access you do not have (host logs, a gateway, a device).

## Stage 5: Final summary (always emit, whatever the outcome)

```
SHIP_RESULT
issue: #<n> <title>
lane: bugfix-tdd | speckit
pr: <url or none>
merge_state: merged | open | blocked:<reason> | no-pr
ci: green | failed:<job>:<infra|known-flake|real>: <one-line evidence>
main_build: <green | red:<job> | pending | not-checked>
review_gate: code-review-skill | adversarial-review | copilot-received | substitution-recorded | UNSATISFIED
assigned: issue=<login|NONE> pr=<login|NONE>   (read back, not assumed)
budget: retries=<x>/2 rebases=<y>/2 review_rounds=<z>/2
unresolved: <none | what a human must decide, with the evidence>
deferred: <none | issues filed instead of fixed, each with why it could not be closed here>
```

Every claim in this block must have been verified per the underlying skill's
postcondition. `merged` means you saw `state == MERGED`, `retries=1` means you saw
`run_attempt` increment. Numbers you did not verify do not go in the block.
