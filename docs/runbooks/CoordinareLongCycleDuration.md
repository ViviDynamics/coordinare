# Runbook: CoordinareLongCycleDuration

## Trigger Condition

Alert fires when `histogram_quantile(0.95, rate(coordinare_cycle_duration_seconds_bucket[10m])) > 30` — the p95 cycle duration exceeds 30 seconds over a 10-minute window.

## Operational Impact

Cards are being processed, but slowly. Operators will experience delayed PR creation, delayed merge decisions, and increased time-to-done for work items. If the cycle duration approaches `poll_interval_seconds`, cycles may start overlapping.

## Diagnostic Steps

1. Check `coordinare_agent_dispatch_seconds` and `coordinare_board_poll_seconds` in `/metrics` to identify which subsystem is the bottleneck — long board polls suggest GitHub API latency; long agent dispatch suggests SSH or agent startup latency.
2. Inspect daemon logs for `event: "runtime_event"` entries with `phase: "monitoring_agent"` — check if the agent is taking unusually long to respond by looking at elapsed time between dispatch and monitor events for the same card.
3. Check the current load on the agent host and GitHub API: run `curl -s "https://api.github.com/rate_limit" -H "Authorization: Bearer $COORDINARE_GITHUB_TOKEN"` to verify remaining API quota. A near-exhausted quota causes retry backoff that inflates cycle duration.

## Resolution Actions

1. If GitHub API is rate-limited, increase `poll_interval_seconds` in `config.yaml` temporarily (e.g., from 30 to 60) to reduce request frequency and allow the rate limit to recover. Redeploy the daemon with the updated config.

## Escalation Path

Contact the platform team (Slack: `#platform-oncall`). Provide: (1) the p50/p95/p99 cycle duration values from the Grafana dashboard, (2) the `coordinare_board_poll_seconds` and `coordinare_agent_dispatch_seconds` gauge values, (3) GitHub rate limit remaining. Expected response time: 1 hour during business hours.
