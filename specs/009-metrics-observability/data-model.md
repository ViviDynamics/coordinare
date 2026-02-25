# Data Model: Metrics & Observability (009)

**Branch**: `009-metrics-observability` | **Date**: 2026-02-25 | **Spec**: [spec.md](spec.md)

---

## Entities

### 1. CycleContext

Ephemeral correlation context bound to a single poll cycle. Never persisted. Scoped to the async execution context of one `graph.ainvoke()` call.

| Field | Type | Description |
|---|---|---|
| `cycle_id` | `str` | UUID4 string, unique per cycle within a session |
| `started_at` | `datetime` | UTC timestamp when cycle began |

**Lifecycle**:
```
cycle_start  → bind_cycle_id(uuid4()) → [all log entries carry cycle_id]
cycle_end    → clear_cycle_id()       → [subsequent log entries have no cycle_id]
```

**Invariant**: Exactly one `CycleContext` is active per async task at any time during a cycle. Non-cycle log entries (startup, shutdown, heartbeat, config load) MUST NOT have a `cycle_id` field.

---

### 2. HealthProbe

Result of a single subsystem health evaluation. Stored in `HealthRegistry`; updated by the daemon's poll cycle.

| Field | Type | Required | Default | Description |
|---|---|---|---|---|
| `subsystem_name` | `str` | Yes | — | Canonical name (e.g., `"github"`, `"agent_ssh"`, `"notifications"`, `"config"`) |
| `status` | `HealthStatus` | Yes | — | `healthy`, `degraded`, or `unavailable` |
| `is_required` | `bool` | Yes | `True` | If `False`, degraded status does not block overall readiness |
| `checked_at` | `datetime` | Yes | — | UTC timestamp of last probe evaluation |
| `details` | `str \| None` | No | `None` | Human-readable failure description when status ≠ `healthy` |

**HealthStatus enum**: `healthy` | `degraded` | `unavailable`

