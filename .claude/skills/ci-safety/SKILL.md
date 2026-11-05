---
name: ci-safety
description: Use when reading CI state, deciding whether a CI failure is a flake, retrying a GitHub Actions run, or writing any poll loop over CI. Background rules for watch-ci, merge-pr, and ship-issue.
license: Proprietary
compatibility: Requires gh, jq, git. Scripts are bash 3.2 compatible.
metadata:
  version: "1.1.0"
  owner: Vivi Dynamics
---

# CI safety

Ground rules for touching CI. Each rule exists because breaking it cost hours; the
incidents are in `references/incidents.md` (read that file, do not execute it). The
scripts in `scripts/` enforce most rules mechanically. Where a rule is enforced by a
script, your job is to run the script and read its JSON, not to re-derive the check.

Violating the letter of these rules is violating the spirit of these rules.

**Where the scripts are.** `S` is this skill's `scripts/` directory, next to this
SKILL.md. Set it once per session, for example `S=.agents/skills/ci-safety/scripts`
(or `.claude/skills/ci-safety/scripts` where that is the installed copy), then run every
script as `$S/<script>`.

## §1 Truth is the run for the PR's current head SHA

Only a run whose `head_sha` equals the PR's current head says anything about the PR.
Right after a push, "current" can still be the previous head for a few seconds, so a
watcher started then must be told which head to wait for: `ci-watch --expect-head`.
`$S/ci-runs <pr>` selects those runs for you and ignores everything else. Every
workflow run on that head counts, not only the main PR workflow: green means every one
of them concluded `success` (or `skipped`/`neutral`, a documented skip), every job of
every run is `completed` with that conclusion, and every check run on the head from
another app is `completed` with it too (`pending_checks` lists the ones still open). A
run's status is not enough: a run can read `completed`/`success` while one of its jobs
is still `queued` (nare PR #160), and that head is `pending`. When
`COORDINARE_CI_WORKFLOW` is set it names a workflow that must also have a run at the head;
until one appears the state is `pending` and `missing_workflow` names it. Workflows
listed in `COORDINARE_CI_IGNORE_WORKFLOWS` are advisory by the consumer's documented choice
and left out (`ignored_run_ids`). For each workflow and event only the newest run on
the head counts; an older one (cancelled by a concurrency group, or parked and then
re-run) is in `superseded_run_ids`. Never
use `gh run list --commit` (returns empty) or `gh pr checks` (403s on some tokens and
renders cancelled as fail). An empty result means "not ready", never "green".

## §2 Cancelled is not failure

| conclusion | meaning | action |
| --- | --- | --- |
| `failure` | ran and failed | classify (§3) |
| `cancelled` | superseded, or a dependent job died | not evidence; find the run for the current head |
| `skipped` | path filter | neutral |
| `action_required` | parked waiting for approval; nothing ran | `parked`, never green (§7) |
| `success` with zero jobs | nothing ran | `parked`, never green (§7) |
| `startup_failure` with zero jobs | the workflow could not start | `failed`, listed in `failed_runs` |
| `success` on an old head | green for old code | not green for this PR |

A wall of red that appeared at once is a cancellation cascade. `ci-runs` puts each
job's `conclusion` in `failed_jobs` and sets `all_cancelled: true` when every failed
job was cancelled; read both before reacting.

## §3 Classify before acting: three buckets

`$S/ci-watch` attaches a `bucket_hint` to every failed job:

| bucket_hint | how it was set | your action |
| --- | --- | --- |
| `infra` | log matched a runner or network failure pattern | verified retry (§4) within budget |
| `known_flake` | log matched a line in the consumer's flake signatures file | first time on this PR: verified retry. Second time, same signature, same PR: stop and root-cause |
| `unknown` | neither matched | read `log_tail`; decide `real` or `flake`; say which evidence decided it |

The hint is a hint. You must state the bucket you settled on and the evidence line.
A second occurrence of the same signature on the same PR is never a flake: patterns
have causes.

## §4 Retries are verified or they did not happen

`gh run rerun` exits 0 and does nothing when the run is still in progress. Only
`$S/verified-rerun <run_id> --pr <pr>` may be used to retry. Exit 0 means the
attempt counter incremented; report "retried (attempt N)". Exit 1 means it did not
happen; report exactly that and do not count it against the budget. Exit 4 means a
precondition failed (run in progress, or stale head) and the JSON `reason` says which.

## §5 Check base freshness before any retry

If `main` already contains a fix for the failing lane, no retry can pass:

    git fetch origin "$($S/repo-config COORDINARE_DEFAULT_BRANCH)"
    git log --oneline "HEAD..origin/$($S/repo-config COORDINARE_DEFAULT_BRANCH)" -- <paths of the failing lane>

`$S/repo-config COORDINARE_DEFAULT_BRANCH` prints the consumer's default branch as a bare
string, so it drops straight into a command; a skill never names the branch itself.

If that log is non-empty, rebase first and let the fresh run speak.

## §6 A watcher never exits "unknown"

`$S/ci-watch` retries transient API errors internally, follows the PR if the
head moves mid-watch (`head_changed: true`), and ends only in `green` (exit 0),
`failed` (exit 1), `timed_out` (exit 4) or `parked` (exit 5). If it exits 4 with
`timed_out`, report the last known state and the elapsed time. Exit 5 is §7.
"Unknown" is not a state you may report.

## §7 Parked is not green

A run is parked when it concluded `action_required`, or completed having executed zero
jobs. Nothing ran, so it proves nothing about the code. GitHub does not report a parked
run as a check, so the PR does not look red; it looks like it has no opinion, and every
"is anything failing?" test answers no. `ci-runs` lists such runs in `parked_runs` and
reports `state: parked`; `ci-watch` exits 5; `merge-verified` refuses to merge.

How a head gets parked: a bot (for example a sweep that merges the default branch into
every open PR with `GITHUB_TOKEN`) pushes to the branch. GitHub will not execute a run
triggered by `github-actions[bot]`, so it creates the run and parks it.

How a parked run gets released, in order:

1. **The sweep's own verification step.** A consumer whose sweep parks runs should
   approve them in the same job (`POST /repos/<repo>/actions/runs/<id>/approve`) and
   check the result. `ci-watch` keeps polling a parked head for `--parked-grace-sec`
   (default 600) so that step has time to land; an approved run goes `pending` and the
   watch follows it to its real result.
2. **An author push.** A push by a person (or a token that is not `GITHUB_TOKEN`)
   triggers runs that execute. This is what you do when the grace window passed.

What a worker does on `state: parked`:

- Check whether the sweep that parked it is still running or its verification step
  failed (the consumer's repo-specific notes name the workflow). If it is still
  running, re-run `ci-watch` once.
- Otherwise, on your own PR branch only, release it with a push: rebase per rebase-main
  if the base moved, or `git commit --allow-empty -m "Re-run CI: runs at <sha> parked at action_required"`
  and push. Then watch the new head with `--expect-head`.
- On a PR that is not yours, report `parked` with the `parked_runs` ids and stop.
  Never approve a run on someone else's branch: approving starts their CI.
- Never report parked as green, as passed, or as "no failures".

## Output contract

Any skill acting on CI ends with the `CI_RESULT` JSON from `$S/ci-watch`,
validated by `$S/validate ci_result <file>`. Numbers you did not see in a
script's output do not go in the block.
