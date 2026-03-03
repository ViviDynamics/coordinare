# API Contracts: Live Web Dashboard (010)

Two HTTP endpoints are served by the dashboard FastAPI app on the configured `dashboard_port` (default 8090).

---

## GET /

**Summary**: Serves the dashboard HTML page.

**Request**: `GET /` — no parameters, no authentication.

**Response**:
- Status: `200 OK`
- Content-Type: `text/html; charset=utf-8`
- Body: Full HTML document (inline Python string constant). The page shows a loading skeleton on first load; JavaScript connects to `/events` and updates the DOM on the first `state_update` event.

**Error cases**: None (page always serves; if daemon is starting, the loading skeleton remains until the first SSE event arrives).

**Logging**: Request logged by middleware with `method="GET"`, `path="/"`, `status_code=200`, `response_time_ms=<float>`.

---

## GET /events

**Summary**: Server-Sent Events stream. Pushes `state_update` events as daemon state changes.

**Request**: `GET /events` — no parameters. Browser sends `Accept: text/event-stream` automatically via `EventSource`.

**Response**:
- Status: `200 OK`
- Content-Type: `text/event-stream`
- Body: Unbounded SSE stream (connection held open)

**SSE event types**:

### `state_update`

Sent immediately on connect (current snapshot) and after each daemon cycle that produces a state change.

```
event: state_update
data: {"phase": "idle", "phase_label": "Idle", "active_card_title": null, ...}

```

**Payload schema** — all fields always present:

| Field | Type | Description |
|-------|------|-------------|
| `phase` | `string` | Raw phase value (e.g., `"monitoring_agent"`) |
| `phase_label` | `string` | Human-readable label (`"Monitoring Agent"`) |
| `active_card_title` | `string \| null` | Card title; null when idle |
| `active_card_column` | `string \| null` | Board column; null when idle |
| `pr_url` | `string \| null` | PR URL; null if no PR |
| `agent_session_id` | `string \| null` | Active session ID; null if none |
| `open_questions` | `string[]` | Blocked-phase questions; empty otherwise |
| `subsystems` | `SubsystemHealth[]` | All registered health probes |
| `cycles_completed` | `integer` | Total successful cycles since daemon start |
| `last_cycle_duration_seconds` | `number \| null` | Duration of most recent cycle; null before first |
| `consecutive_error_count` | `integer` | Number of consecutive failing cycles; resets to 0 after a successful cycle |
| `daemon_start_time` | `string` | ISO 8601 UTC timestamp; displayed prominently |
| `cycle_history` | `CycleHistoryEntry[]` | Up to 20 most recent cycles, newest first |

**SubsystemHealth object**:

| Field | Type | Description |
|-------|------|-------------|
| `name` | `string` | Subsystem identifier |
| `status` | `"healthy" \| "degraded" \| "unavailable"` | Current health status |
| `required` | `boolean` | Whether this subsystem is required for readiness |
| `checked_at` | `string` | ISO 8601 UTC of last probe update |
| `details` | `string \| null` | Optional detail string |

**CycleHistoryEntry object**:

| Field | Type | Description |
|-------|------|-------------|
| `timestamp` | `string` | ISO 8601 UTC of cycle completion |
| `phase` | `string` | Phase value at completion |
| `duration_seconds` | `number` | Wall-clock duration of cycle |
| `outcome` | `"success" \| "error"` | Cycle result |

### Keepalive comment

Sent every 15 seconds when no `state_update` is ready, to prevent proxy/browser timeout.

```
: keepalive

```

**Not delivered** to browser `EventSource` message handlers — invisible to application code.

---

## Reconnect Behaviour

The browser's native `EventSource` API reconnects automatically with exponential backoff when the connection is lost. The `onerror` callback fires during the disconnected interval. The dashboard JavaScript uses `onerror` to show a "Disconnected" banner and `onopen` (on reconnect) to dismiss it.

On reconnect the server immediately sends a `state_update` event with the current snapshot — the banner is dismissed and fresh data appears without a full page reload.

---

## Middleware Logging

Every request to both endpoints is logged as a structured `structlog` entry with these fields:

| Field | Value |
|-------|-------|
| `method` | HTTP method (`"GET"`) |
| `path` | Request path (`"/"` or `"/events"`) |
| `status_code` | HTTP status code |
| `response_time_ms` | Milliseconds from request received to headers sent |
| `streaming` | `true` only for `/events` (elapsed is headers-sent, not stream duration) |

---

## Non-Endpoints

The dashboard app does **not** expose:
- `/health` — served by the existing health app on `health_check_port` (default 8080)
- `/metrics` — served by the existing health app
- Any write/mutation endpoints — dashboard is strictly read-only
