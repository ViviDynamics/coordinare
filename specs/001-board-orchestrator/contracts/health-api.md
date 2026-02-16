# Contract: Health Check & Metrics API

**Feature**: 001-board-orchestrator
**Date**: 2026-02-16
**Service**: FastAPI HTTP server (embedded in coordinare daemon)
**Requirements**: FR-021 (health check), FR-022 (metrics)

## Base URL

```
http://0.0.0.0:{health_check_port}
```

Default port: 8080 (configurable via `health_check_port`).

---

## Endpoints

### GET /health

Reports daemon status and connectivity to external services (FR-021).

**Response 200 OK**:
```json
{
  "status": "healthy" | "degraded" | "unhealthy",
  "uptime_seconds": 12345,
  "current_card": {
    "id": "string",
    "issue_number": 123,
    "title": "string",
    "status": "IN_PROGRESS",
    "since": "2026-02-16T12:00:00Z"
  },
  "services": {
    "github": {
      "status": "connected" | "disconnected",
      "last_poll_at": "2026-02-16T12:00:00Z",
      "rate_limit_remaining": 4500
    },
    "agent_ssh": {
      "status": "connected" | "disconnected",
      "host": "agent-host.example.com",
      "last_check_at": "2026-02-16T12:00:00Z"
    },
    "smtp": {
      "status": "connected" | "disconnected"
    },
    "slack": {
      "status": "connected" | "disconnected"
    }
  },
  "timestamp": "2026-02-16T12:00:00Z"
}
```

**Status Logic**:
- `healthy`: All services connected, daemon operating normally
- `degraded`: One or more non-critical services disconnected (SMTP, Slack) but core loop running
- `unhealthy`: GitHub or agent SSH disconnected, or daemon loop has stopped

**Response 503** (when daemon is unhealthy):
Same schema, with `"status": "unhealthy"`. Kubernetes/Docker health checks use this for restart decisions.

**`current_card`**: `null` when no card is being processed (idle).

---

### GET /metrics

Prometheus-compatible metrics export (FR-022).

**Response 200 OK** (Content-Type: text/plain):
```
# HELP coordinare_cards_processed_total Total number of cards processed
# TYPE coordinare_cards_processed_total counter
coordinare_cards_processed_total 42

# HELP coordinare_card_cycle_seconds Time from card pickup to Done
# TYPE coordinare_card_cycle_seconds histogram
coordinare_card_cycle_seconds_bucket{le="300"} 10
coordinare_card_cycle_seconds_bucket{le="600"} 25
coordinare_card_cycle_seconds_bucket{le="1800"} 38
coordinare_card_cycle_seconds_bucket{le="3600"} 41
coordinare_card_cycle_seconds_bucket{le="+Inf"} 42
coordinare_card_cycle_seconds_sum 28500
coordinare_card_cycle_seconds_count 42

# HELP coordinare_notifications_total Notifications sent by channel and status
# TYPE coordinare_notifications_total counter
coordinare_notifications_total{channel="email",status="success"} 150
coordinare_notifications_total{channel="email",status="failure"} 2
coordinare_notifications_total{channel="slack",status="success"} 148
coordinare_notifications_total{channel="slack",status="failure"} 0

# HELP coordinare_agent_dispatch_seconds Time to dispatch card to agent via SSH
# TYPE coordinare_agent_dispatch_seconds histogram
coordinare_agent_dispatch_seconds_bucket{le="5"} 35
coordinare_agent_dispatch_seconds_bucket{le="10"} 40
coordinare_agent_dispatch_seconds_bucket{le="30"} 42
coordinare_agent_dispatch_seconds_bucket{le="+Inf"} 42
coordinare_agent_dispatch_seconds_sum 210
coordinare_agent_dispatch_seconds_count 42

# HELP coordinare_errors_total Error count by category
# TYPE coordinare_errors_total counter
coordinare_errors_total{category="github_api"} 3
coordinare_errors_total{category="agent_ssh"} 1
coordinare_errors_total{category="notification"} 2
coordinare_errors_total{category="merge"} 0

# HELP coordinare_board_poll_seconds Time to complete a board poll
# TYPE coordinare_board_poll_seconds gauge
coordinare_board_poll_seconds 0.45

# HELP coordinare_up Whether the coordinare daemon is running
# TYPE coordinare_up gauge
coordinare_up 1
```

**Metric Definitions** (FR-022):

| Metric | Type | Labels | Description |
|--------|------|--------|-------------|
| `coordinare_cards_processed_total` | Counter | — | Total cards moved to Done |
| `coordinare_card_cycle_seconds` | Histogram | — | Card lifecycle duration (pickup to Done) |
| `coordinare_notifications_total` | Counter | `channel`, `status` | Notification send attempts by channel |
| `coordinare_agent_dispatch_seconds` | Histogram | — | Time to dispatch card via SSH |
| `coordinare_errors_total` | Counter | `category` | Errors by category |
| `coordinare_board_poll_seconds` | Gauge | — | Latest poll duration |
| `coordinare_up` | Gauge | — | Daemon liveness (1=running, 0=stopped) |

---

### GET /ready

Readiness probe for Kubernetes. Returns 200 only when the daemon has completed startup and verified connectivity.

**Response 200 OK**:
```json
{"ready": true}
```

**Response 503 Service Unavailable** (during startup or if critical services unavailable):
```json
{"ready": false, "reason": "GitHub API not reachable"}
```
