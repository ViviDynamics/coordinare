# Data Model: Live Web Dashboard (010)

## Entities

### DashboardState

A point-in-time snapshot of all data the dashboard renders. Assembled synchronously from in-process daemon objects — no database, no I/O.

| Field | Type | Source | Notes |
|-------|------|--------|-------|
| `phase` | `str` | `CoordinareDaemon._state["phase"]` | Raw phase string; formatted for display via `format_phase_label()` |
| `active_card_title` | `str \| None` | `WorkflowSnapshot.active_card_title` | None when idle |
| `active_card_column` | `str \| None` | `WorkflowSnapshot.active_card_column` | None when idle |
| `pr_url` | `str \| None` | `WorkflowSnapshot.pr_url` | None when no PR exists |
| `agent_session_id` | `str \| None` | `WorkflowSnapshot.agent_session_id` | None when no session active |
| `open_questions` | `list[str]` | `WorkflowSnapshot.open_questions` | Non-empty only during `blocked` phase |
| `subsystems` | `list[SubsystemHealth]` | `HealthRegistry.snapshot().probes` | All registered probes |
| `cycles_completed` | `int` | `METRICS.cycles_completed_total._value` | Prometheus counter value |
| `last_cycle_duration_seconds` | `float \| None` | `METRICS.cycle_duration_seconds` (latest observation) | None before first cycle |
| `consecutive_error_count` | `int` | `CoordinareDaemon._state.get("error_count", 0)` | Number of consecutive failed cycles; resets to 0 after a successful cycle |
| `daemon_start_time` | `str` | `METRICS.build_info["started_at"]` | ISO 8601 UTC string set at daemon startup |
| `cycle_history` | `list[CycleHistoryEntry]` | `DashboardStore.history` | Up to 20 entries, newest first |

**Assembled by**: `DashboardStore.build_snapshot(daemon, metrics, health)` — pure function, called after each `ainvoke` and on SSE connect.

---

### CycleHistoryEntry

One completed-cycle record in the rolling in-memory log.

| Field | Type | Notes |
|-------|------|-------|
| `timestamp` | `str` | ISO 8601 UTC, captured at cycle completion |
| `phase` | `str` | Phase value at completion (raw string, formatted for display) |
| `duration_seconds` | `float` | Wall-clock cycle duration from `perf_counter()` |
| `outcome` | `"success" \| "error"` | `"success"` on normal completion; `"error"` on exception |

**Storage**: `collections.deque(maxlen=20)` on `DashboardStore`. Oldest entry dropped automatically when limit exceeded. Resets on daemon restart.

**Ordering**: Displayed newest-first (reversed iteration of the deque).

---

### SubsystemHealth

Derived from `HealthProbe` in `coordinare.observability`; projected into JSON for the SSE payload.

| Field | Type | Notes |
|-------|------|-------|
| `name` | `str` | Subsystem name (e.g., `"github"`, `"agent_ssh"`) |
| `status` | `"healthy" \| "degraded" \| "unavailable"` | From `HealthProbe.status.value` |
| `required` | `bool` | From `HealthProbe.is_required` |
| `checked_at` | `str` | ISO 8601 UTC timestamp |
| `details` | `str \| None` | Extra info (stale probe message, circuit-open reason, etc.) |

---

### DashboardStore

In-process object holding mutable dashboard-specific state. One instance created at startup, passed to `create_dashboard_app()`.

| Field | Type | Notes |
|-------|------|-------|
| `history` | `deque[CycleHistoryEntry]` | `maxlen=20`; thread-safe only within single asyncio event loop |
| `broadcaster` | `SSEBroadcaster` | Holds per-client queues; `broadcast()` called after each cycle |

**Methods**:
- `record_cycle(duration_seconds, phase, outcome)` — appends a `CycleHistoryEntry`; called by daemon after `ainvoke`
- `build_snapshot(daemon, metrics, health) -> dict` — assembles the full JSON payload for SSE events
- `broadcast(snapshot_dict)` — calls `self.broadcaster.broadcast(snapshot_dict)`

---

### SSEBroadcaster

Manages per-client SSE queues. Lives for the full process lifetime.

| Field | Type | Notes |
|-------|------|-------|
| `_queues` | `set[asyncio.Queue[dict \| None]]` | One queue per connected browser tab |

**Methods**:
- `subscribe() -> asyncio.Queue` — creates queue, adds to set, returns it
- `unsubscribe(q)` — removes queue from set; called from SSE generator `finally` block
- `broadcast(payload: dict)` — iterates `list(self._queues)` snapshot, calls `q.put_nowait(payload)` (drops silently if queue full)

**Queue size**: `maxsize=32` — a slow browser tab can fall 32 events behind before drops begin.

---

## State Transitions

The dashboard is strictly read-only. No dashboard action causes a state change. The SSE connection lifecycle is:

```
Browser opens /events
  → SSEBroadcaster.subscribe() → Queue created
  → Generator yields initial state_update (current snapshot)
  → Generator loops: wait 15s max on queue.get()
      → Got event: yield "event: state_update\ndata: {json}\n\n"
      → Timeout: yield ": keepalive\n\n"
  → Browser disconnects (GeneratorExit)
      → finally: SSEBroadcaster.unsubscribe(queue)
```

---

## JSON Wire Shape (SSE `state_update` payload)

```json
{
  "phase": "monitoring_agent",
  "phase_label": "Monitoring Agent",
  "active_card_title": "Fix login timeout",
  "active_card_column": "In Progress",
  "pr_url": "https://github.com/org/repo/pull/42",
  "agent_session_id": "sess-abc123",
  "open_questions": [],
  "subsystems": [
    {"name": "github", "status": "healthy", "required": true, "checked_at": "2026-03-02T10:00:00Z", "details": null},
    {"name": "agent_ssh", "status": "healthy", "required": true, "checked_at": "2026-03-02T10:00:00Z", "details": null},
    {"name": "config", "status": "healthy", "required": true, "checked_at": "2026-03-02T09:55:00Z", "details": null},
    {"name": "notifications", "status": "unavailable", "required": false, "checked_at": "2026-03-02T09:55:00Z", "details": null}
  ],
  "cycles_completed": 17,
  "last_cycle_duration_seconds": 1.24,
  "consecutive_error_count": 0,
  "daemon_start_time": "2026-03-02T09:30:00Z",
  "cycle_history": [
    {"timestamp": "2026-03-02T10:00:00Z", "phase": "monitoring_agent", "duration_seconds": 1.24, "outcome": "success"},
    {"timestamp": "2026-03-02T09:59:28Z", "phase": "idle", "duration_seconds": 0.87, "outcome": "success"}
  ]
}
```

**Empty-state values** (before first cycle completes):
- `cycles_completed: 0`
- `last_cycle_duration_seconds: null`
- `cycle_history: []`
- `active_card_title: null`, `active_card_column: null`, `pr_url: null`, `agent_session_id: null`
- `phase: "idle"`, `phase_label: "Idle"`
