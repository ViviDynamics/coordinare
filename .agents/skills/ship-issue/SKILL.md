---
name: ship-issue
description: Use when a GitHub issue should go all the way to a merged PR without a human in the loop, including when invoked by an unattended session or orchestrator.
license: Proprietary
compatibility: Requires gh, jq, git and a repo.env in the consumer repo (see ADOPTING.md). Scripts are bash 3.2 compatible.
metadata:
  version: "1.0.0"
  owner: Vivi Dynamics
  requires: ci-safety watch-ci merge-pr rebase-main copilot-review
---

# Ship Issue

Issue number in, merged PR out.

Unattended chain: resume detection (idempotent) → plan (M+ scope) → implement with TDD
in a worktree → open PR with `Closes #N` → CI via watch-ci → review gate → merge via
merge-pr → closure and project board updates → final SHIP_RESULT. This skill never waits
for a human; a question you would ask becomes a stop condition in the final summary.

Every repo-specific fact (repo, default branch, merge flags, test commands, review tool,
project) comes from `repo.env` via the scripts. Repo consumers configure these in
ADOPTING.md before using this skill.

**Where the scripts are.** `S` is this skill's `scripts/` directory, next to this
SKILL.md. Set it once per session, for example `S=.agents/skills/ship-issue/scripts`
(or `.claude/skills/ship-issue/scripts` where that is the installed copy), then run every
script as `$S/<script>` and every one-off `gh` call as `$S/vgh`. Let
`R=$($S/repo-config COORDINARE_REPO)`.

## Inputs

- Issue number. Required.
- Optional: whether to skip the plan document (S-sized fixes may skip; M+ scope always
  writes a plan first).

## Budgets (global, fixed)

| Resource | Budget | On exhaustion |
| --- | --- | --- |
| Verified retries per CI job | 2 | classify and report in SHIP_RESULT |
| Rebases onto the default branch | 2 | report churn, stop |
| Review fix rounds | 2 | proceed; unresolved items listed in SHIP_RESULT |
| Watch wall-clock per CI cycle | 60 min | report timeout state |

## Stage 0: Resume detection (idempotent)

Detect if work already exists. Reads do not change state.

```bash
$S/vgh issue view <issue> --repo "$R" \
  --json state,title,body
$S/vgh pr list --repo "$R" \
  --search "closes #<issue>" --state all \
  --json number,state,url,headRefName,baseRefName
```

Branch pattern from `COORDINARE_BRANCH_PATTERN` in `repo.env`. Substitute `{issue}` and
`{slug}` (derived from issue title), and look for the branch with
`git ls-remote --heads origin '<pattern with {slug} as *>'`. Then:

- Merged PR exists: run Stage 5 (closure and board, idempotent), then Stage 6.
- Issue is OPEN, open PR exists: verify its worktree is in sync, enter Stage 2 (CI).
- Branch exists, no PR yet: verify the worktree, push, open PR, enter Stage 2.
- Nothing exists: Stage 1 (implement).

Before any file write, verify location: `git branch --show-current` and
`git rev-parse --show-toplevel` must match the expected worktree path.

## Stage 1: Implement

Strict test-driven development. For M+ scope, write a plan first. The repo's house
rules (`COORDINARE_HOUSE_RULES_FILE`, for example AGENTS.md or CLAUDE.md) bind at every
stage; read them before writing anything.

1. Read the issue and any referenced docs. Read the "Already delivered / Remaining
   scope" block if it exists; implement only the remaining scope.

2. Worktree: `git worktree add` from a fresh `COORDINARE_DEFAULT_BRANCH`, on a branch
   matching `COORDINARE_BRANCH_PATTERN` with `{issue}` and `{slug}` substituted.

3. Plan document (M+ scope only): create `docs/plans/YYYY-MM-DD-<slug>.md` (or the
   plans directory the house rules name) and commit it with the work. S-sized fixes
   may skip the plan document but never skip TDD. Format:

   ```
   # <issue title>

   Issue #<N>

   ## Scope
   In: <what ships>
   Out: <what does not, and why>

   ## Assumptions
   - <each decision the plan makes>

   ## Tasks
   - [ ] 1. <task>: <the failing test that proves it done>
   - [ ] 2. ...
   ```

   Tick each checkbox as the task lands.

4. Strict TDD, per task:
   1. Red: write a test that captures "done" and watch it fail for the right reason.
   2. Green: write the minimal code that passes it.
   3. Refactor with the suite green, then run the area's full suite for regressions.
   4. Commit, one commit per task.
   Test commands per area come from `COORDINARE_TEST_COMMANDS_FILE`. Never write
   implementation code before its failing test.

5. House rules sweep before pushing: re-read `COORDINARE_HOUSE_RULES_FILE` against the diff,
   and run every suite in `COORDINARE_TEST_COMMANDS_FILE` locally.

6. Push and open the PR:
   - Body: `Closes #<issue>`, plus a description of what shipped and test counts.
   - Assign both issue and PR to `COORDINARE_ASSIGNEE` (skip if unset). Read both back via
     `gh pr view --json assignees` and `gh issue view --json assignees`. An empty
     array means assignment did not happen.

