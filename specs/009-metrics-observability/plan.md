# Implementation Plan: Metrics & Observability

**Branch**: `009-metrics-observability` | **Date**: 2026-02-25 | **Spec**: [spec.md](spec.md)
**Input**: Feature specification from `/specs/009-metrics-observability/spec.md`

---

## Summary

Adds comprehensive observability to the coordinare daemon across five dimensions: (1) 7 new Prometheus metric families for the board orchestrator and config subsystems, plus defined names/labels for the 6 cross-spec metrics owned by specs 005 and 006; (2) per-cycle `cycle_id` correlation via `structlog.contextvars`, threaded through all coordinare log entries within a poll cycle; (3) separate `/live` and `/ready` health endpoints with per-subsystem status powered by a cached `HealthRegistry`; (4) a Grafana-compatible dashboard JSON file with panels for all metric families and 4 alert rules; and (5) runbook Markdown documents for each alert rule, with a pytest test that enforces the 1-alert-1-runbook invariant. All implementation uses existing dependencies — no new packages required.

---

## Technical Context

**Language/Version**: Python 3.12+
**Primary Dependencies**: `structlog` (contextvars), `prometheus-client` (metrics), `fastapi` (health server), `pydantic-settings` (config extensions) — all existing; no new dependencies
**Storage**: N/A (metrics held in prometheus-client registry; health state held in-memory `HealthRegistry`; no persistence)
**Testing**: `pytest` (existing), `pytest-asyncio` (existing), `httpx` test client (existing)
**Target Platform**: Linux server (coordinare daemon host), macOS (developer workstation)
**Project Type**: Single project (`src/` + `tests/`)
**Performance Goals**: SC-003 (liveness < 100ms, readiness bounded by `timeout × subsystems + 1s`), SC-007 (metric collection adds ≤ 10ms to poll cycle average)
**Constraints**: No new external dependencies (Constitution Principle I); all new code fully typed; no changes to public `CoordinareDaemon` API or `ProjectConfiguration` field names
**Scale/Scope**: Single daemon process; ~4 subsystems in health registry; ~20 metric time series; 4 alert rules + runbooks

---

## Constitution Check

### Principle I — Code Quality First ✅

- `src/coordinare/observability.py` has a single clear purpose: cycle correlation context + health registry.
- `CoordinareMetrics` extension is purely additive — no refactoring of existing metric paths.
- Type annotations on all public interfaces (`bind_cycle_id`, `clear_cycle_id`, `HealthRegistry`, `HealthProbe`, `HealthReport`).
- No dead code: runbooks are live documentation; alert rules reference actual metrics.

### Principle II — Testing Discipline ✅

- `test_cycle_correlation.py`: bind/clear lifecycle, no bleed to non-cycle logs, concurrent cycle isolation
- `test_metrics_coverage.py`: each new counter/histogram increments correctly with correct labels, zero-value initialization
- `test_health_endpoints.py`: `/live` always 200, `/ready` 503 on required degraded, subsystem list format, stale detection
- `test_runbook_coverage.py`: parses `coordinare.json`, asserts every `runbookURL` file exists (FR-013)
- Existing `test_health_api.py` and `test_daemon_*` tests updated for new health shapes

### Principle III — UX Consistency ✅

- `/ready` response body mirrors industry-standard health check format (name, status, required, details)
- Log `cycle_id` field follows existing structlog field naming conventions (lowercase_snake)
- Dashboard panels follow consistent naming: metric family name as panel title, unit annotation on axes

### Principle IV — Performance by Design ✅

- SC-003 and SC-007 are measurable; enforced by pytest timing assertions
- `HealthRegistry.snapshot()` is O(subsystems) synchronous dict read — no async, no I/O
- `bind_cycle_id()` / `clear_cycle_id()` are stdlib `contextvars` operations — nanosecond overhead
- Prometheus counter/histogram `.inc()` / `.observe()` operations: < 1µs each

### Principle V — Clarity Before Action ✅

