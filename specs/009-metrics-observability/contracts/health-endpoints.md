# Contract: Health Endpoints (009)

**Branch**: `009-metrics-observability` | **Date**: 2026-02-25

---

## GET /live

**Purpose**: Liveness probe — tells the orchestrator whether the daemon process is alive and responsive.

### Request

```
GET /live HTTP/1.1
Host: localhost:8080
```

No headers required.

### Response: 200 OK (always, while process is running)

```json
{"status": "alive"}
```

| Field | Type | Value |
|---|---|---|
| `status` | `string` | Always `"alive"` |

**Invariant**: This endpoint MUST return 200 as long as the process is running and the FastAPI event loop is accepting connections. It MUST NOT check any subsystem connectivity.

---

## GET /ready

**Purpose**: Readiness probe — tells the orchestrator whether the daemon is ready to handle work (all required subsystems operational).

### Request

```
GET /ready HTTP/1.1
Host: localhost:8080
```

### Response: 200 OK (all required subsystems healthy)

```json
{
  "status": "ready",
  "subsystems": [
    {
      "name": "github",
      "status": "healthy",
      "required": true,
      "checked_at": "2026-02-25T10:00:00Z",
      "details": null
    },
    {
      "name": "agent_ssh",
      "status": "healthy",
      "required": true,
      "checked_at": "2026-02-25T10:00:01Z",
      "details": null
    },
    {
      "name": "notifications",
      "status": "healthy",
      "required": false,
      "checked_at": "2026-02-25T10:00:00Z",
      "details": null
    }
  ],
  "response_time_ms": 1.2
}
```

### Response: 503 Service Unavailable (one or more required subsystems degraded)

```json
{
  "status": "degraded",
  "subsystems": [
    {
      "name": "github",
      "status": "degraded",
      "required": true,
      "checked_at": "2026-02-25T10:00:00Z",
      "details": "Last successful poll was 95 seconds ago (threshold: 60s)"
    },
    {
      "name": "agent_ssh",
      "status": "healthy",
      "required": true,
      "checked_at": "2026-02-25T10:00:01Z",
      "details": null
    }
  ],
  "response_time_ms": 0.8
}
```

### Response Fields

| Field | Type | Always present | Description |
|---|---|---|---|
| `status` | `string` | Yes | `"ready"` or `"degraded"` |
| `subsystems` | `array` | Yes | All registered subsystems |
| `subsystems[].name` | `string` | Yes | Canonical subsystem name |
| `subsystems[].status` | `string` | Yes | `"healthy"`, `"degraded"`, or `"unavailable"` |
| `subsystems[].required` | `bool` | Yes | `false` if in `optional_subsystems` config |
| `subsystems[].checked_at` | `string` (ISO 8601) | Yes | UTC timestamp of last probe |
| `subsystems[].details` | `string \| null` | Yes | Failure description when status ≠ `healthy` |
| `response_time_ms` | `float` | Yes | Time to generate response, in milliseconds |

### HTTP Status Codes

| Code | Condition |
|---|---|
| `200` | All required subsystems have status `healthy` |
| `503` | At least one required subsystem has status `degraded` or `unavailable` |

### Behavior Invariants

1. Response MUST always arrive within `health_check_timeout_seconds × subsystem_count + 1 second` (FR-008).
2. If a subsystem's `checked_at` is older than `health_check_timeout_seconds`, its status MUST be reported as `degraded` with a stale-data details message.
3. Subsystems listed in `optional_subsystems` config MUST appear in the subsystems array with `"required": false` and MUST NOT affect the top-level `status` field.
4. The endpoint is synchronous (reads from `HealthRegistry` cache) — no live connectivity probes on the request path.

---

## GET /health (Backward Compatibility — Deprecated)

The existing `/health` endpoint is retained for backward compatibility. It continues to return the previous format. Operators should migrate to `/live` and `/ready`.

**No changes to this endpoint in spec 009.**

---

## Subsystem Names

Canonical names used in `subsystems[].name` and `optional_subsystems` config:

| Name | Description |
|---|---|
| `github` | GitHub API connectivity (board polling) |
| `agent_ssh` | SSH connectivity to agent host |
| `notifications` | Notification channel delivery (Slack/email) |
| `config` | Configuration loaded and valid |