Stop conditions at this stage:
- Issue premise is contradicted (closed, already shipped).
- Scope requires a decision only a human can make (pricing, legal, secrets, external
  accounts).
- Local test suite fails and cannot be fixed in scope.

On any stop, report Stage 5 with `merge_state: no-pr` and the unresolved item.

## Stage 2: CI

Use the watch-ci skill. Watch the PR's CI for the current head SHA. On failure:

- Infra or runner failure: verified retry within budget.
- Real failure: fix on the branch with TDD, push, re-enter Stage 2.
- Before any retry, check base freshness: if `COORDINARE_DEFAULT_BRANCH` moved and touches
  the failing area, rebase first (Stage 2b).

On green: go to Stage 3.
On timed out (60 min): re-check once, then stop and report `merge_state: open` with
the last known state and elapsed time.

### Stage 2b: Rebase when stale or conflicting

Use the rebase-main skill (budget: 2 rebases), then re-enter Stage 2. Rebase only for a
conflict or a stale base under a failing lane. A green PR that is merely behind the
default branch is not stale; merge-pr merges it.

## Stage 3: Review gate

Choose a gate from `COORDINARE_REVIEW_TOOL`. Never emit silence.

- `harness`: use the harness's own code reviewer against the PR (for example Claude
  Code's `code-review` skill at high effort, or Codex `/review`). If this harness has
  none, fall back to `self` and say so in the gate record.
- `copilot`: use the copilot-review skill. If Copilot never submits a review, the gate
  is unsatisfied; fall back to `self` and record both.
- `self`: review the diff against the issue scope and the house rules yourself.
- Unset or unrecognized: `self`.

Apply Critical and Important findings, push, and return to Stage 2 (budget: 2 review
rounds). Record the outcome in the PR body under `### Code review gate`: the tool used,
findings, and what was fixed, or "no findings".

## Stage 4: Merge

Use the merge-pr skill. Verify the PR is actually mergeable, then merge with the
consumer's `COORDINARE_MERGE_FLAGS`. If the merge succeeds, read the merge commit SHA.

If the merge fails: report the reason in SHIP_RESULT with `merge_state: blocked`.

After a successful merge: update the local default branch, remove the worktree, and
delete the local branch (`--delete-branch` sometimes removes only the remote side).

## Stage 5: Issue closure and project board

`Closes #<issue>` in the PR body normally closes the issue on merge. Verify, never
assume:

    $S/vgh issue view <issue> --repo "$R" --json state,stateReason

Anything but `CLOSED`/`COMPLETED`: `$S/vgh issue close <issue> --repo "$R" --reason completed`,
then read it back.

Board, only when `COORDINARE_PROJECT_OWNER` and `COORDINARE_PROJECT_NUMBER` are both set (else
`board: skipped`). Board writes need the `project` scope, which a repo-scoped token file
may lack; if a board call is refused, retry it as plain `gh` so it uses the signed-in
account. Look everything up by name, never by a hard-coded id:

    gh project item-list <number> --owner <owner> --format json --limit 1000 \
      --jq '.items[] | select(.content.number==<issue> and .content.repository=="'"$R"'") | .id'
    gh project field-list <number> --owner <owner> --format json \
      --jq '.fields[] | select(.name=="Status") | {id, options}'
    gh project view <number> --owner <owner> --format json --jq .id

- Item already in the option named `Done`: `board: done`.
- Item elsewhere: `gh project item-edit --id <item> --project-id <project> --field-id <field> --single-select-option-id <Done option>`, read back, `board: moved`.
- No item: `gh project item-add <number> --owner <owner> --url <issue url>`, then move it
  to Done, `board: added`.
- A board with no `Status` field or no `Done` option, or a write that fails twice:
  `board: failed`, with the reason in `unresolved`.

## Stage 6: Final summary (always emit)

Write the result as JSON to `.agents/state/<issue>-ship.json`, validate it with
`$S/validate ship_result .agents/state/<issue>-ship.json`, and print it:

```json
{
  "schema": "SHIP_RESULT",
  "issue": 123,
  "pr": 456,
  "merge_state": "merged | open | blocked | no-pr",
  "ci": "green | failed:<job>:<infra|flake|real> (<evidence>) | timed_out (<seconds>s)",
  "review_gate": "harness | copilot | self | unsatisfied",
  "issue_closure": "closed | reopened-and-closed | open | not-applicable",
  "board": "done | moved | added | skipped | failed",
  "assigned": { "issue": "<login or empty>", "pr": "<login or empty>" },
  "budget": { "retries_verified": 0, "retries_limit": 2, "rebases": 0, "rebases_limit": 2,
              "review_rounds": 0, "review_rounds_limit": 2 },
  "unresolved": "what a human must decide, with the evidence",
  "deferred": "issues filed instead of fixed, each with why"
}
```

Every value came from a script or a read-back. Never assume; always verify.

## Stop conditions, deferred work, and deferral policies

A question a human would ask is a stop condition (unresolved in SHIP_RESULT). A gap
that can be closed in this PR should be closed. File an issue only when:
- The work is owned by another active ticket (reference it in deferred).
- The work needs a decision or action only a human can take (document in unresolved).

