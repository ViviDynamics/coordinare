# Phase 0 Research: Symphony Management & Multi-Project Orchestration

**Scope**: Investigate coordinare codebase patterns relevant to multi-project/multi-symphony implementation  
**Date**: 2026-04-30

## 1. Dashboard Architecture & Page Registration

**File**: `src/coordinare/dashboard.py` (lines 1989-2040)

### Pattern: Page Registration
- Single HTML shell returned for all pages (SPA-style routing)
- Pages registered as simple GET endpoints returning `HTMLResponse(_DASHBOARD_HTML)`
- Client-side JS router handles navigation (framework agnostic)
- Example endpoints:
  - `GET /` → dashboard home
  - `GET /performers` → performers view
  - `GET /personas` → personas configuration
  - `GET /history` → history view

### Application Factory
```python
def create_dashboard_app(
    store: DashboardStore,
    daemon: CoordinareDaemon,
    metrics: CoordinareMetrics,
    health: HealthRegistry,
    config_path: Path | None = None,
) -> FastAPI
```
- Parameters passed at initialization time for dependency injection
- `config_path` optional parameter suggests extensibility for new config read/write endpoints

### API Endpoint Pattern
- All API endpoints follow `/api/{resource}` convention
- Both GET (read), POST (action), and PUT (update) are used
- Responses: JSON with standard status codes (200, 202, 409)
- Example: `/api/force-poll` (POST) returns `202 Accepted` or `409 Conflict`

**For Symphony Management**: New symphony management page would follow same pattern:
- `GET /symphonies` → returns HTML shell
- Client-side JS router displays symphony list
- `/api/symphonies` (GET) → list all symphonies
- `/api/symphonies/{name}` (PUT) → update symphony config
- `/api/symphonies/{name}` (DELETE) → remove symphony

## 2. Dashboard API Endpoints & Config Patterns

**File**: `src/coordinare/dashboard.py` (lines 2065-2084)

### Force-Poll Implementation (reference pattern)
```python
@app.post("/api/force-poll")
async def force_poll() -> JSONResponse:
    if daemon._cycle_active:
        return JSONResponse({"status": "cycle_in_progress"}, status_code=409)
    daemon._webhook_trigger.set()
    return JSONResponse({"status": "accepted"}, status_code=202)
```

**Key Points**:
- Check guard before action (`_cycle_active`)
- Delegate to daemon state for side effects (`_webhook_trigger.set()`)
- Return standard response codes (202 = submitted, 409 = conflict)
- No response body content beyond status key

### Config Read/Write Pattern
- Existing endpoints for persona updates (spec 018): `GET /api/personas`, `PUT /api/personas/{role}`
- Pattern: endpoint receives config from request body, validates, mutates daemon state, returns result
- No hot-reload trigger in API — would need to integrate with daemon lifecycle
- Store passed at app initialization: `create_dashboard_app(store=DashboardStore, ...)`

**For Symphony Management**: 
- Hot-reload trigger: similar to force-poll, set an event flag for daemon to reload config
- Validation: delegate to pydantic models (existing pattern via config.py)
- Persistence: config stored in `config.yaml` (loaded at startup, can be reloaded)

## 3. Prometheus Metrics Structure

**File**: `src/coordinare/metrics.py` (lines 22-220)

### Metric Types Used
- `Counter`: cumulative events (labels partition by category)
- `Gauge`: point-in-time values (single number or labeled)
- `Histogram`: distributions with predefined buckets
- `Info`: metadata (build info)

### Labeling Strategy
- Labels defined at metric creation: `labelnames=("service", "action")`
- Records partitioned by label values: `metric.labels(service="github", action="push").inc()`
- Current labels in use: `role`, `service`, `channel_name`, `category`, `action`, `outcome`, `transition_type`

**Example**: Multi-role token tracking (spec 034)
```python
self.card_tokens_total = Counter(
    "coordinare_card_tokens_total",
    "Total tokens consumed...",
    labelnames=("role",),
    registry=self.registry,
)
```
Usage: `metrics.card_tokens_total.labels(role=performer_role).inc(token_count)`

### Impact of Adding Symphony Label
- **Approach A** (recommended): Add `"symphony"` to all metrics that report per-project activity:
  - `coordinare_cycles_completed_total` (symphony)
  - `coordinare_cards_processed_total` (symphony, card_status)
  - `coordinare_card_state_transitions_total` (symphony, transition_type)
  - `coordinare_card_cycle_seconds` (symphony)
  - `coordinare_agent_dispatch_seconds` (symphony)
  - `coordinare_errors_total` (symphony, category)
  - `coordinare_service_calls_total` (symphony, service, action, outcome)
  