- 0 clarification questions required (scan found all categories Clear)
- No `NEEDS CLARIFICATION` tags remain in any spec 009 artifact
- Cross-spec dependencies (specs 005 and 006) explicitly documented in A-007, A-008, and Metric Catalog

### Quality Gates

| Gate | Status |
|---|---|
| Lint & Format (ruff) | Required; enforced in CI |
| Type Check | Required; all new code fully typed |
| Unit Tests | Required; four new test files |
| Integration Tests | N/A — all cross-module interactions (daemon↔health, daemon↔metrics) are covered at unit test level using mocks and in-process invocation; no external service calls are introduced by this spec. Constitution Principle II integration test requirement is met via unit tests that cross module boundaries within the same process. |
| Contract Tests | N/A for this spec — `contracts/health-endpoints.md` and `contracts/metrics-endpoint.md` document the API shape; contract-level assertions are embedded in `test_health_endpoints.py` (schema validation) and `test_metrics_coverage.py` (metric name and label verification). No separate inter-service contract test is needed as there is no inter-process API boundary added. |
| Coverage | Must not decrease |
| Performance | SC-003 assertion in T019; SC-007 benchmark in T028 |
| Accessibility | N/A (CLI/API, no UI) |
| Code Review | Required per constitution |

**No violations. No Complexity Tracking entries needed.**

---

## Project Structure

### Documentation (this feature)

```text
specs/009-metrics-observability/
├── plan.md                          # This file
├── research.md                      # Phase 0 output
├── data-model.md                    # Phase 1 output
├── quickstart.md                    # Phase 1 output
├── contracts/
│   ├── health-endpoints.md          # /live and /ready contract
│   └── metrics-endpoint.md         # /metrics contract
└── tasks.md                         # Phase 2 output (/speckit.tasks)
```

### Source Code (repository root)

```text
src/coordinare/
├── observability.py                 # NEW — CycleContext, HealthRegistry, bind/clear helpers
├── metrics.py                       # MODIFIED — 7 new metric families added
├── health.py                        # MODIFIED — /live added, /ready enhanced
├── daemon.py                        # MODIFIED — cycle_id, HealthRegistry updates, new metrics
└── config.py                        # MODIFIED — health_check_timeout_seconds, optional_subsystems

docs/
├── dashboards/
│   └── coordinare.json               # NEW — Grafana dashboard definition
└── runbooks/
    ├── _template.md                 # NEW — runbook template
    ├── CoordinareHighCycleFailureRate.md  # NEW
    ├── CoordinareLongCycleDuration.md     # NEW
    ├── CoordinareNotificationFailures.md  # NEW
    └── CoordinareCircuitBreakerOpen.md    # NEW

tests/unit/
├── test_health_api.py               # MODIFIED — update for /live and new /ready shape
├── test_cycle_correlation.py        # NEW
├── test_metrics_coverage.py         # NEW
├── test_health_endpoints.py         # NEW
└── test_runbook_coverage.py         # NEW
```

---

## Design Decisions

### DD-1: `structlog.contextvars` for cycle_id propagation (not `CoordinareState`)

**Problem**: All coordinare log entries within a poll cycle need to carry the same `cycle_id`, but log calls are spread across multiple nodes, services, and helper functions — threading a parameter explicitly would require changes at every log site.

**Solution**: Use `structlog.contextvars.bind_contextvars(cycle_id=cycle_id)` at the start of each cycle in `CoordinareDaemon.start()`. The existing structlog configuration already includes `merge_contextvars` as the first processor, so the `cycle_id` is automatically merged into every subsequent log call in the same async task. `clear_contextvars()` at cycle end prevents leakage into inter-cycle logs.

**Trade-off**: This relies on Python's `asyncio` task-local context propagation. If coordinare spawns sub-tasks that escape the cycle's async context (e.g., `asyncio.create_task()` with no context copy), those sub-tasks would not inherit the `cycle_id`. Accepted: all current coordinare graph nodes run within the cycle's async execution context. Documented in A-009.

---

