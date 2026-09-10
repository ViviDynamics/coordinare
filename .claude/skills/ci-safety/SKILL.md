---
name: ci-safety
description: Use when reading CI state, retrying a GitHub Actions run or job, classifying a CI failure as flake vs real, or writing any watcher/poll loop in this repo. Required background for watch-ci, merge-pr, and ship-issue.
user-invocable: false
allowed-tools: Bash(gh *) Bash(git *) Read Grep Glob
effort: medium
---

# CI Safety, how to read, retry and classify CI in ViviDynamics/coordinare

Ground rules for touching CI here. Ported from an in-house project and re-grounded in
coordinare's own incidents. Each rule states its *why* so you do not relearn it.

Violating the letter of these rules is violating the spirit of them. There is no
"this case is different".

## 1. GitHub token, resolve it worktree-safely

Every Bash call is a fresh shell, so the token prefix goes on **every** `gh` command.
Two token files exist and they are not equivalent:

```bash
TOKEN_FILE="$HOME/.gh_token"   # classic PAT: Checks API, Actions API, admin merge
[ -f "$TOKEN_FILE" ] || TOKEN_FILE="$HOME/Workspaces/ViviDynamics/.gh_token"  # fine-grained fallback
GH_TOKEN=$(cat "$TOKEN_FILE") gh ...
```

- Prefer `~/.gh_token` (classic PAT). The org-level fine-grained token 403s on
  the Checks API with "Resource not accessible by personal access token". The classic PAT
  reads `gh pr checks` fine in this repo.
- Use `$HOME`-anchored paths, never `../.gh_token`. Relative paths break inside
  `.claude/worktrees/<name>/`, and the failure looks like an auth outage.
- "Resource not accessible" is a token-scope problem, not a CI state. Retry with the
  classic PAT before concluding anything.
- Neither token can write repository rulesets. `PATCH .../rulesets/<id>` returns 404 even
  for a no-op. Ruleset changes need the GitHub UI.

## 2. Which run is the truth, head SHA always

Only act on the run whose `head_sha` equals the PR's current head. Anything else is
superseded: its conclusion says nothing about the current code, and re-running a
superseded run can steal the concurrency slot and cancel the run for the current head.

```bash
GH_TOKEN=$(cat "$TOKEN_FILE") gh pr view <N> --json headRefOid --jq .headRefOid
GH_TOKEN=$(cat "$TOKEN_FILE") gh api "repos/ViviDynamics/coordinare/actions/runs?head_sha=<sha>" \
  --jq '.workflow_runs[] | {id, name, status, conclusion, run_attempt, head_sha}'
```

Known traps:

- `gh run list --commit <sha>` can return an **empty list even when runs exist**. Filter
  with `--branch` and match `headSha` yourself, or use the `runs?head_sha=` endpoint.
- **An empty poll result means "not ready", never "all complete".** A poll that reads
  empty as done reports success having checked nothing.
- A bot version-bump commit can strand the PR head with **zero checks**. Zero checks is
  not green. The fix is to push a real commit after `git pull --ff-only`.

## 3. Conclusion semantics, cancelled is not failure

| Conclusion | Meaning here | Treat as |
| --- | --- | --- |
| `failure` | the job ran and failed | classify it (section 5) |
| `cancelled` | concurrency superseded it, or a dependent job died | not evidence of anything, find the run for the current head |
| `skipped` | path filter or dependency skip | neutral |
| `success` on a run whose `head_sha` is not the PR head | green for old code | **not green** for this PR |

A wall of red that appeared all at once is usually a cancellation cascade, not N genuine
failures. Read one job's `conclusion` before reacting.

## 4. Required vs advisory checks

`main` requires exactly three contexts: **`Lint`, `Test`, `Build Success`**. Everything
else in `Pull Request CI` (Chart, Daemon Image, Chart Install (kind), Coverage, E2E
Browser Tests, Performer Tests, Docker Images, Performance Benchmarks) is advisory and
does **not** gate the merge.

