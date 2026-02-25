# Contract: Metrics Endpoint (009)

**Branch**: `009-metrics-observability` | **Date**: 2026-02-25

---

## GET /metrics

**Purpose**: Expose all Prometheus metrics in text format for scraping by Prometheus, Grafana Agent, or any OpenMetrics-compatible scraper.

### Request

```
GET /metrics HTTP/1.1
Host: localhost:8080
```

### Response: 200 OK

Content-Type: `text/plain; version=0.0.4; charset=utf-8`

The response body is the Prometheus text exposition format. Example (abbreviated):

```
# HELP cycles_completed_total Total number of poll cycles completed successfully
# TYPE cycles_completed_total counter
cycles_completed_total 42

# HELP cards_processed_total Total cards processed by status
# TYPE cards_processed_total counter
cards_processed_total{card_status="dispatched"} 15
cards_processed_total{card_status="monitoring_agent"} 10
cards_processed_total{card_status="idle"} 17

# HELP card_state_transitions_total Card state transition events
# TYPE card_state_transitions_total counter
card_state_transitions_total{transition_type="idle_to_dispatch"} 12
card_state_transitions_total{transition_type="dispatch_to_monitor"} 10
card_state_transitions_total{transition_type="monitor_to_merge"} 8

# HELP cycle_duration_seconds Full poll cycle wall-clock duration
# TYPE cycle_duration_seconds histogram
cycle_duration_seconds_bucket{le="0.5"} 5
cycle_duration_seconds_bucket{le="1.0"} 12
cycle_duration_seconds_bucket{le="2.5"} 30
cycle_duration_seconds_bucket{le="5.0"} 40
cycle_duration_seconds_bucket{le="10.0"} 42
cycle_duration_seconds_bucket{le="+Inf"} 42
cycle_duration_seconds_sum 87.4
cycle_duration_seconds_count 42

# HELP daemon_up 1 when daemon is running, 0 after shutdown
# TYPE daemon_up gauge
daemon_up 1

# HELP config_load_duration_seconds Time to load and validate configuration at startup
# TYPE config_load_duration_seconds histogram
config_load_duration_seconds_bucket{le="0.01"} 1
config_load_duration_seconds_sum 0.003
config_load_duration_seconds_count 1

# HELP coordinare_build_info Coordinare build metadata
# TYPE coordinare_build_info gauge
coordinare_build_info{version="0.9.0",python_version="3.12.2",started_at="2026-02-25T08:00:00Z"} 1

# HELP notifications_dispatched_total Notifications successfully dispatched
# TYPE notifications_dispatched_total counter
notifications_dispatched_total{event_type="card_blocked",channel_name="slack-ops"} 3
notifications_dispatched_total{event_type="card_merged",channel_name="email-team"} 8

# HELP notifications_failed_total Notification delivery failures after all retries
# TYPE notifications_failed_total counter
notifications_failed_total{channel_name="slack-ops"} 1

# HELP circuit_breaker_trips_total Circuit breaker trip events
# TYPE circuit_breaker_trips_total counter
circuit_breaker_trips_total{service_name="github"} 0
circuit_breaker_trips_total{service_name="agent_ssh"} 1

# HELP circuit_breaker_state Circuit breaker state (0=closed, 1=half-open, 2=open)
# TYPE circuit_breaker_state gauge
circuit_breaker_state{service_name="github"} 0
circuit_breaker_state{service_name="agent_ssh"} 0
```

---

## Metric Families

### Required Metrics (owned by spec 009)

| Metric | Type | Labels | Zero-value exposed |
|---|---|---|---|
| `cycles_completed_total` | Counter | — | Yes (starts at 0) |
| `cards_processed_total` | Counter | `card_status` | Yes (all status values at 0) |
| `card_state_transitions_total` | Counter | `transition_type` | Yes (all transition types at 0) |
| `cycle_duration_seconds` | Histogram | — | Yes (empty histogram) |
| `daemon_up` | Gauge | — | Yes (set to 1 at startup) |
| `config_load_duration_seconds` | Histogram | — | Yes (one observation at startup) |
| `coordinare_build_info` | Info/Gauge | `version`, `python_version`, `started_at` | Yes (always 1) |

### Cross-Spec Metrics (defined here, emitted by other specs)

| Metric | Type | Labels | Owner Spec |
|---|---|---|---|
| `notifications_dispatched_total` | Counter | `event_type`, `channel_name` | 006 |
| `notifications_failed_total` | Counter | `channel_name` | 006 |
| `notifications_rate_limited_total` | Counter | `channel_name` | 006 |
| `notifications_deduplicated_total` | Counter | `channel_name` | 006 |
| `circuit_breaker_trips_total` | Counter | `service_name` | 005 |
| `circuit_breaker_state` | Gauge | `service_name` | 005 |

**Note**: All metrics from all specs share the single `CollectorRegistry` in `src/coordinare/metrics.py`. They are all served by the same `/metrics` endpoint.

### Zero-Value Guarantee

All metric families MUST be initialized with label-value combinations at startup so that dashboards never show "no data." For counters, the initialization value is 0. This satisfies SC-001 (counter delta verification) and the edge case requirement in the spec.

---

## Non-Functional Constraints

- The `/metrics` endpoint is served by the same FastAPI app on port `health_check_port` (default: 8080).
- Metric rendering (`METRICS.render()`) MUST complete in < 10ms on a normally-loaded system (SC-007: metric collection adds ≤10ms to poll cycle average, but rendering cost is separate from collection cost).
- The endpoint requires no authentication (Prometheus scraping uses network-level security by convention).