**Stale detection**: If `checked_at` is older than `health_check_timeout_seconds` from now, the probe is treated as `degraded` (the subsystem's state is unknown, which is not safe to assume healthy).

---

### 3. HealthReport

Aggregated snapshot of all probes, produced on each `/ready` request.

| Field | Type | Description |
|---|---|---|
| `overall_status` | `HealthStatus` | `healthy` if all required probes are `healthy`, else `degraded` |
| `probes` | `list[HealthProbe]` | All registered probes (required + optional) |
| `response_time_ms` | `float` | Milliseconds from request receipt to response generation |

**Overall status rule**:
- `healthy` → all required probes are `healthy`
- `degraded` → at least one required probe is `degraded` or `unavailable`
- Optional probes contribute to the `probes` list but never affect `overall_status`

---

### 4. HealthRegistry

Singleton that holds the current `HealthProbe` per registered subsystem. Thread-safe reads; updated by daemon poll cycle.

| Method | Signature | Description |
|---|---|---|
| `register` | `(name: str, required: bool) → None` | Register a new subsystem; initial status is `unavailable` |
| `update` | `(name: str, status: HealthStatus, details: str \| None) → None` | Record latest probe result |
| `snapshot` | `() → HealthReport` | Return current `HealthReport` (synchronous, non-blocking) |

**Internal storage**: `dict[str, HealthProbe]` — keyed by subsystem name.

---

### 5. MetricDefinition (reference, not a runtime class)

Describes each tracked measurement point. Used as design-time documentation only — actual metrics are `prometheus_client` objects.

| Field | Type | Description |
|---|---|---|
| `name` | `str` | Prometheus metric name (snake_case) |
| `metric_type` | `MetricType` | `counter`, `gauge`, `histogram`, `info` |
| `label_keys` | `list[str]` | Label dimension names |
| `description` | `str` | Help string shown in `/metrics` output |
| `owned_by` | `str` | Which spec owns emission (`009`, `005`, `006`) |

---

### 6. AlertRule (defined in `coordinare.json`)

Threshold condition on a metric, defined within the Grafana dashboard. Not a Python class — defined as JSON in `docs/dashboards/coordinare.json`.

| Field | Type | Description |
|---|---|---|
| `alert_name` | `str` | CamelCase alert identifier |
| `expr` | `str` | PromQL expression defining the alert condition |
| `severity` | `"warning" \| "critical"` | Alert severity label |
| `runbook_url` | `str` | Relative path `docs/runbooks/<AlertName>.md` |

---

### 7. Runbook (Markdown document)

Operational procedure for a specific alert. Committed as a file at `docs/runbooks/<AlertName>.md`.

| Section | Required | Description |
|---|---|---|
| Trigger Condition | Yes | The PromQL expression and human explanation of when this fires |
| Operational Impact | Yes | What breaks or degrades when this alert is active |
| Diagnostic Steps | Yes (≥3) | Ordered, executable steps with observable outcomes |
| Resolution Actions | Yes (≥1) | Concrete actions to resolve the alert |
| Escalation Path | Yes | Who to contact, what to provide, expected response time |

---

## Metric Catalog

Complete list of Prometheus metrics for spec 009. Metrics owned by other specs are noted.

### Board Orchestrator Metrics (FR-001)

| Metric Name | Type | Labels | Description | Owner |
|---|---|---|---|---|
| `cycles_completed_total` | Counter | — | Poll cycles completed successfully | 009 |
| `cards_processed_total` | Counter | `card_status` | Cards processed per status | 009 |
| `card_state_transitions_total` | Counter | `transition_type` | Card state transitions | 009 |
| `cycle_duration_seconds` | Histogram | — | Full poll cycle wall-clock duration | 009 |
| `daemon_up` | Gauge | — | 1 when daemon running, 0 on shutdown | 009 |

**Note**: `cards_processed_total` already exists as `cards_processed_total` in the current metrics.py; it will be preserved and may gain an updated label scheme. `daemon_up` already exists as `up`; spec 009 aligns its name to the FR-001 spec name.

### Notification Metrics (FR-002)

| Metric Name | Type | Labels | Description | Owner |
|---|---|---|---|---|
| `notifications_dispatched_total` | Counter | `event_type`, `channel_name` | Notifications sent | 006 |
| `notifications_failed_total` | Counter | `channel_name` | Notifications that failed after all retries | 006 |
| `notifications_rate_limited_total` | Counter | `channel_name` | Notifications suppressed by rate limiter | 006 |
| `notifications_deduplicated_total` | Counter | `channel_name` | Notifications suppressed by dedup | 006 |

**Note**: Spec 006 owns emission; spec 009 defines names, labels, and dashboard panels. These counters are registered in `CoordinareMetrics` (single registry) and written by spec 006's `NotificationService`.

### Circuit Breaker Metrics (FR-003)

| Metric Name | Type | Labels | Description | Owner |
|---|---|---|---|---|
| `circuit_breaker_trips_total` | Counter | `service_name` | Circuit breaker trip events | 005 |
| `circuit_breaker_state` | Gauge | `service_name` | 0=closed / 1=half-open / 2=open | 005 |

**Note**: Spec 005 owns emission; spec 009 defines names, labels, and dashboard panels.

### Config & Build Metrics (FR-004)

| Metric Name | Type | Labels | Description | Owner |
|---|---|---|---|---|
| `config_load_duration_seconds` | Histogram | — | Time to load and validate config at startup | 009 |
| `coordinare_build_info` | Info | `version`, `python_version`, `started_at` | Static build metadata | 009 |

---

## Config Schema Extensions

New fields added to `ProjectConfiguration` (in `src/coordinare/config.py`):

| Field | Type | Default | Constraints | Description |
|---|---|---|---|---|
| `health_check_timeout_seconds` | `int` | `2` | `ge=1, le=30` | Per-subsystem probe stale threshold in seconds |
| `optional_subsystems` | `list[str]` | `[]` | Items: any subsystem name | Subsystems whose degraded status does not block readiness |

---

## Sequence Diagram: Per-Cycle Observability Flow

```
CoordinareDaemon.start()
    │
    ├── [cycle N begins]
    │   ├── cycle_id = uuid4()
    │   ├── bind_contextvars(cycle_id=cycle_id)   ← all logs now carry cycle_id
    │   ├── t0 = perf_counter()
    │   │
    │   ├── graph.ainvoke(state)
    │   │   ├── check_board node → last_poll_at updated → HealthRegistry.update("github", healthy)
    │   │   ├── ... other nodes log with cycle_id automatically ...
    │   │   └── notify node → (spec 006) notifications_dispatched_total.inc()
    │   │
    │   ├── METRICS.cycles_completed_total.inc()
    │   ├── METRICS.cycle_duration_seconds.observe(perf_counter() - t0)
    │   ├── clear_contextvars()                    ← cycle_id no longer emitted
    │   │
    │   └── [sleep poll_interval_seconds]          ← no cycle_id on heartbeat log
    │
    └── [repeat]
```

---

## Sequence Diagram: /ready Request Flow

```
GET /ready
    │
    ├── HealthRegistry.snapshot()           ← synchronous, non-blocking, O(subsystems)
    │   ├── For each probe:
    │   │   └── If checked_at + timeout < now → override status = degraded (stale)
    │   └── Compute overall_status: degraded if any required probe is degraded/unavailable
    │
    ├── Serialize to JSON: {status, subsystems[], response_time_ms}
    │
    └── Return 200 (status=healthy) or 503 (status=degraded)
```

---

## State Machine: HealthProbe Status Transitions

```
           register()
               ↓
         [unavailable]
               │
    update(healthy) ↕ update(degraded/unavailable)
               │
           [healthy] ←──────→ [degraded]
                                   │
                        checked_at stale (> timeout)
                                   ↓
                             [degraded]   ← stays degraded until fresh update
```
