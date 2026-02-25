# Quickstart: Metrics & Observability (009)

**Branch**: `009-metrics-observability` | **Date**: 2026-02-25

---

## For Operators

### Query metrics

```bash
# All Prometheus metrics (text format)
curl http://localhost:8080/metrics

# Filter to just cycle metrics
curl -s http://localhost:8080/metrics | grep cycles_completed

# Check liveness (is the daemon process alive?)
curl http://localhost:8080/live

# Check readiness (are all required subsystems operational?)
curl http://localhost:8080/ready | python3 -m json.tool
```

### Interpret readiness response

```json
{
  "status": "degraded",
  "subsystems": [
    {"name": "github", "status": "degraded", "required": true, "details": "Stale: last poll 95s ago"},
    {"name": "agent_ssh", "status": "healthy", "required": true},
    {"name": "notifications", "status": "degraded", "required": false}
  ]
}
```

- `status: "ready"` + HTTP 200 → daemon is fully operational
- `status: "degraded"` + HTTP 503 → at least one *required* subsystem is unavailable
- Optional subsystems (configured in `config.yaml`) degrade silently

### Configure optional subsystems and health check timeout

```yaml
# config.yaml
health_check_timeout_seconds: 5    # default: 2 — stale threshold in seconds
optional_subsystems:               # these won't block readiness
  - notifications
  - agent_ssh
```

### Import the Grafana dashboard

1. Open Grafana → Dashboards → Import
2. Upload `docs/dashboards/coordinare.json`
3. Select your Prometheus data source
4. Click Import

All panels display immediately. No further configuration required.

---

## For Developers

### Module layout (new and modified files)

```
src/coordinare/
├── observability.py         # NEW — CycleContext, HealthRegistry, bind/clear cycle_id
├── metrics.py               # MODIFIED — new metrics added (cycles, transitions, build_info, etc.)
├── health.py                # MODIFIED — /live added, /ready enhanced with subsystem list
├── daemon.py                # MODIFIED — bind/clear cycle_id, HealthRegistry updates, metric recording
└── config.py                # MODIFIED — health_check_timeout_seconds, optional_subsystems

tests/unit/
├── test_cycle_correlation.py    # NEW — cycle_id binding, clearing, non-leak tests
├── test_metrics_coverage.py     # NEW — metric increment and label tests
├── test_health_endpoints.py     # NEW — /live, /ready endpoint contract tests
└── test_runbook_coverage.py     # NEW — 1-alert-1-runbook invariant test

docs/
├── dashboards/
│   └── coordinare.json           # NEW — Grafana dashboard definition
└── runbooks/
    ├── _template.md             # NEW — runbook template
    ├── CoordinareHighCycleFailureRate.md
    ├── CoordinareLongCycleDuration.md
    ├── CoordinareNotificationFailures.md
    └── CoordinareCircuitBreakerOpen.md
```

### Running tests

```bash
cd src
# Cycle correlation
pytest ../tests/unit/test_cycle_correlation.py -v

# Metrics coverage
pytest ../tests/unit/test_metrics_coverage.py -v

# Health endpoints
pytest ../tests/unit/test_health_endpoints.py -v

# 1-alert-1-runbook invariant
pytest ../tests/unit/test_runbook_coverage.py -v

# All existing tests must still pass
pytest ../tests/unit/ -v
```

### How cycle_id correlation works

```
1. daemon.py: CoordinareDaemon.start() begins a cycle
        ↓
2. observability.py: bind_cycle_id(str(uuid4()))
   → structlog.contextvars.bind_contextvars(cycle_id=<uuid>)
        ↓
3. All log entries emitted by structlog during graph.ainvoke()
   automatically include cycle_id (merge_contextvars processor handles it)
        ↓
4. After graph.ainvoke() completes:
   observability.py: clear_cycle_id()
   → structlog.contextvars.clear_contextvars()
        ↓
5. Subsequent heartbeat, sleep, and startup log entries have no cycle_id
```

### How HealthRegistry works

```python
# At startup (in __main__.py):
HEALTH = HealthRegistry()
HEALTH.register("github", required=True)
HEALTH.register("agent_ssh", required=True)
HEALTH.register("notifications", required=False)

# During poll cycle (in daemon.py, after board poll):
HEALTH.update("github", HealthStatus.HEALTHY)

# On /ready request (in health.py):
report = HEALTH.snapshot()  # Reads from cache — non-blocking
```

If a subsystem's `checked_at` is older than `config.health_check_timeout_seconds`,
`snapshot()` automatically overrides its status to `degraded` (stale state).

### How to add a new metric

1. Add the metric object to `CoordinareMetrics` in `src/coordinare/metrics.py`:
```python
self.my_new_counter = Counter(
    "my_new_counter_total",
    "What this counter measures",
    ["label_key"],
    registry=self._registry,
)
```

2. Record it at the appropriate call site (daemon.py, a node, or a service).

3. Add zero-value initialization in `CoordinareMetrics.__init__()` for all expected label values.

4. Add a panel to `docs/dashboards/coordinare.json` to visualize it.

### How to add a new alert rule and runbook

1. Add the alert rule to `docs/dashboards/coordinare.json` in the appropriate panel's `alert` block. Include a `runbookURL` annotation:
```json
"annotations": {
  "runbookURL": "docs/runbooks/MyNewAlert.md"
}
```

2. Create `docs/runbooks/MyNewAlert.md` using the template at `docs/runbooks/_template.md`.

3. Run `pytest tests/unit/test_runbook_coverage.py` — it verifies the file exists.

### Alert rules defined in this spec

| Alert Name | Condition | Severity | Runbook |
|---|---|---|---|
| `CoordinareHighCycleFailureRate` | Error rate > 10% over 5m | warning | `docs/runbooks/CoordinareHighCycleFailureRate.md` |
| `CoordinareLongCycleDuration` | p95 cycle > 30s over 10m | warning | `docs/runbooks/CoordinareLongCycleDuration.md` |
| `CoordinareNotificationFailures` | notifications_failed_total > 0 | warning | `docs/runbooks/CoordinareNotificationFailures.md` |
| `CoordinareCircuitBreakerOpen` | circuit_breaker_state > 0 | critical | `docs/runbooks/CoordinareCircuitBreakerOpen.md` |

### Cross-spec metric ownership

| Metric prefix | Emitter | Notes |
|---|---|---|
| `cycles_*`, `cards_*`, `cycle_duration_*`, `daemon_up`, `config_load_*`, `coordinare_build_info` | spec 009 (daemon.py) | Registered and emitted by this spec |
| `notifications_*` | spec 006 (NotificationService) | Names and labels defined here; spec 006 writes values |
| `circuit_breaker_*` | spec 005 (CircuitBreaker) | Names and labels defined here; spec 005 writes values |
