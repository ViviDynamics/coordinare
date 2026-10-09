# Runbook: CoordinareCircuitBreakerOpen

## Trigger Condition

Alert fires when `coordinare_circuit_breaker_state{service_name=...} > 0` — a circuit breaker has transitioned to half-open (1) or open (2) for any service (github, agent_ssh, slack, smtp).

## Operational Impact

Coordinare is isolating a degraded service to prevent cascading failures. Depending on which service is isolated: a GitHub circuit open means no board polling (cards stall); an agent circuit open means no new card dispatches; a notification circuit open means silent failure for alerts.

## Diagnostic Steps

1. Identify the affected service by checking `coordinare_circuit_breaker_state` in `/metrics` — the `service_name` label with value > 0 is the isolated service. Also check `coordinare_circuit_breaker_trips_total` to see how many trips have occurred.
2. For GitHub (`service_name="github"`): test connectivity manually with `curl -s "https://api.github.com/octocat" -H "Authorization: Bearer $COORDINARE_GITHUB_TOKEN"` — a 401 or 5xx confirms the issue. Check https://www.githubstatus.com/ for ongoing incidents.
3. Review the daemon logs for `circuit_breaker.state_changed` events — the `reason` field identifies the specific error that triggered the trip (e.g., `"ConnectionError"`, `"TimeoutError"`, `"HTTP 503"`).

## Resolution Actions

1. The circuit breaker will auto-recover after `recovery_window_seconds` (default: 60s) by transitioning to half-open and allowing a probe request. If the probe succeeds, the circuit closes automatically — no manual intervention required. To force immediate recovery, restart the daemon (circuit breaker state is in-memory only and resets on restart).

## Escalation Path

Contact the platform team (Slack: `#platform-oncall`) if the circuit breaker does not close within 10 minutes of the affected service recovering. Provide: (1) the `service_name` from the alert, (2) `coordinare_circuit_breaker_trips_total` value, (3) the `circuit_breaker.state_changed` log entries with `reason` field, (4) any relevant external service status page. Expected response time: 15 minutes during business hours, 1 hour off-hours.