### DD-2: `HealthRegistry` singleton with cached probe state

**Problem**: The `/ready` endpoint must respond in bounded time (SC-003) without coupling its latency to external API availability. Live health probes on the request path could hang on GitHub API timeouts.

**Solution**: `HealthRegistry` is a singleton holding the most recent `HealthProbe` per subsystem. The daemon updates each subsystem's status at natural checkpoints in the poll cycle (e.g., after a successful board poll, `HEALTH.update("github", HealthStatus.HEALTHY)`). The `/ready` handler calls `HEALTH.snapshot()` — a synchronous, non-blocking dict read. Stale detection: if `checked_at` is older than `health_check_timeout_seconds`, the probe is reported as `degraded` without hanging.

**Trade-off**: Health state reflects the last poll cycle, not real-time. A subsystem could become unavailable between poll cycles, and `/ready` would return healthy until the next poll cycle detects it. Accepted: this matches the existing behavior (last_poll_at inference in current `/health`); the response is bounded and deterministic.

---

### DD-3: Additive metrics extension (no registry replacement)

**Problem**: Adding 7 new metric families to `CoordinareMetrics` while preserving existing metrics and their existing uses.

**Solution**: Add new `Counter`, `Histogram`, `Gauge`, and `Info` attributes to `CoordinareMetrics.__init__()`. All metrics share the single `_registry`. Existing metrics (`cards_processed_total`, `notifications_total`, `errors_total`, `card_cycle_seconds`, `agent_dispatch_seconds`, `board_poll_seconds`) are preserved unchanged.

**`daemon_up` naming resolution**: The existing `up` attribute is renamed to `daemon_up` to match FR-001. All existing call sites are updated in T006. The Prometheus exposition name changes from `up` to `daemon_up`; this is intentional — the name `up` conflicts with Prometheus's own native `up` scrape-target metric, so the rename is correct practice.

**`cards_processed_total` status**: Already exists from spec 001 integration. Not re-created by spec 009. T006 explicitly states "preserve it unchanged."

**`coordinare_build_info` type**: Uses `prometheus_client.Info` (not a plain `Gauge`). Info metrics are the idiomatic way to expose build metadata in Prometheus. The exposition name will be `coordinare_build_info_info{...} 1.0` — this is standard prometheus-client behavior. Spec.md FR-004 has been updated to reflect this.

**Trade-off**: Over time `CoordinareMetrics` will accumulate many attributes. A future refactor could split by subsystem (e.g., `BOARD_METRICS`, `HEALTH_METRICS`) — but that is a spec 010+ concern. For now, a single class with clear docstrings is the simplest safe approach.

---

### DD-4: Alert rules embedded in Grafana dashboard JSON

**Problem**: Alert rules must reference runbook URLs. Two options: (a) separate Prometheus Alertmanager YAML, or (b) embedded in Grafana dashboard JSON.

**Solution**: Embed alert rules in `docs/dashboards/coordinare.json` using Grafana's unified alerting format. Each alert panel has an `"alert"` block with an `"annotations"` field containing the `runbookURL`. This keeps everything in one file, simplifies the FR-013 invariant test (one file to parse), and makes the import flow a single action for operators.

**Trade-off**: Grafana-specific format (A-005). Operators using other monitoring systems must adapt. Accepted: Grafana is the specified target.

---

### DD-5: pytest file-existence test for 1-alert-1-runbook invariant

**Problem**: FR-013 requires automated enforcement that every dashboard alert has a runbook.

**Solution**: `tests/unit/test_runbook_coverage.py` loads `docs/dashboards/coordinare.json`, walks all panels' alert blocks, extracts `runbookURL` annotation values, and asserts `Path(runbook_url).exists()`. The test runs in < 1ms (pure filesystem) and is deterministic.

**Trade-off**: The test uses a relative path from the repo root; it must run from the correct working directory. The pytest `conftest.py` or a `pytest.ini` `rootdir` setting will ensure this.

---

## Implementation Phases