`main` requires **no approving review**. Do not reach for `--admin` here (that is the in-house scheme's
rule, not this repo's).

A red advisory job is still worth one look, because it can be a real regression. It is
not a reason to hold a merge whose three required checks are green.

## 5. The verified-retry protocol

`gh run rerun` silently no-ops while the run is still in progress: exit code 0, nothing
happens. A retry you did not verify is a retry that did not happen. Never report one.

Preconditions, all required:

1. The run's `status` is `completed`.
2. The run's `head_sha` equals the PR's current head (section 2).
3. Base freshness checked (section 7).
4. The failure is classified retryable (section 6).

```bash
# BEFORE: capture the attempt counter
GH_TOKEN=$(cat "$TOKEN_FILE") gh api repos/ViviDynamics/coordinare/actions/runs/<run_id> \
  --jq '{status, run_attempt, head_sha}'
# ACT (failed jobs only)
GH_TOKEN=$(cat "$TOKEN_FILE") gh run rerun <run_id> --failed
# AFTER: poll until run_attempt has INCREMENTED
GH_TOKEN=$(cat "$TOKEN_FILE") gh api repos/ViviDynamics/coordinare/actions/runs/<run_id> --jq '.run_attempt'
```

Only an incremented `run_attempt` earns the words "retried (attempt N)". If it has not
incremented within about two minutes the retry did not happen. Say exactly that, work out
which precondition you missed, and do not count it against the budget.

## 6. Classify before acting, three buckets

| Bucket | Signatures | Action |
| --- | --- | --- |
| **infra / resource** | `setup-ruby` EACCES on `/opt/hostedtoolcache`; "runner has been lost"; `no space left on device`; `exec format error` (arch roulette, see section 8); registry 403 on the macOS runner's arm64 bake | verified retry (section 5) |
| **known flake** | `E2E Browser Tests` failing in about 9 seconds (playwright install reaching for sudo on a self-hosted runner, real runs take 2 to 4 minutes); `Docker Images` (advisory, flakes) | first occurrence: verified retry. **Second occurrence of the same signature on the same PR: stop retrying and root-cause.** |
| **real** | assertion mismatches, ruff findings, deterministic failures matching the diff | never retry, investigate |

The second-occurrence rule is absolute. A flake heuristic matching the log does not prove
flakiness, it only buys one retry. Twice is a pattern, and patterns have causes.

| Excuse | Reality |
| --- | --- |
| "It matches the flake list, retry again" | The list buys ONE retry. Second hit means root-cause. |
| "It passed locally, must be infra" | Local pass plus repeated CI fail is the classic signature of a race CI's timing exposes. That is a real bug. |
| "One more retry and we're green" | That is what the last three retries were for. Stop. |
| "The retry probably went through" | `run_attempt` incremented or it did not happen. |

## 7. Base freshness, check BEFORE any retry

If `main` already contains the fix for the failing lane, no number of retries can pass.

```bash
git fetch origin main
git rev-list --count HEAD..origin/main
git log --oneline HEAD..origin/main -- <paths of the failing lane and its tests>
```

If the commits behind touch the failing lane (its code, its tests, or `.github/workflows/`),
rebase first with `/rebase-main`, then let the fresh run speak.

## 8. Runner arch roulette

`[self-hosted, linux]` matches both amd64 pods and arm64 boxes, so an unpinned job gets a
random architecture. This broke two of three coordinare main builds (`exec format error`,
publish jobs skipped, `latest` silently stale). If a failure smells architectural, read
the runner labels before blaming the code.

`Main Branch Build` never runs on PRs. After every merge, check it:

```bash
GH_TOKEN=$(cat "$TOKEN_FILE") gh run list --branch main --limit 5
```

## 9. Watcher resilience, never exit "unknown"

- A transient `gh` failure (network, 5xx, rate limit) is **not a result**. Retry the poll
  up to five times with increasing waits before calling it an outage.
- Legal terminal states are exactly: a definite CI conclusion, or "wall-clock cap reached,
  last known state was X at HH:MM". "unknown" is not a terminal state.
- Re-resolve the PR head SHA every iteration. A push during the watch supersedes the run
  you were watching, and you must switch to the new run.

## 10. Local shell traps

- Shell state does not persist between tool calls. An `export` in one Bash call is gone in
  the next. Carry env inline.
- Run pytest as `.venv/bin/pytest` and ruff as `.venv/bin/ruff check`, never
  `python -m pytest` (pyenv shim issues). The coordinare and performer test trees are run
  separately. Never run the coordinare tree with `-o addopts=""`.
- Never pipe a pre-commit `ruff` through `tail`, it masks the exit code.
- `git checkout main` fails inside a linked worktree. Use `git fetch origin main` and
  reference `origin/main`.

## Output contract for skills that build on this

```
CI_RESULT
run_id: <id>  head_sha: <sha>  attempt: <n>
state: green | failed | watching-timed-out
failures: <job>: <infra|known-flake|real>: <one-line evidence>  (or none)
required_checks: Lint=<c> Test=<c> Build Success=<c>
retries_verified: <n>   retries_attempted_unverified: <n, must be 0>
```
