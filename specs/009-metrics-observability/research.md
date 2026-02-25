# Research: Metrics & Observability (009)

**Branch**: `009-metrics-observability` | **Date**: 2026-02-25 | **Spec**: [spec.md](spec.md)

---

## R1: Cycle ID Propagation Mechanism

**Decision**: Use `structlog.contextvars.bind_contextvars(cycle_id=...)` at cycle start; `structlog.contextvars.clear_contextvars()` at cycle end.

**Rationale**: `structlog` is already configured with `structlog.contextvars.merge_contextvars` as the first processor in its chain (confirmed in `src/coordinare/__init__.py`). This means any key bound via `bind_contextvars()` is automatically included on every subsequent log call in the same async task — zero changes to individual log call sites. The `clear_contextvars()` at cycle end prevents bleed-over into the inter-cycle sleep or heartbeat log entries, satisfying the US2 acceptance scenario that non-cycle entries must NOT carry `cycle_id`.

**Alternatives considered**:
- `contextvars.ContextVar` directly: Works but requires passing the variable explicitly to every site or monkey-patching structlog — more intrusive.
- Adding `cycle_id` to `CoordinareState`: Would appear in log entries only where `CoordinareState` is explicitly logged, not at all log sites; would not propagate into node functions without threading state through every call.
- UUID4 for uniqueness: Standard library, no dependency, session-scoped uniqueness guaranteed. Short UUID (first 8 hex chars) is visually readable if desired, but full UUID4 is safer for cross-session debugging.

**Integration point**: `CoordinareDaemon.start()` in `src/coordinare/daemon.py` — wrap each `graph.ainvoke()` call with bind/clear.

---

## R2: Metrics Registry Extension Strategy

**Decision**: Extend the existing `CoordinareMetrics` class in `src/coordinare/metrics.py` with new counters, gauges, and histograms. Do not create a separate registry.

**Rationale**: A single `CollectorRegistry` singleton (`METRICS = CoordinareMetrics()`) is already established in the codebase. Adding new metrics to the existing class avoids duplicate registrations, maintains the existing render path (`METRICS.render()` → `/metrics` endpoint), and is the lowest-risk approach. The existing 9 metrics (cards_processed_total, notifications_total, errors_total, card_cycle_seconds, agent_dispatch_seconds, board_poll_seconds, up) remain unchanged; new metrics are additive.

**New metrics to add**:
- `cycles_completed_total` (Counter) — replaces the integer counter in daemon.py
- `card_state_transitions_total` (Counter, labels: transition_type) — replaces the phase label from existing cards_processed
- `cycle_duration_seconds` (Histogram, standard buckets) — wraps the full poll cycle
- `config_load_duration_seconds` (Histogram) — measured at startup
- `coordinare_build_info` (Info gauge, labels: version, started_at, python_version)
- `daemon_up` (Gauge, 1=running) — already exists as `up`; renamed for clarity per FR-001
- Deferred: circuit_breaker_state, circuit_breaker_trips_total — owned by spec 005
- Deferred: notification_* counters — owned by spec 006

**Alternatives considered**:
- Separate metrics module per subsystem: Clean separation but requires coordinating multiple registries or a shared registry passed by dependency injection — overkill for the current scale.
- Remove existing metrics and rebuild: Risk of regressions in existing health tests; no benefit.

---

## R3: Health Probe Architecture

**Decision**: Expose cached health probe results via a `HealthRegistry` singleton. During each poll cycle, the daemon updates the registry with the latest subsystem statuses. The `/ready` endpoint reads the registry synchronously — no live probes on request path.

**Rationale**: Live connectivity probes (e.g., a GitHub API call on every `/ready` request) would couple the health endpoint's latency to external API latency, violating SC-003 (100ms liveness, bounded readiness). The existing health.py already infers GitHub connectivity from `last_poll_at` recency — the cached-state pattern is already implicit. Making it explicit with a typed `HealthRegistry` improves testability and lets the daemon control when probes run.