### Phase A — Foundation (no-impact)

Create new modules without modifying any existing code.

**A1**: Create `src/coordinare/observability.py`
- `HealthStatus` enum: `healthy`, `degraded`, `unavailable`
- `HealthProbe` dataclass: `subsystem_name`, `status`, `is_required`, `checked_at`, `details`
- `HealthReport` dataclass: `overall_status`, `probes`, `response_time_ms`
- `HealthRegistry` class: `register()`, `update()`, `snapshot()`
- `bind_cycle_id(cycle_id: str) -> None` — calls `structlog.contextvars.bind_contextvars`
- `clear_cycle_id() -> None` — calls `structlog.contextvars.clear_contextvars`
- `CycleContext` dataclass: `cycle_id`, `started_at` (lightweight reference type)

**A2**: Extend `src/coordinare/metrics.py` with new metrics
- `cycles_completed_total` (Counter)
- `card_state_transitions_total` (Counter, label: `transition_type`)
- `cycle_duration_seconds` (Histogram, standard Prometheus buckets)
- `config_load_duration_seconds` (Histogram)
- `coordinare_build_info` (Info, labels: `version`, `python_version`, `started_at`)
- `daemon_up` (alias or rename of existing `up`)
- Register notification + circuit-breaker metric names in the registry with zero-value initialization so they appear in `/metrics` output even before specs 005/006 write values
- Add `_initialize_zero_values()` method called from `__init__()`

**A3**: Write `tests/unit/test_cycle_correlation.py`
- `test_cycle_id_present_on_log_entries_during_cycle`
- `test_cycle_id_absent_on_log_entries_outside_cycle`
- `test_cycle_id_unique_across_sequential_cycles`
- `test_clear_cycle_id_removes_field`
- `test_concurrent_cycles_have_distinct_ids`

**A4**: Write `tests/unit/test_metrics_coverage.py`
- `test_cycles_completed_total_increments_on_cycle`
- `test_card_state_transitions_total_uses_correct_labels`
- `test_cycle_duration_seconds_observes_positive_value`
- `test_config_load_duration_recorded_at_startup`
- `test_coordinare_build_info_always_1`
- `test_zero_value_metrics_present_before_any_events`
- `test_notification_counters_registered_in_registry`

### Phase B — Health Endpoints

**B1**: Modify `src/coordinare/health.py`
- Import `HealthRegistry`, `HealthReport` from `observability.py`
- Add `GET /live` route: returns `{"status": "alive"}` with 200 unconditionally
- Enhance `GET /ready` route:
  - Call `HEALTH.snapshot()` (non-blocking)
  - Serialize `HealthReport` to JSON per the contract schema
  - Return 200 if `overall_status == healthy`, else 503
- Keep existing `/health` route unchanged (backward compatibility)
- Accept `HealthRegistry` as a constructor parameter (dependency injection for tests)

**B2**: Write `tests/unit/test_health_endpoints.py`
- `test_live_always_200`
- `test_live_returns_alive_body`
- `test_ready_200_when_all_required_healthy`
- `test_ready_503_when_required_subsystem_degraded`
- `test_ready_200_when_only_optional_subsystem_degraded`
- `test_ready_includes_all_registered_subsystems`
- `test_ready_marks_stale_probe_as_degraded`
- `test_ready_response_includes_response_time_ms`

### Phase C — Daemon Integration

**C1**: Modify `src/coordinare/daemon.py`
- Import `bind_cycle_id`, `clear_cycle_id`, `HEALTH` from `observability.py`
- Import `METRICS` from `metrics.py`
- At cycle start: `cycle_id = str(uuid4())`; `bind_cycle_id(cycle_id)`; record `t0 = perf_counter()`
- After `graph.ainvoke()` success: `METRICS.cycles_completed_total.inc()`; `METRICS.cycle_duration_seconds.observe(perf_counter() - t0)`; `HEALTH.update("github", HealthStatus.HEALTHY)`
- At cycle end (finally block): `clear_cycle_id()`
- On exception: `HEALTH.update("github", HealthStatus.DEGRADED, details=str(exc))`
- Record `card_state_transitions_total` in the appropriate graph nodes (or via daemon state inspection after ainvoke)
- Update `HEALTH.update("agent_ssh", ...)` based on agent dispatch success/failure in state

