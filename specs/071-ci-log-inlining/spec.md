# 071 — CI Log Inlining

> Stacked on top of `069-blocked-notification-rehydration`.

## Summary

Inline GitHub Actions failure logs directly into both the performer-side
backend relay AND the coordinare-side PR-checks bounce body, so the
implementer always has the concrete failure text in front of it instead
of only the check name + a `gh run view --log-failed` command it may or
may not execute.

## Motivation

Live observation: card #70 looped for hours on the same CI failure. The
performer's `_poll_check_runs` already fetches `output.text` from the
Check Run, but many GitHub Actions (notably the ones we use) do not
populate `output.text`, so the relay payload was just `### lint\n`.
The persona directive (T054) told the model to run
`performer-fetch-ci-log` on its own; in practice, several backends
skipped that step and re-pushed the same broken code.

Symmetrically, the coordinare's spec-064 PR-checks gate BOUNCE body
only carried check names + the `gh` commands needed to fetch logs.
When the bounce re-dispatched the implementer, the relay context
contained no log text — same guess-and-loop failure mode.

## Core Principle

> The failing log is delivered automatically, not requested. Performer
> and coordinare MUST fetch and inline the actual failure text whenever
> CI fails, so the implementer never relies on a separate tool call to
> see the error.

## Success Criteria

- **Content budget**: A single relay or bounce body adds ≤ 6000 chars
  of inlined log content (configurable). Verified by unit tests
  asserting body length and per-check slicing.
- **Latency budget**: Log fetches inherit the existing `httpx` default
  timeout (5s connect / 30s read per request); a turn with N failed
  checks adds at most N sequential fetches. No new global timeout is
  introduced. Acceptable because failed-CI turns are rare (worst case
  a few per card) and the alternative is the persona issuing a
  separate `performer-fetch-ci-log` call with the same latency.
- **Cache strategy**: None. Log tails are re-fetched per turn — each
  turn is rare and idempotent; caching across turns risks stale logs
  after a re-run.

## Functional Requirements

- **FR-001 (performer)**: When `_poll_check_runs` reports failure, for
  each failed Check Run whose `output.text` is empty or shorter than
  `CI_LOG_INLINE_MIN_OUTPUT_CHARS` (default 200), the performer MUST
  call `get_check_run_logs` and append the log tail to the
  `relay_feedback` body delivered to the backend.

- **FR-002 (performer)**: Total inlined log content across all failing
  checks per relay MUST be capped at `CI_LOG_INLINE_MAX_CHARS`
  (default 6000). Per-check budgets are equal slices of the remaining
  cap with a floor of 800 chars, so a small first failure cannot
  consume the whole budget and a large last failure is never starved
  below 800 chars. "Tail" means the last `max_chars` of the log;
  earlier bytes are dropped.

- **FR-003 (performer)**: Log fetch failures (network, archived run,
  insufficient scope) MUST degrade gracefully to the current
  name+title+text behavior. The relay never aborts on a log-fetch
  error.

- **FR-004 (coordinare)**: In `_handle_pr_checks_gate`, when the
  decision is BOUNCE with `decision.reason != "pending_timeout"`, the
  coordinare MUST fetch the failing job log via the GitHub REST
  endpoint `/repos/{owner}/{repo}/actions/jobs/{job_id}/logs` for
  each failed check and inline the tail into
  `relay_feedback[].body` before re-dispatching the implementer.

- **FR-005 (coordinare)**: Total inlined log content per bounce body
  MUST be capped at `notifications.pr_checks_bounce_log_max_chars`
  (default 6000). Setting the cap to `0` disables inlining entirely.
  Fallback is per-check: a single failed log fetch demotes only that
  check to name-only, while other checks' inlined tails are retained.
  Bounce never blocks on a log fetch.

- **FR-006 (no-CI repos)**: When no Check Runs exist (verdict ==
  "pass" on performer; empty rollup on coordinare), both paths MUST
  short-circuit unchanged — no log fetches, no extra latency.

## Out of scope

- Replacing the `performer-fetch-ci-log` CLI shim. It stays as an
  on-demand follow-up tool; FR-001/FR-004 just make the *first*
  relay sufficient for the common case.
- Rewriting `_handle_pr_checks_gate`'s decision logic; only the
  BOUNCE body is enriched.
- Authorization changes: log fetches use the same tokens the existing
  paths already use.
