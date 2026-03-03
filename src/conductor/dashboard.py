"""Live web dashboard for the coordinare daemon (010).

Serves a single-page dashboard at / and an SSE stream at /events.
No new dependencies — uses FastAPI/Starlette StreamingResponse (already present).
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import socket
import sys
import time
from collections import deque
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, StreamingResponse

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from coordinare.daemon import CoordinareDaemon
    from coordinare.metrics import CoordinareMetrics
    from coordinare.observability import HealthRegistry

_log = structlog.get_logger(__name__)

# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def format_phase_label(phase: str) -> str:
    """Convert a raw phase string to a human-readable label.

    Examples:
        "monitoring_agent" -> "Monitoring Agent"
        "relay_feedback"   -> "Relay Feedback"
        "idle"             -> "Idle"
    """
    return phase.replace("_", " ").title()


# ---------------------------------------------------------------------------
# SSE broadcaster
# ---------------------------------------------------------------------------


class SSEBroadcaster:
    """Fan-out SSE events to all connected browser tabs.

    Each subscriber gets its own asyncio.Queue.  put_nowait() is used so that
    a slow browser tab can never stall the daemon poll loop — events are simply
    dropped when the queue is full.
    """

    def __init__(self) -> None:
        self._queues: set[asyncio.Queue[dict | None]] = set()

    def subscribe(self) -> asyncio.Queue[dict | None]:
        """Register a new client queue and return it."""
        q: asyncio.Queue[dict | None] = asyncio.Queue(maxsize=32)
        self._queues.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict | None]) -> None:
        """Remove a client queue (called from the SSE generator finally block)."""
        self._queues.discard(q)

    def broadcast(self, payload: dict) -> None:
        """Push payload to all subscribers; drops silently for full queues.

        Iterates a list() snapshot of _queues so that concurrent disconnects
        (which call unsubscribe) cannot cause RuntimeError: Set changed size
        during iteration.
        """
        for q in list(self._queues):
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait(payload)  # slow client — drop event rather than blocking daemon


# ---------------------------------------------------------------------------
# Dashboard store
# ---------------------------------------------------------------------------


class DashboardStore:
    """In-process state for the dashboard: cycle history + SSE broadcaster.

    One instance is created at daemon startup and passed to both
    create_dashboard_app() and CoordinareDaemon (as dashboard_store).
    """

    def __init__(self) -> None:
        self.broadcaster = SSEBroadcaster()
        # Rolling in-memory log (FR-009); deque drops oldest automatically
        self.history: deque[dict] = deque(maxlen=20)
        # Most recent cycle duration — stored here rather than reading prometheus internals
        self.last_cycle_duration: float | None = None

    def record_cycle(
        self,
        *,
        duration_seconds: float,
        phase: str,
        outcome: str,
    ) -> None:
        """Append a CycleHistoryEntry for the just-completed cycle.

        Uses appendleft so iteration order is newest-first.
        """
        if outcome == "success":
            self.last_cycle_duration = duration_seconds
        self.history.appendleft(
            {
                "timestamp": datetime.now(UTC).isoformat(),
                "phase": phase,
                "duration_seconds": round(duration_seconds, 3),
                "outcome": outcome,
            }
        )

    async def sse_stream(
        self,
        daemon: CoordinareDaemon,
        metrics: CoordinareMetrics,
        health: HealthRegistry,
    ) -> AsyncGenerator[str, None]:
        """Async generator for the SSE /events stream.

        Yields the current snapshot immediately on subscribe, then waits for
        broadcaster events with a 15-second keepalive timeout.
        """
        q = self.broadcaster.subscribe()
        try:
            # Send current state immediately on connect (FR-011)
            snapshot = self.build_snapshot(daemon, metrics, health)
            yield f"event: state_update\ndata: {json.dumps(snapshot)}\n\n"
            while True:
                try:
                    payload = await asyncio.wait_for(q.get(), timeout=15.0)
                    yield f"event: state_update\ndata: {json.dumps(payload)}\n\n"
                except TimeoutError:
                    # Keepalive comment — prevents proxy/browser timeout
                    yield ": keepalive\n\n"
        finally:
            self.broadcaster.unsubscribe(q)

    def build_snapshot(
        self,
        daemon: CoordinareDaemon,
        metrics: CoordinareMetrics,
        health: HealthRegistry,
    ) -> dict:
        """Assemble the full DashboardState dict for an SSE state_update event."""
        # --- Phase / card / agent from persisted snapshot or live state ---
        snapshot = (
            daemon.state_store.last_snapshot
            if daemon.state_store is not None
            else None
        )
        phase = str(snapshot.phase if snapshot else daemon.state.get("phase", "idle"))

        # --- Subsystems from HealthRegistry ---
        health_report = health.snapshot()
        subsystems = [
            {
                "name": p.subsystem_name,
                "status": p.status.value,
                "required": p.is_required,
                "checked_at": p.checked_at.isoformat(),
                "details": p.details,
            }
            for p in health_report.probes
        ]

        # --- Metrics ---
        # Read prometheus counter value via the internal _value; this is stable
        # across prometheus-client 0.x for single-process use.
        try:
            cycles_completed = int(metrics.cycles_completed_total._value.get())
        except Exception:
            cycles_completed = 0

        # daemon start time stored in build_info label at startup
        try:
            build_info = metrics.build_info.labels()._value.get()
            started_at: str | None = (
                build_info.get("started_at") if isinstance(build_info, dict) else None
            )
        except Exception:
            started_at = None

        error_count = int(daemon.state.get("error_count", 0))

        return {
            "phase": phase,
            "phase_label": format_phase_label(phase),
            "active_card_title": snapshot.active_card_title if snapshot else None,
            "active_card_column": snapshot.active_card_column if snapshot else None,
            "pr_url": snapshot.pr_url if snapshot else None,
            "agent_session_id": snapshot.agent_session_id if snapshot else None,
            "open_questions": list(snapshot.open_questions) if snapshot else [],
            "subsystems": subsystems,
            "cycles_completed": cycles_completed,
            "last_cycle_duration_seconds": self.last_cycle_duration,
            "consecutive_error_count": error_count,
            "daemon_start_time": started_at,
            "cycle_history": list(self.history),
        }


# ---------------------------------------------------------------------------
# Dashboard HTML
# ---------------------------------------------------------------------------

_DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Coordinare Dashboard</title>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: monospace; font-size: 14px; background: #0d1117; color: #c9d1d9; padding: 16px; }
h1 { font-size: 18px; color: #58a6ff; margin-bottom: 16px; }
h2 { font-size: 13px; color: #8b949e; text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 8px; margin-top: 16px; }
.card { background: #161b22; border: 1px solid #30363d; border-radius: 6px; padding: 12px; margin-bottom: 12px; }
.phase { font-size: 22px; font-weight: bold; }
.phase-idle { color: #8b949e; }
.phase-dispatching, .phase-monitoring { color: #58a6ff; }
.phase-merging { color: #3fb950; }
.phase-blocked { color: #d29922; }
.phase-recovery { color: #f85149; }
.badge { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 12px; margin-right: 4px; }
.badge-healthy { background: #1f4a1f; color: #3fb950; }
.badge-degraded { background: #4a3a1f; color: #d29922; }
.badge-unavailable { background: #4a1f1f; color: #f85149; }
.badge-success { background: #1f4a1f; color: #3fb950; }
.badge-error { background: #4a1f1f; color: #f85149; }
.badge-required { background: #1a2a3a; color: #58a6ff; }
.badge-optional { background: #1e1e2e; color: #8b949e; }
.label { color: #8b949e; margin-right: 6px; }
a { color: #58a6ff; text-decoration: none; }
a:hover { text-decoration: underline; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th { text-align: left; color: #8b949e; padding: 4px 8px; border-bottom: 1px solid #30363d; }
td { padding: 4px 8px; border-bottom: 1px solid #21262d; }
.empty-state { color: #8b949e; font-style: italic; padding: 8px 0; }
.metric-row { display: flex; gap: 24px; flex-wrap: wrap; }
.metric { display: flex; flex-direction: column; }
.metric-value { font-size: 20px; font-weight: bold; color: #c9d1d9; }
.metric-label { font-size: 11px; color: #8b949e; }
.daemon-start { font-size: 13px; color: #8b949e; margin-top: 8px; }
.daemon-start strong { color: #d29922; }
#disconnected-banner {
  display: none; position: fixed; top: 0; left: 0; right: 0;
  background: #4a1f1f; color: #f85149; text-align: center;
  padding: 8px; font-weight: bold; z-index: 999;
}
</style>
</head>
<body>
<div id="disconnected-banner">&#9888; Disconnected — reconnecting...</div>
<h1>Coordinare Dashboard</h1>

<div class="card">
  <h2>Phase</h2>
  <div id="phase" class="phase phase-idle">Idle</div>
</div>

<div class="card">
  <h2>Active Card</h2>
  <div id="card-section">
    <span class="empty-state">No active card</span>
  </div>
</div>

<div id="questions-card" class="card" style="display:none">
  <h2>Open Questions</h2>
  <ul id="questions-list" style="padding-left:20px;line-height:1.8"></ul>
</div>

<div class="card">
  <h2>Metrics</h2>
  <div class="metric-row">
    <div class="metric">
      <span class="metric-value" id="cycles-completed">—</span>
      <span class="metric-label">Cycles Completed</span>
    </div>
    <div class="metric">
      <span class="metric-value" id="last-duration">—</span>
      <span class="metric-label">Last Cycle Duration</span>
    </div>
    <div class="metric">
      <span class="metric-value" id="error-count">—</span>
      <span class="metric-label">Consecutive Errors</span>
    </div>
  </div>
  <div class="daemon-start">
    Daemon started: <strong id="daemon-start-time">—</strong>
  </div>
  <div id="agent-session" style="margin-top:8px;display:none">
    <span class="label">Agent Session:</span><span id="agent-session-id"></span>
  </div>
</div>

<div class="card">
  <h2>Subsystem Health</h2>
  <div id="subsystems-section"><span class="empty-state">Loading...</span></div>
</div>

<div class="card">
  <h2>Recent Cycles</h2>
  <div id="history-section"><span class="empty-state">Loading...</span></div>
</div>

<script>
function phaseClass(phase) {
  if (phase === 'idle') return 'phase-idle';
  if (phase === 'merging') return 'phase-merging';
  if (phase === 'blocked') return 'phase-blocked';
  if (phase === 'recovery') return 'phase-recovery';
  return 'phase-monitoring';
}

function fmtDuration(secs) {
  if (secs === null || secs === undefined) return '—';
  return secs.toFixed(2) + 's';
}

function fmtTime(iso) {
  if (!iso) return '—';
  try { return new Date(iso).toLocaleString(); } catch(e) { return iso; }
}

function renderState(s) {
  // Phase
  var phaseEl = document.getElementById('phase');
  phaseEl.textContent = s.phase_label || s.phase;
  phaseEl.className = 'phase ' + phaseClass(s.phase);

  // Active card
  var cardEl = document.getElementById('card-section');
  if (s.active_card_title) {
    var prPart = s.pr_url && /^https?:\\/\\//i.test(s.pr_url)
      ? '<a href="' + esc(s.pr_url) + '" target="_blank" rel="noopener">Open PR &#8599;</a>'
      : '<span class="label">No PR yet</span>';
    cardEl.innerHTML =
      '<div><span class="label">Title:</span>' + esc(s.active_card_title) + '</div>' +
      '<div style="margin-top:4px"><span class="label">Column:</span>' +
        '<span class="badge badge-required">' + esc(s.active_card_column || '') + '</span>' + prPart + '</div>';
  } else {
    cardEl.innerHTML = '<span class="empty-state">No active card</span>';
  }

  // Open questions (blocked phase)
  var qCard = document.getElementById('questions-card');
  var qList = document.getElementById('questions-list');
  if (s.open_questions && s.open_questions.length > 0) {
    qCard.style.display = '';
    qList.innerHTML = s.open_questions.map(function(q) {
      return '<li>' + esc(q) + '</li>';
    }).join('');
  } else {
    qCard.style.display = 'none';
  }

  // Metrics
  document.getElementById('cycles-completed').textContent = s.cycles_completed;
  document.getElementById('last-duration').textContent = fmtDuration(s.last_cycle_duration_seconds);
  document.getElementById('error-count').textContent = s.consecutive_error_count;
  document.getElementById('daemon-start-time').textContent = fmtTime(s.daemon_start_time);

  // Agent session
  var agentEl = document.getElementById('agent-session');
  if (s.agent_session_id) {
    agentEl.style.display = '';
    document.getElementById('agent-session-id').textContent = s.agent_session_id;
  } else {
    agentEl.style.display = 'none';
  }

  // Subsystems
  var subsEl = document.getElementById('subsystems-section');
  if (!s.subsystems || s.subsystems.length === 0) {
    subsEl.innerHTML = '<span class="empty-state">No subsystems registered</span>';
  } else {
    subsEl.innerHTML = '<table><thead><tr>' +
      '<th>Subsystem</th><th>Status</th><th>Required</th><th>Details</th>' +
      '</tr></thead><tbody>' +
      s.subsystems.map(function(sub) {
        return '<tr>' +
          '<td>' + esc(sub.name) + '</td>' +
          '<td><span class="badge badge-' + sub.status + '">' + sub.status + '</span></td>' +
          '<td><span class="badge ' + (sub.required ? 'badge-required' : 'badge-optional') + '">' +
            (sub.required ? 'required' : 'optional') + '</span></td>' +
          '<td>' + esc(sub.details || '') + '</td>' +
          '</tr>';
      }).join('') +
      '</tbody></table>';
  }

  // Cycle history
  var histEl = document.getElementById('history-section');
  if (!s.cycle_history || s.cycle_history.length === 0) {
    histEl.innerHTML = '<span class="empty-state">No cycles completed yet</span>';
  } else {
    histEl.innerHTML = '<table><thead><tr>' +
      '<th>Time</th><th>Phase</th><th>Duration</th><th>Outcome</th>' +
      '</tr></thead><tbody>' +
      s.cycle_history.map(function(e) {
        return '<tr>' +
          '<td>' + fmtTime(e.timestamp) + '</td>' +
          '<td>' + esc(e.phase.replace(/_/g,' ').replace(/\\b\\w/g,function(c){return c.toUpperCase();})) + '</td>' +
          '<td>' + fmtDuration(e.duration_seconds) + '</td>' +
          '<td><span class="badge badge-' + e.outcome + '">' + e.outcome + '</span></td>' +
          '</tr>';
      }).join('') +
      '</tbody></table>';
  }
}

function esc(s) {
  if (!s) return '';
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

var banner = document.getElementById('disconnected-banner');
var es = new EventSource('events');
es.addEventListener('state_update', function(e) {
  try { renderState(JSON.parse(e.data)); } catch(err) { console.error('parse error', err); }
});
es.onerror = function() { banner.style.display = 'block'; };
es.onopen = function() { banner.style.display = 'none'; };
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# FastAPI app factory
# ---------------------------------------------------------------------------


def create_dashboard_app(
    store: DashboardStore,
    daemon: CoordinareDaemon,
    metrics: CoordinareMetrics,
    health: HealthRegistry,
) -> FastAPI:
    """Create the dashboard FastAPI application.

    Endpoints:
        GET /        — serves the dashboard HTML page
        GET /events  — SSE stream of state_update events
    """
    app = FastAPI(title="coordinare-dashboard")

    @app.middleware("http")
    async def _log_requests(request: Request, call_next):  # type: ignore[no-untyped-def]
        t0 = time.perf_counter()
        response = await call_next(request)
        elapsed_ms = round((time.perf_counter() - t0) * 1000, 2)
        is_sse = request.url.path == "/events"
        log_kwargs: dict = {
            "method": request.method,
            "path": str(request.url.path),
            "status_code": response.status_code,
            "response_time_ms": elapsed_ms,
        }
        if is_sse:
            log_kwargs["streaming"] = True
        _log.info("http_request", **log_kwargs)
        return response

    @app.get("/", response_class=HTMLResponse)
    async def dashboard() -> HTMLResponse:
        return HTMLResponse(_DASHBOARD_HTML)

    @app.get("/events")
    async def sse_events() -> StreamingResponse:
        return StreamingResponse(
            store.sse_stream(daemon, metrics, health),
            media_type="text/event-stream",
        )

    return app


# ---------------------------------------------------------------------------
# Port conflict check (called from __main__ before starting uvicorn)
# ---------------------------------------------------------------------------


def check_port_available(host: str, port: int) -> None:
    """Probe that the dashboard port is available.

    Logs a structured error and calls sys.exit(1) if the port is already in use —
    same fail-fast pattern as StateStore.verify_writable().
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
        except OSError as exc:
            _log.error(
                "dashboard_port_conflict",
                host=host,
                port=port,
                error=str(exc),
            )
            sys.exit(1)