**C2**: Modify `src/coordinare/__main__.py`
- Initialize `HealthRegistry` with all subsystems and `optional_subsystems` from config
- Record `config_load_duration_seconds` timing around config load
- Set `coordinare_build_info` labels at startup

**C3**: Modify `src/coordinare/config.py`
- Add `health_check_timeout_seconds: int = Field(default=2, ge=1, le=30)`
- Add `optional_subsystems: list[str] = Field(default_factory=list)`

### Phase D — Dashboard and Runbooks

**D1**: Create `docs/dashboards/coordinare.json`
- Grafana JSON dashboard with panels for all metric families (FR-001 through FR-004)
- 4 alert rules (one per metric family in FR-001 through FR-003): `CoordinareHighCycleFailureRate`, `CoordinareLongCycleDuration`, `CoordinareNotificationFailures`, `CoordinareCircuitBreakerOpen`
- Each alert rule includes `"runbookURL": "docs/runbooks/<AlertName>.md"` annotation
- Build info panel (coordinare_build_info)
- All panels initialize correctly with zero values (no "no data" on fresh install)

**D2**: Create runbook documents
- `docs/runbooks/_template.md` — template with all 5 required sections
- `docs/runbooks/CoordinareHighCycleFailureRate.md`
- `docs/runbooks/CoordinareLongCycleDuration.md`
- `docs/runbooks/CoordinareNotificationFailures.md`
- `docs/runbooks/CoordinareCircuitBreakerOpen.md`
- Each runbook: trigger condition, operational impact, ≥3 diagnostic steps, ≥1 resolution action, escalation path

**D3**: Write `tests/unit/test_runbook_coverage.py`
- `test_every_alert_has_runbook` — parse dashboard JSON, assert each runbookURL file exists
- `test_runbooks_have_required_sections` — assert each runbook contains all 5 required section headings
- `test_dashboard_is_valid_json` — ensure coordinare.json parses without errors

### Phase E — Compatibility Fixes

**E1**: Update `tests/unit/test_health_api.py`
- Update `/ready` response assertions to match new schema (subsystems list, response_time_ms)
- Add assertions for `/live` endpoint
- Existing `/health` tests unchanged

---

## Dependency Order

```
A1 (observability module) ──→ A3 (correlation tests)
                          ──→ B1 (health.py modification)
                          ──→ C1 (daemon modification)

A2 (metrics extension) ────→ A4 (metrics tests)
                        ──→ C1 (daemon modification)
                        ──→ C2 (__main__ modification)

A1 + A2 ───────────────────→ C3 (config extension)
                          ──→ C1 (daemon)

B1 ────────────────────────→ B2 (health endpoint tests)
B1 ────────────────────────→ E1 (compatibility fixes)

D1 (dashboard) ────────────→ D3 (runbook coverage test)
D2 (runbooks) ─────────────→ D3 (runbook coverage test)

C1 + C2 + C3 ──────────────→ E1 (full integration pass)
```

A can proceed fully in parallel. B depends on A1. C depends on A1 + A2. D is independent of A/B/C. E depends on B and C.

---

## Out of Scope

- Real-time health probes on the `/ready` request path — deferred to a future spec (see DD-2)
- Cross-session or persistent cycle correlation (cycle IDs are session-scoped per A-003)
- Metrics for individual graph nodes (node-level instrumentation deferred; daemon-level metrics cover the aggregate)
- XDG directory support for runbook paths — runbooks are always at `docs/runbooks/`
- Alertmanager integration or notification routing — Grafana-embedded alert rules only
- Prometheus recording rules or aggregation rules — raw metrics only in this spec
- OpenTelemetry / distributed tracing — out of scope per spec; Prometheus only