- **No symphony label** for:
  - `coordinare_cycles_completed_total` (global coordinare cycles, spans all symphonies)
  - `coordinare_daemon_up` (single daemon instance)
  - `coordinare_config_load_duration_seconds` (global)
  - Build/version info metrics

- Cardinality: If ~10 symphonies × existing label combinations, negligible impact
- Registry cleanup: Metrics persisted across symphony lifecycle (need stale label cleanup strategy for decommissioned symphonies)

## 4. Observability & Structlog Context Binding

**File**: `src/coordinare/observability.py` (lines 28-43)

### Cycle Context Pattern
```python
def bind_cycle_id(cycle_id: str) -> None:
    structlog.contextvars.bind_contextvars(cycle_id=cycle_id)

def clear_cycle_id() -> None:
    structlog.contextvars.unbind_contextvars("cycle_id")
```

**Pattern**:
- Context variables bound at task entry point (e.g., start of poll cycle)
- Automatically propagated to all child tasks in same async context
- Cleared at task exit (or before handling next task)
- Usage: `_log.info("event", key="value")` — structlog automatically includes bound context

### Adding Symphony Context
- Follow same pattern: `bind_symphony(symphony_name: str)` and `clear_symphony()`
- Call at start of per-symphony poll iteration
- All logs within that symphony's cycle will include `symphony=<name>` field
- Works naturally with async context propagation

**For Sequential Per-Symphony Polling** (per spec clarification Q3):
```python
for symphony in orchestrator.symphonies:
    bind_symphony(symphony.name)
    try:
        await conduct_symphony(symphony)
    finally:
        clear_symphony()
```

### Context Vars Interaction
- `structlog.contextvars.bind_contextvars()` accepts any `key=value` pairs
- Each async task inherits parent's context
- Multiple context vars coexist: `cycle_id=X, symphony=Y, card_id=Z` all logged together
- No conflict with existing `cycle_id` binding — both will appear

## 5. Force-Poll Mechanism & Trigger Model

**File**: `src/coordinare/daemon.py` (lines 190-200, 367-389)

### Current Implementation
```python
self._webhook_trigger: asyncio.Event = webhook_trigger or asyncio.Event()
self._cycle_active = False
```

In orchestration loop:
```python
trigger_task = asyncio.ensure_future(self._webhook_trigger.wait())
# ... other tasks ...
if trigger_task.done():
    self._webhook_trigger.clear()
    # poll immediately
```

**Key Points**:
- Single event shared between webhook listener and dashboard API
- Cleared after consuming (prevents tight loop)
- Flag `_cycle_active` prevents concurrent cycles
- Non-blocking: event.set() returns immediately

### Extending for Symphony Management
- **Hot-reload trigger**: Could reuse same event or create separate `_config_reload_trigger`
  - Separate event cleaner (semantically distinct from board poll)
  - Handler checks what changed and re-validates affected symphonies
  - Pattern: `await daemon._config_reload_trigger.wait()` in daemon loop

- **Alternative**: Embed config version in daemon state, daemon polls periodically for changes
  - Less reactive, but simpler (no new event machinery)
  - Still requires validation and state reset per changed symphony

## 6. Summary: Technical Decisions for Phase 1

### Dashboard & Config Management
- ✅ New page: `GET /symphonies` with client-side routing
- ✅ API endpoints: `/api/symphonies` (GET list), `/api/symphonies/{name}` (PUT/DELETE)
- ✅ Validation: Pydantic models in config.py (extend ProjectConfiguration to support symphony list)
- ⚠ Hot-reload: Use new `_config_reload_trigger` event (separate concern from board poll)

### Metrics & Observability
- ✅ Add `symphony` label to: cycles_completed, cards_processed, card_state_transitions, card_cycle_seconds, agent_dispatch_seconds, errors_total, service_calls_total
- ✅ Record symphony name with each metric increment in per-symphony poll loop
- ✅ Accept metric cardinality increase (10 symphonies × existing labels = manageable)

### Structlog Context Binding
- ✅ Add `bind_symphony(name)` and `clear_symphony()` functions to observability.py
- ✅ Call at start of per-symphony orchestration loop (within existing cycle context)
- ✅ Works naturally with async propagation

### Orchestration & Force-Poll
- ✅ Sequential per-symphony polling (per user clarification Q3)
- ✅ Reuse webhook_trigger for immediate forced cycles
- ✅ New config_reload_trigger for config hot-reload (fire after config.yaml writes succeed)
- ✅ _cycle_active flag prevents concurrent execution (extends naturally to symphony-level state if needed)

