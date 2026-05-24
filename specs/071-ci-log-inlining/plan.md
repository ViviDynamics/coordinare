# 071 Plan — CI Log Inlining

> **Terminology**: A *Check Run* (GitHub API term) is the unit
> summarized by `summarise_check_runs`. Logs are fetched per
> *job_id*, resolved from the Check Run's `details_url`
> (`/runs/{run_id}/job/{job_id}`). One Check Run ↔ one job_id in
> non-matrix workflows; matrix builds produce one Check Run per
> matrix cell, each with its own job_id.

## Performer side (FR-001 – FR-003)

Edit `agent/performer/src/performer/main.py` `_poll_check_runs` /
`_format_check_failures`:

- After `summarise_check_runs` returns `verdict == "fail"`, iterate
  failing runs. For each run whose existing `output.text` slice is
  empty or `< CI_LOG_INLINE_MIN_OUTPUT_CHARS`, call
  `get_check_run_logs(owner, repo, run["id"], token, max_chars=slice)`.
- Compute the per-check slice as
  `max(800, remaining_budget // (failed - i))` where
  `remaining_budget` starts at `CI_LOG_INLINE_MAX_CHARS`. Equal
  fair-share with a floor so the first failure does not consume the
  whole budget.
- Append the fetched log under a `**Log tail (job <id>)**:` header in
  the same Markdown block produced by `_format_check_failures`.
- Settings additions in `agent/performer/src/performer/config.py`:
  `CI_LOG_INLINE_MIN_OUTPUT_CHARS: int = 200`,
  `CI_LOG_INLINE_MAX_CHARS: int = 6000`. Env-overridable.

## Coordinare side (FR-004 – FR-005)

Edit `src/coordinare/graph/nodes/monitor_performer.py` BOUNCE branch
(lines 667-713):

- Add module-level helper `_parse_job_id_from_details_url(url)` —
  GitHub Actions check details URLs look like
  `https://github.com/{owner}/{repo}/actions/runs/{run_id}/job/{job_id}`.
  We need `job_id` because the logs endpoint is per-job.
- Add `GitHubService.fetch_failed_job_log(owner, repo, job_id,
  max_chars)` in `src/coordinare/services/github.py` that hits
  `/repos/{owner}/{repo}/actions/jobs/{job_id}/logs`, follows the 302
  to S3 *without* the Authorization header, returns the tail
  truncated to `max_chars`, never raises.
- In the BOUNCE branch, when `decision.reason != "pending_timeout"`,
  resolve each failed check name to its job_id via the existing
  `url_by_name` map. Compute per-check slice the same way as the
  performer side and inline the log tail into the bounce body.
- Config addition in `src/coordinare/config.py`
  `NotificationsConfig`:
  `pr_checks_bounce_log_max_chars: int = Field(default=6000, ge=0)`.
  `0` disables inlining (back to current name-only body).

## Tests

- `agent/performer/tests/unit/test_main.py` (or new file): mock
  `get_check_runs` to return a failing run with empty `output.text`,
  mock `get_check_run_logs` to return synthetic log text, assert the
  relay body contains the log tail. Add: fetch raises → graceful
  fallback to existing body.
- `tests/unit/graph/nodes/test_monitor_performer.py`: stub the new
  service method, assert bounce body contains the log tail; assert
  fetch-failure path falls back to the current body.
- Both: zero-CI / empty-rollup short-circuit (no fetch calls made).

## Backwards compatibility

- All new settings default to non-zero values that match the existing
  behavior plus the new log inlining. Operators that want the old
  behavior set both caps to `0`.
- No persistence schema change.
