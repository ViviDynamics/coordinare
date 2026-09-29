---
name: watch-ci
description: Use when a pull request has CI running or red and you need to wait for it, retry safely, or find out why it failed. Also use when asked "is CI green" or "watch the build".
license: Proprietary
compatibility: Requires gh, jq, git and a repo.env in the consumer repo (see ADOPTING.md). Scripts are bash 3.2 compatible.
metadata:
  version: "1.0.0"
  owner: Vivi Dynamics
  requires: ci-safety
---

# Watch CI

Wait for a PR's CI, retry only what may be retried, and hand back a verified result.
Rules: the ci-safety skill (read it once per session). Every mechanical step below is a
script in this skill's `scripts/` directory; run it and read its JSON. Do not replace a
script with your own `gh` command.

**Where the scripts are.** `S` is this skill's `scripts/` directory, next to this
SKILL.md. Set it once per session, for example `S=.agents/skills/watch-ci/scripts`
(or `.claude/skills/watch-ci/scripts` where that is the installed copy), then run every
script as `$S/<script>`.

## Inputs

- PR number. If not given, detect it: `$S/vgh pr view --json number --jq .number`.
- Issue number for state, if known; otherwise use the PR number.

## Budget

| resource | budget |
| --- | --- |
| verified retries per PR | 2 |
| same flake signature on this PR | 1 retry, then root-cause |
| wall clock per watch | 60 minutes |

Read the budget already spent before acting: `$S/state get <issue>`.

## Procedure

1. Record the phase: `$S/state set <issue> phase watching`.

2. Watch: `$S/ci-watch <pr> --expect-head "$(git rev-parse HEAD)" > .agents/state/<issue>-ci.json`.
   Pass `--expect-head` whenever you just pushed: GitHub can report the previous
   head for a few seconds, and its run may already be green, so without it the
   watch can end green on poll 1 for code that was never tested. This blocks until a
   terminal state. Read the exit code and the JSON in that file.
   - exit 0, `state: green`: go to step 6.
   - exit 4, `state: timed_out`: report the last state and elapsed time (ci-safety §6),
     go to step 6.
   - exit 1, `state: failed`: continue.
   - any other exit: the JSON has an `error`; report it and stop.

3. Save what you saw, mechanically; do not retype values from the JSON:

   ```sh
   $S/state set <issue> head_sha "$(jq -r .head_sha .agents/state/<issue>-ci.json)"
   $S/state set <issue> failures "$(jq -c .failures .agents/state/<issue>-ci.json)"
   ```

4. Classify every entry in `failures` (ci-safety §3). For each job write one line:
   `<job>: <bucket> because <evidence line from log_tail or signature_match>`.
   - `infra` hint: bucket is `infra`.
   - `known_flake` hint: check whether the signature was already seen on this PR:
     `$S/state get <issue> seen_signatures | jq -e --arg s "<signature_match>"
     'index($s) != null'`. Exit 0 means already seen; bucket is `real` (second
     occurrence). A missing key prints nothing, and `jq -e` on empty input exits
     nonzero, which correctly reads as "not seen": bucket is `known_flake` and you run
     `$S/state append <issue> seen_signatures "<signature_match>"`.
   - `unknown` hint: if `log_error` is true, do not classify; report
     `<job>: log unavailable` and treat it as `unknown` for the retry decision.
     Otherwise read `log_tail`: assertion mismatches, compile, vet or lint errors, and
     failures in code the PR touches are `real`. Anything else stays `unknown`, and
     `unknown` is treated as `real` for the retry decision.

5. Act:
   - Any job is `real` or `unknown`: do not retry. Go to step 7.
   - All jobs are `infra` or first-occurrence `known_flake`, and retries spent < 2:
     first check base freshness (ci-safety §5). If `main` touches the failing lane,
     report "rebase needed" and go to step 6. Otherwise run
     `$S/verified-rerun <run_id> --pr <pr>` for each distinct `run_id` in
     `failures`.
       - exit 0: `$S/state incr <issue> retries`, then return to step 2.
       - exit 1: report "retry did not happen" with the JSON `reason`; do not count it;
         return to step 2 once (the run may have been mid-transition). If it happens a
         second time, go to step 6 and report `retries_unverified` as 0 and the reason
         in your classification lines.
       - exit 4: report the `reason`; return to step 2.
   - Retries spent = 2: stop retrying. Go to step 7.

6. Finish: take the CI_RESULT JSON from the last `$S/ci-watch` run (the file saved
   in step 2), set its `retries_verified` field to the integer from state, and validate
   the result:

   ```sh
   n=$($S/state get <issue> retries); n=${n:-0}
   jq --argjson n "$n" \
     '.retries_verified=$n' .agents/state/<issue>-ci.json > .agents/state/<issue>-final.json
   $S/validate ci_result .agents/state/<issue>-final.json
   ```

   `state get` prints nothing (not the literal `0`) when the key was never set, so
   check for emptiness in the shell before handing the value to `jq --argjson`; an
   empty string there is invalid JSON and jq will reject it.

   This must print `{"valid":true}`. Then `$S/state set <issue> phase done` and
   print the validated JSON as the final `CI_RESULT` block.

7. Investigate a real failure (a judgment step):
   1. Name the failing test, file and line from `log_tail`.
   2. Read that file and the PR's diff to it.
   3. Reproduce locally using the command for that area from the file named by
      `COORDINARE_TEST_COMMANDS_FILE` in `repo.env`. If it passes locally and failed in CI
      twice, that is a timing bug in the code, not a flake.
   4. Propose or apply the minimal fix. Never disable or delete a test to get green.
   5. If the fix needs a decision outside the PR's scope, stop and report.
   Then go to step 6 with `state: failed` and your classification lines.

## Report

End with the classification lines from step 4, then the validated `CI_RESULT` block.
Every number in the block came from a script. If you retried, the block's
`retries_verified` equals the number of `verified-rerun` exit-0 results you saw.

## Red flags

- "I'll run `gh run rerun` directly, it is faster": you will report a retry that did
  not happen. Use `verified-rerun`.
- "The flake list matched, retry again": the list buys one retry per signature per PR.
- "It is probably still running, I'll say unknown": the watcher's `timed_out` is the
  state; report it with the elapsed time.
- "CI is red so the PR is broken": read `conclusion` first (ci-safety §2).
