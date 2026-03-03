# Research: Live Web Dashboard (010)

## Decision 1: Real-time Delivery Mechanism

**Decision**: Server-Sent Events (SSE) via FastAPI's built-in `StreamingResponse` with `media_type="text/event-stream"`.

**Rationale**: SSE is unidirectional (server → client), which is all the dashboard needs. `StreamingResponse` is already part of Starlette (bundled with FastAPI) — no new dependency. The browser's native `EventSource` API handles reconnection automatically (FR-012), so reconnect logic comes for free with no JS library.

**Alternatives considered**:
- `sse-starlette` package — wraps the same `StreamingResponse` pattern with ~200 LOC. Adds a dependency for no material benefit at this scale. **Rejected**: unnecessary dependency (constitution Principle I).
- WebSockets — bidirectional, more complex lifecycle management. **Rejected**: overkill for a read-only dashboard.
- Polling — simpler but wastes resources and can't meet the ≤5s update requirement reliably. **Rejected**: spec requires push-based updates.

## Decision 2: HTML Rendering Strategy

**Decision**: Single-file inline HTML served as a Python string constant from `dashboard.py`. No server-side template engine.

**Rationale**: The initial page loads with a skeleton/loading state. SSE immediately fires a `state_update` event on connect with the full current snapshot; JavaScript updates the DOM. This means the server always serves the same static HTML — no per-request rendering needed.

**Alternatives considered**:
- Jinja2 templates — adds `jinja2` as a new dependency; not in the current venv. **Rejected**: the inline-HTML-plus-SSE pattern eliminates any need for server-side templating.
- Static file serving via Starlette `StaticFiles` — requires a separate `templates/` directory and a mount. **Rejected**: one file is simpler; dashboard is a single page.

## Decision 3: Broadcasting Architecture

**Decision**: `SSEBroadcaster` class holding a `set` of per-client `asyncio.Queue` instances. Daemon poll loop calls `broadcaster.broadcast(snapshot)` synchronously after each `ainvoke`.

**Rationale**: Per-client queues decouple slow clients from the daemon loop. `put_nowait()` drops events for full queues instead of blocking — a lagging browser tab can never stall the daemon. All objects live on the same asyncio event loop; no thread-safety machinery needed.

**Key implementation notes**:
- `list(self._queues)` snapshot before iterating prevents `RuntimeError: Set changed size during iteration` during concurrent disconnects.
- 15-second `asyncio.wait_for` on `queue.get()` sends a keepalive comment (`: keepalive\n\n`) to prevent proxy/browser timeout, and ensures disconnect is checked at least every 15 seconds.
- `finally` block in the SSE generator always removes the queue from the broadcaster, even on `GeneratorExit` (Starlette's disconnect signal).

## Decision 4: Dependencies

**Decision**: Zero new Python dependencies. Zero new browser-side libraries.

**New dependencies required**: None. All implementation uses:
- `fastapi` + `starlette` (already in `pyproject.toml`) — `StreamingResponse`, `HTMLResponse`, middleware
- `asyncio`, `json`, `time`, `uuid`, `collections.deque`, `datetime` (stdlib)
- `structlog` (already in `pyproject.toml`) — request logging middleware

**Browser**: Native `EventSource` API (supported in all modern browsers). No `npm`, no build step.

## Decision 5: Config Fields

**Decision**: Add `dashboard_port: int = Field(default=8090)` and `dashboard_host: str = "127.0.0.1"` to `ProjectConfiguration`.

**Rationale**: 8090 does not conflict with the default health check port (8080). `127.0.0.1` is the safe default per the clarification session; operators using a reverse proxy set `COORDINARE_DASHBOARD_HOST=0.0.0.0`.

**Environment variables** (auto-derived by pydantic-settings `COORDINARE_` prefix):
- `COORDINARE_DASHBOARD_PORT`
- `COORDINARE_DASHBOARD_HOST`

## Decision 6: Startup Failure on Port Conflict

**Decision**: If the dashboard port is already in use, log a structured error and call `sys.exit(1)` — same pattern as `verify_writable()` for the state store.

**Rationale**: A silent skip would leave the operator without the dashboard and no obvious reason why. Hard fail is consistent with existing daemon behaviour and makes the error immediately visible.

## Decision 7: Cycle History Storage

**Decision**: `collections.deque(maxlen=20)` on a new `DashboardStore` object, held in-memory alongside the daemon. Reset on daemon restart.

**Rationale**: `deque(maxlen=N)` automatically drops the oldest entry when the limit is exceeded — zero boilerplate. In-memory is acceptable per spec Assumption: structured logs provide long-term history. No persistence layer needed.

## Decision 8: Phase Label Formatting

**Decision**: `phase.replace("_", " ").title()` — implemented as a pure function `format_phase_label(phase: str) -> str`.

**Rationale**: Applies consistently to all phase values (known and future unknown) with no lookup table. `"monitoring_agent"` → `"Monitoring Agent"`, `"relay_feedback"` → `"Relay Feedback"`. The function is tested independently.

## Decision 9: Request Logging Middleware

**Decision**: `@app.middleware("http")` decorator with `structlog` — logs `method`, `path`, `status_code`, `response_time_ms`. For SSE endpoints (`/events`), also logs `streaming=True`; elapsed time reflects headers-sent, not stream duration (correct per FR-017).

**Rationale**: Simpler than `BaseHTTPMiddleware` and avoids the historical response-body-buffering concern in older Starlette versions. No new dependency.

## SSE Wire Format Reference

```
# Named event with JSON payload:
event: state_update\n
data: {"phase": "idle", ...}\n
\n

# Keepalive comment (not delivered to browser EventSource handlers):
: keepalive\n
\n
```

The browser `EventSource` built-in reconnects automatically with exponential backoff. The `onerror` callback fires during the disconnected interval — sufficient to show the "Disconnected" banner (FR-012).
