---
name: watch-ci-main
description: Use when asked whether the default branch is green, to watch the latest CI run on the default branch or another long-lived branch, retry its flakes safely, or report a real breakage.
license: Proprietary
compatibility: Requires gh, jq, git and a repo.env in the consumer repo. Scripts are bash 3.2 compatible.
metadata:
  version: "1.0.0"
  owner: Vivi Dynamics
  requires: ci-safety
---

# Watch CI on the Default Branch

Rules: the ci-safety skill binds here, especially §2 (only the current tip's run counts)
and §4 (retries are verified or they did not happen). This skill retries and reports.
It never commits, pushes, or opens a branch.

**Where the scripts are.** `S` is this skill's `scripts/` directory, next to this
SKILL.md. Set it once per session, for example `S=.agents/skills/watch-ci-main/scripts`,
then run every script as `$S/<script>`. Let `R=$($S/repo-config COORDINARE_REPO)`.

## Inputs

- Branch, optional. Default: `$($S/repo-config COORDINARE_DEFAULT_BRANCH)`.

## Procedure

1. **Find the tip and its runs.**

       tip=$(git ls-remote origin "refs/heads/<branch>" | cut -f1)
       $S/vgh api "repos/$R/actions/runs?branch=<branch>&head_sha=$tip&per_page=100" \
         --jq '[.workflow_runs[] | {id, name, path, status, conclusion, run_attempt}]'

   An empty list means "not started yet", never "green": poll again. If
   `COORDINARE_CI_WORKFLOW` is set, only runs whose `path` ends in that file count.

2. **Poll to completion.** Every 60 seconds, re-read the tip and its runs. If the tip
   moved, switch to the new tip; the old runs are superseded. Retry transient `gh`
   errors; never report "unknown". Cap: 60 minutes, then report the last known state
   and elapsed time. Use the harness's background or scheduled wait if it has one;
   otherwise a shell loop with `sleep 60` is fine.

3. **All runs `success`:** report green and stop.

4. **Classify each failed job** from its log
   (`$S/vgh run view --job <job_id> --repo "$R" --log-failed | tail -200`), per
   ci-safety §3:
   - `infra`: runner lost, network errors, image pull failures, disk full.
   - `known_flake`: a line from `COORDINARE_FLAKE_SIGNATURES_FILE` matches, first time seen
     for this tip.
   - `real`: assertion mismatches, compile or lint errors, anything deterministic.
   - Anything unclear is `real`. The default branch breaking is worse than a missed
     retry.
   All-cancelled jobs are a cascade (ci-safety §2), not a failure: wait for the tip's
   run instead.

5. **Act.**
   - Only `infra` and first-occurrence `known_flake`, fewer than 2 verified retries for
     this tip: `$S/verified-rerun <run_id>`. Exit 0 means it happened; go to step 2.
     Exit 1 means it did not; say so and do not count it.
   - Any `real`, or retries exhausted: report, do not fix. Name the job, the failing
     test or file and line, the evidence line from the log, and the suspect commits
     (`git log --oneline <previous green sha>..<tip>`).

## Report

    WATCH_MAIN_RESULT
    branch: <branch>  tip: <sha>
    state: green | failed | timed_out
    jobs: <job>: <infra|known_flake|real> because <evidence>   (one per failed job)
    retries_verified: <n>/2
    suspects: <commits or none>

Every value comes from a command you ran, not from memory.