**Timeout contract**: Each subsystem's cached probe has a `checked_at` timestamp. If the cache entry is older than `health_check_timeout_seconds`, the endpoint reports it as `degraded` (stale). This satisfies FR-008 without ever blocking the request.

**Subsystem list**: GitHub API, Agent SSH, Notification (Slack+Email aggregate), Config. Optional subsystems are configurable in `config.yaml` (`optional_subsystems: [agent_ssh, notifications]`).

**New config fields needed**:
- `health_check_timeout_seconds: int = 2`
- `optional_subsystems: list[str] = []`

**Alternatives considered**:
- Live async probes per request with timeout wrapper: Complex, coupling external latency into health path, risk of hanging on half-open TCP connections.
- Spawn background task to update health: Adds complexity; the poll cycle already runs on a cadence — piggyback on that.

---

## R4: `/live` vs `/ready` Endpoint Design

**Decision**: Add `/live` route (always 200, `{"status": "alive"}`); enhance existing `/ready` with per-subsystem `HealthProbe` list; keep `/health` for backward compatibility (deprecated).

**Rationale**: The spec explicitly names `liveness` and `readiness` as separate concepts (FR-006, FR-007). Container orchestrators (Kubernetes, ECS) use `/live` for restart decisions and `/ready` for traffic routing. The existing `/health` and `/ready` endpoints are minimal and do not conflict; keeping `/health` prevents breaking any existing monitoring integrations while we add the proper liveness/readiness pair.

**Response shapes**:

`GET /live` → 200:
```json
{"status": "alive"}
```

`GET /ready` → 200 (all required healthy) or 503 (any required degraded):
```json
{
  "status": "ready" | "degraded",
  "subsystems": [
    {"name": "github", "status": "healthy", "required": true, "checked_at": "2026-02-25T10:00:00Z"},
    {"name": "agent_ssh", "status": "degraded", "required": false, "details": "SSH timeout", "checked_at": "2026-02-25T10:00:01Z"}
  ],
  "response_time_ms": 1
}
```

---

## R5: Dashboard Format and Alert Rules

**Decision**: Grafana JSON dashboard format, committed to `docs/dashboards/coordinare.json`. Alert rules embedded in dashboard panels (Grafana unified alerting format). Each alert rule includes a `runbookURL` annotation pointing to `docs/runbooks/<alert-name>.md`.

**Rationale**: A-005 explicitly calls out Grafana JSON as the target format. Grafana is the most widely deployed open-source metrics visualization tool; JSON import is a single-file operation. Embedding alert rules in the dashboard JSON (unified alerting model) keeps them co-located with the panels they monitor and ensures the 1-alert-1-runbook invariant test (FR-013) can be enforced by parsing a single file.

**Alert rules planned** (one per metric family from FR-001 through FR-003):
1. `CoordinareHighCycleFailureRate` — board orchestrator
2. `CoordinareLongCycleDuration` — board orchestrator
3. `CoordinareNotificationFailures` — notification system
4. `CoordinareCircuitBreakerOpen` — circuit breaker

**Alternatives considered**:
- Separate alert rule YAML (Prometheus Alertmanager rules): More portable but requires two files and two import steps; operators would need a separate system to see runbook links.
- Grafana Provisioning YAML: Not portable across Grafana installations without provisioning setup.

---

## R6: 1-Alert-1-Runbook Invariant Test

**Decision**: Implement as a pytest test (`test_runbook_coverage.py`) that parses `docs/dashboards/coordinare.json`, extracts all `runbookURL` values, and asserts that each referenced path exists as a file. Runs in CI with every PR.

**Rationale**: FR-013 requires the invariant to be enforced automatically. A pytest test is the lightest implementation: no new tool, no new CI step, just a new test file. The test is deterministic (file existence check), fast (pure filesystem), and self-documenting.

**Test structure**:
```python
def test_every_alert_has_runbook():
    dashboard = json.load(open("docs/dashboards/coordinare.json"))
    # Extract runbookURL from alert rules in all panels
    for alert_rule in extract_alert_rules(dashboard):
        runbook_path = Path(alert_rule["annotations"]["runbookURL"])
        assert runbook_path.exists(), f"Missing runbook: {runbook_path}"
```

