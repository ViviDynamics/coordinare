# Quickstart: External Service Resilience

**Branch**: `005-resilience` | Assumes spec 003 (state persistence) and spec 004 (agent protocol) are already implemented.

---

## 1. Install the New Dependency

```bash
# Add stamina to pyproject.toml and install
uv add "stamina>=24.2.0"
# Or:
pip install "stamina>=24.2.0"
```

Verify installation:
```bash
python -c "import stamina; print(stamina.__version__)"
```

---

## 2. Configuration

All retry and circuit-breaker parameters are configured under the `resilience:` key in your `coordinare.yaml`. All fields have production-ready defaults — no configuration is required on first deployment.

```yaml
# coordinare.yaml — resilience defaults (shown for reference)
resilience:
  github_retry:
    attempts: 4
    wait_initial_seconds: 2.0
    wait_max_seconds: 30.0
    wait_jitter_seconds: 2.0

  github_circuit:
    failure_threshold: 3
    recovery_window_seconds: 120.0
    observation_window_seconds: 300.0

  # Repeat for: slack_retry, slack_circuit, smtp_retry, smtp_circuit,
  #             anthropic_retry, anthropic_circuit, agent_retry, agent_circuit
```

**Key constraint**: `wait_max_seconds` for each service should not exceed `poll_interval_seconds` (default 30s) to prevent a single retry from delaying the entire poll cycle.

---

## 3. Observe Retry Behavior

### Trigger a transient GitHub failure

Using a proxy (e.g., `mitmproxy`) or by temporarily revoking the GitHub token:

```bash
# Watch structured retry events in real time
coordinare run 2>&1 | jq 'select(.event | startswith("service.retry"))'
```

Expected log events on retry:
```json
{
  "event": "service.retry_attempt",
  "service": "github",
  "action": "poll_board",
  "attempt": 2,
  "error": "TransientGitHubError: server error 503",
  "wait_seconds": 4.3,
  "level": "warning"
}
```

### Trigger a circuit breaker

Run coordinare with GitHub returning 503 on every call. After `failure_threshold` exhausted retry budgets, observe:

```json
{
  "event": "circuit_breaker.state_changed",
  "service": "github",
  "previous_state": "closed",
  "new_state": "open",
  "reason": "failure_threshold_exceeded",
  "level": "warning"
}
```

Once the circuit is open, subsequent poll cycles emit:
```json
{
  "event": "service.call_skipped",
  "service": "github",
  "action": "poll_board",
  "reason": "circuit_open",
  "level": "warning"
}
```

---

## 4. Check Circuit Breaker States via Health Endpoint

```bash
curl -s http://localhost:8080/health | jq '.circuit_breakers'
```

**Healthy response** (all closed):
```json
{
  "circuit_breakers": {
    "github":    { "state": "closed", "opened_at": null, "failure_count": 0 },
    "slack":     { "state": "closed", "opened_at": null, "failure_count": 0 },
    "smtp":      { "state": "closed", "opened_at": null, "failure_count": 0 },
    "anthropic": { "state": "closed", "opened_at": null, "failure_count": 0 },
    "agent":     { "state": "closed", "opened_at": null, "failure_count": 0 }
  }
}
```

**Degraded response** (GitHub open):
```json
{
  "status": "degraded",
  "circuit_breakers": {
    "github": { "state": "open", "opened_at": "2026-02-22T03:14:00Z", "failure_count": 3 }
  }
}
```

---

## 5. Prometheus Metrics

```bash
curl -s http://localhost:8080/metrics | grep circuit_breaker
# coordinare_circuit_breaker_state{service="github",state="open"} 1.0
# coordinare_circuit_breaker_state{service="github",state="closed"} 0.0

curl -s http://localhost:8080/metrics | grep service_retries
# coordinare_service_retries_total{action="poll_board",service="github"} 12.0
```

**Prometheus alert rule** for open circuit:
```yaml
- alert: CoordinareCircuitOpen
  expr: coordinare_circuit_breaker_state{state="open"} == 1
  for: 2m
  labels:
    severity: warning
  annotations:
    summary: "Coordinare {{ $labels.service }} circuit is open"
```

---

## 6. Test Isolation with `stamina.set_active(False)`

In `tests/conftest.py`:
```python
import stamina

@pytest.fixture(autouse=True)
def disable_stamina_retries():
    stamina.set_active(False)
    yield
    stamina.set_active(True)
```

This disables all retry sleeps globally in tests. Circuit breaker tests that need to trigger state transitions should call `cb.record_failure()` directly rather than relying on stamina retries.

---

## 7. Running Tests

```bash
cd src
# Unit tests (fast, no retries)
pytest tests/unit/test_circuit_breaker.py -v
pytest tests/unit/services/test_resilient_github.py -v

# Integration tests (mock services)
pytest tests/integration/test_resilience_integration.py -v

# Performance test (poll cycle budget)
pytest tests/perf/test_poll_cycle_budget.py -v -s
```

---

## 8. Verifying the Notification No-Block Guarantee

Configure Slack to a dead endpoint in test config:
```yaml
slack_webhook_url: "http://localhost:9999/dead"
```

Trigger a card transition that sends a notification. Verify:
1. Card transitions as expected (not blocked).
2. Structured failure event appears in logs.
3. `coordinare_service_calls_total{service="slack",outcome="failure"}` increments.
4. No `RuntimeExecutionError` is raised.
