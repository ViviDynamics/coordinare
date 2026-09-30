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

Unattended chain: resume detection (idempotent) → claim the issue → plan (M+ scope) → implement with TDD
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

## Preflight before every push

CI is not the first test run. Before every push (the first one, review fixes, CI
fixes, rebases), commit, then run `$S/preflight <issue>`. It runs the
`COORDINARE_TEST_COMMANDS_FILE` rows whose paths the diff touches, cheapest first, and
records the result against `HEAD`. Push only when `$S/preflight --check <issue>`
exits 0. A failure is fixed locally with TDD and preflighted again; it never goes to CI
to find out. Count each run in `preflight_runs`.

## Quality guard before every push

The fastest way to turn CI green is to weaken what CI checks, and that is never the
fix. Before every push, run `$S/quality-guard --body-file <draft PR body>` (or
`--pr <pr>` once the PR exists). It flags any change to lint, type, test, coverage or
CI configuration and any added suppression marker (`rubocop:disable`, `noqa`,
`@ts-ignore`, `xit`, `.skip(`, `continue-on-error: true`, and so on).

- `clean`: push.
- A hit you introduced to get past a failure: revert it and fix the code instead.
- A hit the issue genuinely asks for: name the file and the reason under
  `### Quality gate changes` in the PR body, which makes it `justified`.
- Anything else: stop with `unresolved: quality gate weakened`.

Record the final state in `quality_guard`. merge-pr checks it again before merging.

When CI later fails for a real reason, ask whether a preflight row should have caught
it. If yes, count it in `ci_failures_preflight_would_catch` and name the missing row in
`unresolved`, so the test commands table improves from evidence.

## Lessons (when `COORDINARE_LESSONS=on`)

Each run learns something the next run should not have to rediscover. With lessons on:

- **Stage 1 reads** `COORDINARE_LESSONS_FILE` (default `.agents/lessons.md`) alongside the
  house rules.
- **Record as it happens**, always with a link to the evidence:
  - a verified retry that turned a job green:
    `$S/lessons observe <issue> flake_retry --log-file <saved job log> --log-line "<exact line>" --evidence <run url>`
    (refused unless the line appears verbatim in the log);
  - a real CI failure: `$S/lessons observe <issue> real_failure --area <area> [--preflight-row-missing] --evidence <run url> --text "<root cause>"`;
  - a repo fact that cost more than one cycle to find:
    `$S/lessons observe <issue> repo_fact --text "<one line>" --evidence <url>`.
- **Propose, never apply silently.** After Stage 6, run `$S/lessons propose`. When it
  has candidates, open one small separate PR: `$S/lessons propose --write` appends the
  flake lines and lessons; add any proposed test-command rows by hand with the right
  command. List every line with its evidence in the PR body. The flake file is a
  protected path, so the PR needs a `### Quality gate changes` section; that review is
  the point.

Copy the run's observations into SHIP_RESULT `observations`.

## Checkpoints: resume, never redo

A run can outlive its context window or its session. The checkpoint is its memory.

- **Write one at the end of every stage** and after every push:
  `$S/checkpoint write <issue> <stage> [--pr N] [--plan PATH] [--decision "what and why"] [--unresolved "..."]`.
  Stages: `plan implement pr-open ci review merge closure done stopped`. Record a
  `--decision` for every call a later reader would otherwise have to re-derive (plan
  skipped because S-sized, a finding rejected and why, a retry and its evidence).
- **Read it first** in Stage 0: `$S/checkpoint read <issue>`. Resume at the recorded
  stage; keep the plan, decisions and unresolved items. When `fresh` is false the
  branch moved since the checkpoint: re-run preflight and re-read CI before trusting
  the recorded results. Budgets always carry over; a restart never resets them.
- **After compaction**, read the checkpoint before doing anything else, even when the
  summary seems complete.
- Report `resumed_from: <stage>` in SHIP_RESULT when a run picked up a checkpoint.

## Stage 0: Resume detection (idempotent)

Read the checkpoint first (above). Then detect what exists on GitHub; reads do not
change state.

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
- Otherwise claim the issue first (below), then:
  - Issue is OPEN, open PR exists: verify its worktree is in sync, enter Stage 2 (CI).
  - Branch exists, no PR yet: verify the worktree, push, open PR, enter Stage 2.
  - Nothing exists: Stage 1 (implement).

### Claim before any work

An unassigned issue looks free on the board, so it is claimed before the first file
write, not when the PR opens. Another agent may be minutes into the same issue.

    $S/claim <issue>

It assigns the issue to `COORDINARE_ASSIGNEE` (see ADOPTING.md), or to the token's own login
when that is unset, and reads the assignment back.

- Exit 0, `claimed` or `already-mine`: continue. Record
  `--decision "claimed as <login>"` in the checkpoint.
- Exit 1, `held`: someone else is assigned, or has an open PR that closes the issue.
  `lost-race`: another claim landed first, and the script already removed yours.
  `closed` or `not-assigned`: nothing to claim, or the assignment did not stick.
  Stop with `merge_state: no-pr` (or `open` for a PR that is not yours) and
  `unresolved: claimed by <holders>: <evidence>`. Never unassign someone else, and
  never adopt their branch or PR.

Before any file write, verify location: `git branch --show-current` and
`git rev-parse --show-toplevel` must match the expected worktree path.

## Stage 1: Implement

Strict test-driven development. For M+ scope, write a plan first. The repo's house
rules (`COORDINARE_HOUSE_RULES_FILE`, for example AGENTS.md or CLAUDE.md) bind at every
stage; read them before writing anything.

1. Read the issue, any referenced docs, and the lessons file when lessons are on. Read the "Already delivered / Remaining
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
   then preflight (see "Preflight before every push").

6. Push and open the PR:
   - Body: `Closes #<issue>`, plus a description of what shipped and test counts.
   - Assign the PR to the login that claimed the issue: `$S/claim <issue> --pr <pr>`.
     It reads the assignment back; `not-assigned` goes in `unresolved`. The issue was
     assigned at the claim; `assigned` in SHIP_RESULT reports both read-backs.

Stop conditions at this stage:
- The issue is held by someone else (the claim exited 1).
- Issue premise is contradicted (closed, already shipped).
- Scope requires a decision only a human can make (pricing, legal, secrets, external
  accounts).
- Local test suite fails and cannot be fixed in scope.

On any stop, report Stage 5 with `merge_state: no-pr` and the unresolved item.

## Stage 2: CI

Use the watch-ci skill. Watch the PR's CI for the current head SHA. On failure:

- Infra or runner failure: verified retry within budget.
- Real failure: fix on the branch with TDD, preflight, push, re-enter Stage 2.
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
  "resumed_from": "<stage, only when resumed>",
  "observations": [{ "type": "real_failure", "area": "<area>", "evidence": "<run url>" }],
  "preflight_runs": 0,
  "quality_guard": "clean | justified | blocked",
  "ci_failures_preflight_would_catch": 0,
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