**Alternatives considered**:
- Shell script in Makefile: Less testable, not IDE-friendly, harder to debug.
- Pre-commit hook: Only runs locally, not enforced in CI automatically.

---

## R7: Runbook Template Structure

**Decision**: 5-section Markdown template: Trigger Condition, Operational Impact, Diagnostic Steps, Resolution Actions, Escalation Path. Template committed to `docs/runbooks/_template.md`.

**Rationale**: FR-012 mandates exactly these sections (trigger, impact, diagnostic steps ≥3, resolution ≥1, escalation). Committing a template ensures new runbooks follow the same structure and makes FR-013 validation simpler (could optionally validate section presence).

**Template structure**:
```markdown
# Runbook: <AlertName>
## Trigger Condition
## Operational Impact
## Diagnostic Steps
1. ...
2. ...
3. ...
## Resolution Actions
1. ...
## Escalation Path
```

---

## R8: Config Extensions for Health Checks

**Decision**: Add two optional fields to `ProjectConfiguration` in `config.py`:
- `health_check_timeout_seconds: int = Field(default=2, ge=1, le=30)` — per-subsystem probe stale threshold
- `optional_subsystems: list[str] = Field(default_factory=list)` — subsystems that don't block readiness

**Rationale**: FR-008 requires configurable timeouts; FR-009 requires optional subsystem designation. Both need config backing. Both have safe defaults (2s timeout, no optional subsystems → strict readiness by default). These fields are owned by spec 009, not spec 008 (008 covers the validation/discovery mechanism, not these specific fields).

**Note**: `optional_subsystems` is a list type and must be set in YAML (cannot be overridden via env var per spec 008 A-002).

---

## R9: Build Info Metric

**Decision**: Use `prometheus_client.Info` gauge for `coordinare_build_info` with labels: `version`, `python_version`, `started_at`.

**Rationale**: `prometheus_client.Info` is the canonical way to expose build metadata as a Prometheus metric. It creates a gauge named `coordinare_build_info` with value 1 and user-defined labels. It satisfies FR-004 (version, start time) and US1 acceptance scenario 4.

**Values sourced from**:
- `version`: Read from package metadata (`importlib.metadata.version("coordinare")`) or a `__version__` constant
- `python_version`: `platform.python_version()`
- `started_at`: ISO timestamp captured at daemon init

---

## Summary: New Files

| File | Purpose |
|---|---|
| `src/coordinare/observability.py` | `CycleContext`, `bind_cycle_id()`, `clear_cycle_id()`, `HealthRegistry`, `HealthProbe`, `HealthReport` |
| `src/coordinare/metrics.py` | MODIFIED — extend with new metrics |
| `src/coordinare/health.py` | MODIFIED — add `/live`, enhance `/ready` |
| `src/coordinare/daemon.py` | MODIFIED — bind/clear cycle_id, update HealthRegistry, record metrics |
| `src/coordinare/config.py` | MODIFIED — add health_check_timeout_seconds, optional_subsystems |
| `docs/dashboards/coordinare.json` | Grafana dashboard definition |
| `docs/runbooks/_template.md` | Runbook template |
| `docs/runbooks/CoordinareHighCycleFailureRate.md` | Runbook |
| `docs/runbooks/CoordinareLongCycleDuration.md` | Runbook |
| `docs/runbooks/CoordinareNotificationFailures.md` | Runbook |
| `docs/runbooks/CoordinareCircuitBreakerOpen.md` | Runbook |
| `tests/unit/test_cycle_correlation.py` | Cycle ID binding/clearing tests |
| `tests/unit/test_metrics_coverage.py` | Metric increment/label tests |
| `tests/unit/test_health_endpoints.py` | Live/ready endpoint tests |
| `tests/unit/test_runbook_coverage.py` | 1-alert-1-runbook invariant test |
