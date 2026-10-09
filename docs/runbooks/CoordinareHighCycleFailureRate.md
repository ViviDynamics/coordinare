# Runbook: CoordinareHighCycleFailureRate

## Trigger Condition

Alert fires when `rate(coordinare_errors_total[5m]) / rate(coordinare_cycles_completed_total[5m]) > 0.1` — the cycle failure rate exceeds 10% over a 5-minute window.

## Operational Impact

Cards on the project board are not being processed. Work items that were dispatched to the AI agent will not progress to PR review, blocking the development pipeline.

## Diagnostic Steps

1. Check the structured daemon logs for `event: "runtime_event"` with `category: "failure"` entries. Look for the `error` field to identify the exception type and the `failing_step` field (typically `"cycle_execution"`).
2. Run `coordinare config validate` to confirm the configuration is still valid — a config regression after a deploy can cause repeated cycle failures.
3. Check the GitHub API rate limit headers by inspecting recent `github_service` calls in the logs (look for `errors_total{category="github"}` in `/metrics`). If the rate limit is exhausted, cycles will fail until it resets.

## Resolution Actions

1. If the failure is a transient exception (network timeout, GitHub 5xx), the daemon auto-recovers on the next cycle. Monitor `/metrics` for `cycles_completed_total` to resume incrementing. If failures persist for > 5 minutes, restart the daemon process and re-run `coordinare config validate`.

## Escalation Path

Contact the platform team (Slack: `#platform-oncall`). Provide: (1) the last 50 lines of structured daemon logs, (2) the output of `curl localhost:8080/metrics | grep cycles`, (3) the output of `coordinare config validate`. Expected response time: 30 minutes during business hours, 2 hours off-hours.
