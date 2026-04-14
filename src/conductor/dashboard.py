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
from typing import TYPE_CHECKING, Any

import structlog
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from pathlib import Path

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

    def shutdown(self) -> None:
        """Wake all SSE generators so they exit cleanly on server shutdown.

        Sends a None sentinel to every subscriber queue.  The sse_stream()
        generator treats None as a stop signal and returns, allowing uvicorn
        to close the connection and proceed with its graceful shutdown.
        """
        for q in list(self._queues):
            with contextlib.suppress(asyncio.QueueFull):
                q.put_nowait(None)


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

    def shutdown(self) -> None:
        """Signal all active SSE streams to exit cleanly."""
        self.broadcaster.shutdown()

    async def sse_stream(
        self,
        daemon: CoordinareDaemon,
        metrics: CoordinareMetrics,
        health: HealthRegistry,
    ) -> AsyncGenerator[str, None]:
        """Async generator for the SSE /events stream.

        Yields the current snapshot immediately on subscribe, then waits for
        broadcaster events with a 15-second keepalive timeout.  A None
        sentinel from broadcaster.shutdown() causes a clean exit.
        """
        q = self.broadcaster.subscribe()
        try:
            # Send current state immediately on connect (FR-011)
            snapshot = self.build_snapshot(daemon, metrics, health)
            yield f"event: state_update\ndata: {json.dumps(snapshot)}\n\n"
            while True:
                try:
                    payload = await asyncio.wait_for(q.get(), timeout=15.0)
                    if payload is None:
                        # Shutdown sentinel — exit the generator cleanly
                        break
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

        card = daemon.state.get("current_card")
        card_dict = card if isinstance(card, dict) else {}
        active_card_issue_url = str(card_dict.get("issue_url", "")) or None

        card_clarifications = list(snapshot.card_clarifications) if snapshot else []
        performer_events = list(daemon.state.get("performer_events") or [])
        performer_metrics = daemon.state.get("performer_metrics")
        performer_backend = str(
            (daemon.state.get("agent_dispatch") or {}).get("backend") or ""
        ) or None

        # Stderr logs from the active performer process (drained continuously
        # by the transport to prevent pipe-buffer blocking)
        agent_service = daemon.state.get("agent_service")
        performer_logs: list[str] = []
        if agent_service is not None and hasattr(agent_service, "get_agent_logs"):
            with contextlib.suppress(Exception):
                performer_logs = agent_service.get_agent_logs()

        # 035: Multi-card parallelism — active session summaries
        active_sessions_raw = daemon.state.get("active_sessions") or {}
        active_session_count = len(active_sessions_raw)
        active_session_summaries = []
        # Coerce performer_stage to "" when None / non-string before
        # stringifying — ``str(None)`` returns the literal "None" which
        # would then show up as a real stage label in the UI and break
        # the stage-progress calculation. Same bug class as the notify
        # and _build_snapshot fixes in this PR.
        for sid, sess in active_sessions_raw.items():
            sess_card = sess.get("current_card") or {}
            _sess_raw_stage = sess.get("performer_stage")
            _sess_stage = _sess_raw_stage if isinstance(_sess_raw_stage, str) else ""
            active_session_summaries.append({
                "card_id": sid,
                "card_title": str(sess_card.get("title", "")),
                "phase": str(sess.get("phase", "idle")),
                "performer_stage": _sess_stage,
                "card_tokens_total": sess.get("card_tokens_total", 0),
                "card_cost_estimate": sess.get("card_cost_estimate", 0.0),
            })

        return {
            "phase": phase,
            "phase_label": format_phase_label(phase),
            "active_card_title": snapshot.active_card_title if snapshot else None,
            "active_card_column": snapshot.active_card_column if snapshot else None,
            "active_card_issue_url": active_card_issue_url,
            "pr_url": snapshot.pr_url if snapshot else None,
            "agent_session_id": snapshot.agent_session_id if snapshot else None,
            "agent_dispatch_at": (
                daemon.state.get("agent_dispatch_at").isoformat()
                if isinstance(daemon.state.get("agent_dispatch_at"), datetime)
                else None
            ),
            "open_questions": list(snapshot.open_questions) if snapshot else [],
            "card_clarifications": card_clarifications,
            "performer_events": performer_events,
            "performer_metrics": performer_metrics,
            "performer_stage": (
                _top_performer_stage
                if isinstance((_top_performer_stage := daemon.state.get("performer_stage")), str)
                else ""
            ),
            "lifecycle_sequence": list(daemon.state.get("lifecycle_sequence") or []),
            "performer_backend": performer_backend,
            "performer_logs": performer_logs,
            "card_tokens_total": daemon.state.get("card_tokens_total", 0),
            "card_cost_estimate": daemon.state.get("card_cost_estimate", 0.0),
            "active_session_count": active_session_count,
            "active_sessions": active_session_summaries,
            "subsystems": subsystems,
            "project_name": getattr(_cfg, "project_name", "") if (_cfg := daemon.state.get("config")) else "",
            "project_board_url": f"https://github.com/orgs/{_cfg.github_org}/projects/{_cfg.github_project_number}" if _cfg else "",
            "cycles_completed": cycles_completed,
            "last_cycle_duration_seconds": self.last_cycle_duration,
            "consecutive_error_count": error_count,
            "daemon_start_time": started_at,
            "cycle_history": list(self.history),
            "cycle_active": daemon._cycle_active,
            "daemon_running": daemon.running,
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
<script src="https://cdn.jsdelivr.net/npm/mermaid@10/dist/mermaid.min.js"></script>
<style>
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: monospace; font-size: 14px; background: #0d1117; color: #c9d1d9; padding: 16px; }
h1 { font-size: 18px; color: #58a6ff; margin-bottom: 16px; }
h2 { font-size: 13px; color: #8b949e; text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 8px; margin-top: 16px; }
.grid { display: grid; grid-template-columns: 1fr; gap: 12px; }
@media (min-width: 900px) {
  .grid { grid-template-columns: 1fr 1fr; }
  .full { grid-column: 1 / -1; }
}
.card { background: #161b22; border: 1px solid #30363d; border-radius: 6px; padding: 12px; }
.phase { font-size: 22px; font-weight: bold; }
.phase-idle { color: #8b949e; }
.phase-dispatching, .phase-monitoring { color: #58a6ff; }
.phase-merging { color: #3fb950; }
.phase-blocked { color: #d29922; }
.phase-recovery { color: #f85149; }
.phase-desc { font-size: 12px; color: #8b949e; margin-top: 4px; }
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
.card-warning { border-color: #d29922 !important; }
.empty-state-warning { color: #d29922; font-weight: bold; padding: 8px 0; }
.metric-row { display: flex; gap: 24px; flex-wrap: wrap; }
.metric { display: flex; flex-direction: column; }
.metric-value { font-size: 20px; font-weight: bold; color: #c9d1d9; }
.metric-label { font-size: 11px; color: #8b949e; }
.daemon-start { font-size: 13px; color: #8b949e; margin-top: 8px; }
.daemon-start strong { color: #d29922; }
.action-btn {
  margin-top: 10px; padding: 5px 12px; font-size: 12px; font-weight: 600;
  background: #21262d; color: #58a6ff; border: 1px solid #30363d;
  border-radius: 6px; cursor: pointer;
}
.action-btn:hover:not(:disabled) { background: #30363d; }
.action-btn:disabled { opacity: 0.45; cursor: not-allowed; }
.action-msg { font-size: 11px; color: #8b949e; margin-top: 4px; min-height: 14px; }
#disconnected-banner {
  display: none; position: fixed; top: 0; left: 0; right: 0;
  background: #4a1f1f; color: #f85149; text-align: center;
  padding: 8px; font-weight: bold; z-index: 999;
}
#flow-chart { overflow-x: auto; }
#flow-chart svg { max-width: 100%; height: auto; }
.qa-round { margin-bottom: 10px; padding: 8px; background: #0d1117; border-radius: 4px; border-left: 3px solid #30363d; }
.qa-round-q { color: #8b949e; font-size: 12px; margin-bottom: 4px; }
.qa-round-q li { margin-left: 16px; line-height: 1.6; }
.qa-round-a { color: #c9d1d9; font-size: 13px; margin-top: 4px; white-space: pre-wrap; }
.session-age { font-size: 12px; color: #58a6ff; margin-top: 6px; }
.ev-progress  { background: #1a2a1a; color: #3fb950; }
.ev-tool_use  { background: #1a2a3a; color: #58a6ff; }
.ev-thinking  { background: #2a2a1a; color: #d29922; }
.ev-cost      { background: #1e1e2e; color: #8b949e; }
.ev-error     { background: #2a1a1a; color: #f85149; }
.ev-output    { background: #1e1e1e; color: #8b949e; }
/* Performers card */
.perf-header { display: flex; align-items: center; gap: 10px; margin-bottom: 10px; flex-wrap: wrap; }
.perf-dot { width: 10px; height: 10px; border-radius: 50%; display: inline-block; flex-shrink: 0; }
.perf-running { background: #3fb950; animation: pulse-dot 1.5s ease-in-out infinite; }
.perf-idle    { background: #8b949e; }
.perf-error   { background: #f85149; }
@keyframes pulse-dot { 0%,100% { opacity: 1; box-shadow: 0 0 0 0 rgba(63,185,80,.5); } 50% { opacity: 0.8; box-shadow: 0 0 0 5px rgba(63,185,80,0); } }
.perf-metrics { display: flex; gap: 20px; flex-wrap: wrap; font-size: 12px; margin-bottom: 10px; padding: 8px; background: #0d1117; border-radius: 4px; }
.perf-metric { display: flex; flex-direction: column; }
.perf-metric-value { font-size: 15px; font-weight: bold; color: #c9d1d9; }
.perf-metric-label { font-size: 10px; color: #8b949e; }
.perf-log-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 4px; font-size: 12px; color: #8b949e; border-top: 1px solid #21262d; padding-top: 8px; }
.perf-log { max-height: 300px; overflow-y: auto; background: #0d1117; border: 1px solid #21262d; border-radius: 4px; font-size: 12px; }
.perf-log-row { display: grid; grid-template-columns: 68px 82px 1fr; gap: 4px; padding: 3px 6px; border-bottom: 1px solid #161b22; align-items: start; }
.perf-log-row:last-child { border-bottom: none; }
.perf-log-time { color: #8b949e; white-space: nowrap; font-size: 11px; padding-top: 2px; }
.perf-log-text { word-break: break-word; color: #c9d1d9; }
.jump-btn { background: #21262d; border: 1px solid #30363d; color: #58a6ff; border-radius: 3px; padding: 2px 8px; cursor: pointer; font-size: 11px; font-family: monospace; }
.perf-list-row { display: flex; align-items: center; gap: 10px; padding: 10px; background: #0d1117; border: 1px solid #21262d; border-radius: 4px; cursor: pointer; transition: border-color .15s; }
.perf-list-row:hover { border-color: #58a6ff; }
.perf-list-chevron { margin-left: auto; color: #8b949e; font-size: 14px; }
.perf-back-btn { background: none; border: none; color: #58a6ff; cursor: pointer; font-size: 13px; font-family: monospace; padding: 0; margin-bottom: 10px; display: flex; align-items: center; gap: 4px; }
</style>
</head>
<body>
<div id="disconnected-banner">&#9888; Disconnected — reconnecting...</div>
<h1>Coordinare Dashboard <span id="project-link" style="font-size:16px;font-weight:normal;color:#8b949e"></span></h1>
<main class="grid">

<div class="card">
  <h2>Phase</h2>
  <div id="phase" class="phase phase-idle">Idle</div>
  <div id="phase-desc" class="phase-desc"></div>
  <div id="session-age" class="session-age" style="display:none"></div>
  <button id="force-poll-btn" class="action-btn" onclick="forcePoll()"
    aria-label="Trigger immediate board poll">Check Board Now</button>
  <div id="force-poll-msg" class="action-msg"></div>
</div>

<div class="card">
  <h2>Active Card</h2>
  <div id="card-section">
    <span class="empty-state">No active card</span>
  </div>
</div>

<div class="card full">
  <h2>Workflow</h2>
  <div id="flow-chart"><span class="empty-state">Loading flowchart...</span></div>
</div>

<div id="performers-card" class="card full" style="display:none">
  <!-- List view: one row per active performer -->
  <div id="perf-list-view">
    <h2>Performers</h2>
    <div id="perf-list"></div>
  </div>
  <!-- Detail view: shown when a performer row is clicked -->
  <div id="perf-detail-view" style="display:none">
    <button class="perf-back-btn" onclick="showPerfList()">&#8592; Performers</button>
    <div class="perf-header">
      <span id="perf-dot" class="perf-dot perf-running"></span>
      <span id="perf-backend" class="badge badge-required">performer</span>
      <span class="label">Session:</span><code id="perf-session" style="font-size:12px;color:#c9d1d9">—</code>
      <span class="label" style="margin-left:8px">Uptime:</span><span id="perf-age" style="color:#58a6ff;font-size:12px">—</span>
    </div>
    <div class="perf-metrics">
      <div class="perf-metric"><span class="perf-metric-value" id="perf-mem">—</span><span class="perf-metric-label">Memory</span></div>
      <div class="perf-metric"><span class="perf-metric-value" id="perf-cpu">—</span><span class="perf-metric-label">CPU</span></div>
      <div class="perf-metric"><span class="perf-metric-value" id="perf-tokens">—</span><span class="perf-metric-label">Tokens</span></div>
      <div class="perf-metric"><span class="perf-metric-value" id="perf-pid">—</span><span class="perf-metric-label">PID</span></div>
    </div>
    <div class="perf-log-header">
      <span>Live Activity Log</span>
      <button id="perf-jump-btn" class="jump-btn" style="display:none" onclick="jumpToLatest()">&#8595; Jump to latest</button>
    </div>
    <div id="perf-log" class="perf-log"><div style="padding:8px;color:#8b949e;font-style:italic">Waiting for events&hellip;</div></div>
    <details id="perf-logs-details" style="margin-top:10px">
      <summary style="cursor:pointer;font-size:12px;color:#8b949e;user-select:none">Process Logs (stderr) <span id="perf-logs-count"></span></summary>
      <div id="perf-logs-jump-wrap" style="display:none;text-align:right;padding:2px 0">
        <button class="jump-btn" onclick="jumpToLatestLogs()">&#8595; Jump to latest</button>
      </div>
      <div id="perf-logs" class="perf-log" style="margin-top:4px;font-family:monospace;font-size:11px"><div style="padding:8px;color:#8b949e;font-style:italic">No logs yet&hellip;</div></div>
    </details>
  </div>
</div>

<div id="questions-card" class="card full" style="display:none">
  <h2>Open Questions</h2>
  <ul id="questions-list" style="padding-left:20px;line-height:1.8"></ul>
</div>

<div id="clarifications-card" class="card full" style="display:none">
  <h2>Clarification History</h2>
  <div id="clarifications-list"></div>
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
  <div class="metric-row" style="margin-top:8px">
    <div class="metric">
      <span class="metric-value" id="card-tokens-total">0</span>
      <span class="metric-label">Card Tokens</span>
    </div>
    <div class="metric">
      <span class="metric-value" id="card-cost-estimate">$0.00</span>
      <span class="metric-label">Estimated Cost</span>
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

<div class="card full">
  <h2>Recent Cycles</h2>
  <div id="history-section"><span class="empty-state">Loading...</span></div>
</div>

<div class="card full">
  <h2>Personas</h2>
  <div id="personas-section"><span class="empty-state">Loading...</span></div>
</div>

</main>

<script>
mermaid.initialize({
  startOnLoad: false,
  theme: 'dark',
  themeVariables: {
    background: '#161b22',
    primaryColor: '#21262d',
    primaryTextColor: '#c9d1d9',
    primaryBorderColor: '#30363d',
    lineColor: '#8b949e',
    secondaryColor: '#161b22',
    tertiaryColor: '#0d1117',
  },
  flowchart: { curve: 'basis', useMaxWidth: true },
});

// Maps a phase to the node ID that should be highlighted
var PHASE_NODE = {
  'idle':             'CB',
  'dispatching':      'AC',
  'monitoring_agent': 'MA',
  'monitoring_performer': 'MA',
  'monitoring_pr':    'MP',
  'merging':          'MR',
  'relay_feedback':   'RF',
  'blocked':          'HB',
  'recovery':         'CB',
};

var _lastRenderedPhase = null;

function buildFlowDef(phase) {
  var active = PHASE_NODE[phase] || '';
  var lines = [
    'graph TD',
    '  CB["check_board"]',
    '  AC["assess_card"]',
    '  DC["dispatch_card"]',
    '  MA["monitor_agent"]',
    '  MP["monitor_pr"]',
    '  MR["merge_pr"]',
    '  RF["relay_feedback"]',
    '  HB["handle_blocked"]',
    '  N["notify"]',
    '  IDLE(["idle — waiting"])',
    '  WAIT1(["wait next cycle"])',
    '  WAIT2(["wait next cycle"])',
    '  DONE1(["done"])',
    '  DONE2(["done"])',
    '  DONE3(["done"])',
    '',
    '  CB -->|"todo"| AC',
    '  CB -->|"in_progress"| MA',
    '  CB -->|"in_review"| MP',
    '  CB -->|"blocked"| HB',
    '  CB -->|"idle"| IDLE',
    '',
    '  AC -->|"sufficient"| DC',
    '  AC -->|"needs info"| HB',
    '',
    '  DC -->|"dispatched"| N',
    '  DC -->|"error"| HB',
    '',
    '  MA -->|"pr opened"| MP',
    '  MA -->|"blocked"| HB',
    '  MA -->|"working"| WAIT1',
    '',
    '  MP -->|"approved"| MR',
    '  MP -->|"changes"| RF',
    '  MP -->|"blocked"| HB',
    '  MP -->|"pending"| WAIT2',
    '',
    '  MR --> N',
    '  HB --> N',
    '  N --> DONE1',
    '  RF --> DONE2',
    '',
    '  classDef default fill:#21262d,stroke:#30363d,color:#c9d1d9',
    '  classDef terminal fill:#0d1117,stroke:#30363d,color:#8b949e,font-style:italic',
    '  classDef active fill:#1f3a5f,stroke:#58a6ff,color:#ffffff,font-weight:bold,stroke-width:2px',
    '  class IDLE,WAIT1,WAIT2,DONE1,DONE2,DONE3 terminal',
  ];
  if (active) lines.push('  class ' + active + ' active');
  return lines.join('\\n');
}

var _renderSeq = 0;
async function updateFlowChart(phase) {
  if (phase === _lastRenderedPhase) return;
  _lastRenderedPhase = phase;
  var seq = ++_renderSeq;
  var def = buildFlowDef(phase);
  try {
    var id = 'fc' + seq;
    var result = await mermaid.render(id, def);
    if (seq === _renderSeq) {
      document.getElementById('flow-chart').innerHTML = result.svg;
    }
  } catch(e) {
    console.error('mermaid render failed', e);
  }
}

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

function fmtAge(iso) {
  if (!iso) return null;
  var secs = Math.floor((Date.now() - new Date(iso).getTime()) / 1000);
  if (secs < 60) return secs + 's';
  var mins = Math.floor(secs / 60);
  if (mins < 60) return mins + 'm ' + (secs % 60) + 's';
  return Math.floor(mins / 60) + 'h ' + (mins % 60) + 'm';
}

function renderState(s) {
  // Phase
  var phaseEl = document.getElementById('phase');
  phaseEl.textContent = s.phase_label || s.phase;
  phaseEl.className = 'phase ' + phaseClass(s.phase);
  var phaseDescriptions = {
    'idle':             'Waiting for a card to enter the TODO column on the GitHub Project board.',
    'running':          'Processing a work cycle.',
    'blocked':          'Work is paused — review the Open Questions below and take action.',
    'dispatching':      'Assessing and dispatching card to the performer agent.',
    'monitoring_agent': 'Performer agent is actively working on the card.',
    'monitoring_performer': 'Performer agent is actively working on the card.',
    'monitoring_pr':    'Waiting for PR review approval.',
    'merging':          'Merging the approved pull request.',
    'relay_feedback':   'Relaying PR review feedback to the performer agent.',
    'recovery':         'A cycle error occurred; the daemon is recovering before retrying.',
  };
  document.getElementById('phase-desc').textContent = phaseDescriptions[s.phase] || '';

  // Session age (shown when monitoring_agent)
  var ageEl = document.getElementById('session-age');
  if ((s.phase === 'monitoring_agent' || s.phase === 'monitoring_performer') && s.agent_dispatch_at) {
    ageEl.style.display = '';
    ageEl.textContent = 'Agent running for: ' + (fmtAge(s.agent_dispatch_at) || '—');
  } else {
    ageEl.style.display = 'none';
  }

  // Flowchart (async)
  updateFlowChart(s.phase);

  // Active card
  var cardEl = document.getElementById('card-section');
  var cardContainer = cardEl.closest('.card');
  if (s.active_card_title) {
    cardContainer.classList.remove('card-warning');
    var prPart = s.pr_url && /^https?:\\/\\//i.test(s.pr_url)
      ? '<a href="' + esc(s.pr_url) + '" target="_blank" rel="noopener">Open PR &#8599;</a>'
      : '<span class="label">No PR yet</span>';
    var issuePart = s.active_card_issue_url && /^https?:\\/\\//i.test(s.active_card_issue_url)
      ? ' <a href="' + esc(s.active_card_issue_url) + '" target="_blank" rel="noopener">View on GitHub &#8599;</a>'
      : '';
    cardEl.innerHTML =
      '<div><span class="label">Title:</span>' + esc(s.active_card_title) + issuePart + '</div>' +
      '<div style="margin-top:4px"><span class="label">Column:</span>' +
        '<span class="badge badge-required">' + esc(s.active_card_column || '') + '</span>' + prPart + '</div>';
  } else if (s.phase === 'idle') {
    cardContainer.classList.add('card-warning');
    cardEl.innerHTML = '<span class="empty-state-warning">&#9888; No cards in the TODO column &mdash; add a card to your GitHub Project board with status <code>TODO</code> to start work.</span>';
  } else {
    cardContainer.classList.remove('card-warning');
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

  // Performers card
  updatePerformers(s);

  // Clarification history
  var clCard = document.getElementById('clarifications-card');
  var clList = document.getElementById('clarifications-list');
  if (s.card_clarifications && s.card_clarifications.length > 0) {
    clCard.style.display = '';
    clList.innerHTML = s.card_clarifications.map(function(round, i) {
      var qs = (round.questions || []).map(function(q) {
        return '<li>' + esc(q) + '</li>';
      }).join('');
      var ans = round.answer ? '<div class="qa-round-a">&#x1F4AC; ' + esc(round.answer) + '</div>' : '';
      return '<div class="qa-round">' +
        '<div style="font-size:11px;color:#8b949e;margin-bottom:4px">Round ' + (i+1) + '</div>' +
        (qs ? '<div class="qa-round-q"><ul>' + qs + '</ul></div>' : '') +
        ans +
        '</div>';
    }).join('');
  } else {
    clCard.style.display = 'none';
  }

  // Metrics
  document.getElementById('cycles-completed').textContent = s.cycles_completed;
  document.getElementById('last-duration').textContent = fmtDuration(s.last_cycle_duration_seconds);
  document.getElementById('error-count').textContent = s.consecutive_error_count;
  document.getElementById('daemon-start-time').textContent = fmtTime(s.daemon_start_time);
  if (s.project_board_url && s.project_name) {
    var linkEl = document.getElementById('project-link');
    linkEl.textContent = '— ';
    var a = document.createElement('a');
    a.href = s.project_board_url;
    a.target = '_blank';
    a.rel = 'noopener';
    a.style.cssText = 'color:#58a6ff;text-decoration:none';
    a.textContent = s.project_name + ' Board';
    linkEl.appendChild(a);
  }
  document.getElementById('card-tokens-total').textContent =
    (s.card_tokens_total || 0).toLocaleString();
  document.getElementById('card-cost-estimate').textContent =
    '$' + (s.card_cost_estimate || 0).toFixed(2);

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

  // Force-poll button state (016-force-poll)
  var fpBtn = document.getElementById('force-poll-btn');
  var fpMsg = document.getElementById('force-poll-msg');
  if (fpBtn) {
    var shouldDisable = s.cycle_active || !s.daemon_running;
    fpBtn.disabled = shouldDisable;
    if (!shouldDisable) fpMsg.textContent = '';
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
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&#39;');
}

function fmtBytes(b) {
  if (b == null) return '—';
  if (b < 1024) return b + ' B';
  if (b < 1048576) return (b/1024).toFixed(0) + ' KB';
  return (b/1048576).toFixed(1) + ' MB';
}

// ---- Performers card ----
var _perfSessionId = null;
var _perfEventCount = 0;
var _perfAutoScroll = true;
var _perfDetailOpen = false;  // true when the detail view is visible

function showPerfList() {
  _perfDetailOpen = false;
  sessionStorage.removeItem('perfDetailOpen');
  document.getElementById('perf-list-view').style.display = '';
  document.getElementById('perf-detail-view').style.display = 'none';
}

function showPerfDetail() {
  _perfDetailOpen = true;
  sessionStorage.setItem('perfDetailOpen', '1');
  document.getElementById('perf-list-view').style.display = 'none';
  document.getElementById('perf-detail-view').style.display = '';
}

document.getElementById('perf-log').addEventListener('scroll', function() {
  var el = this;
  var atBottom = el.scrollTop + el.clientHeight >= el.scrollHeight - 12;
  _perfAutoScroll = atBottom;
  document.getElementById('perf-jump-btn').style.display = atBottom ? 'none' : '';
});

function jumpToLatest() {
  var log = document.getElementById('perf-log');
  log.scrollTop = log.scrollHeight;
  _perfAutoScroll = true;
  document.getElementById('perf-jump-btn').style.display = 'none';
}

var _perfLogsCount = 0;
var _perfLogsAutoScroll = true;

document.getElementById('perf-logs').addEventListener('scroll', function() {
  var el = this;
  _perfLogsAutoScroll = el.scrollTop + el.clientHeight >= el.scrollHeight - 12;
  document.getElementById('perf-logs-jump-wrap').style.display = _perfLogsAutoScroll ? 'none' : '';
});

function jumpToLatestLogs() {
  var el = document.getElementById('perf-logs');
  el.scrollTop = el.scrollHeight;
  _perfLogsAutoScroll = true;
  document.getElementById('perf-logs-jump-wrap').style.display = 'none';
}

function updatePerformerLogs(logs) {
  var logsEl = document.getElementById('perf-logs');
  var newLines = (logs || []).slice(_perfLogsCount);
  if (newLines.length > 0) {
    if (_perfLogsCount === 0) logsEl.innerHTML = '';
    newLines.forEach(function(line) {
      var row = document.createElement('div');
      row.style.cssText = 'padding:1px 6px;border-bottom:1px solid #161b22;word-break:break-all;color:#8b949e;white-space:pre-wrap';
      row.textContent = line;
      logsEl.appendChild(row);
    });
    _perfLogsCount = logs.length;
    if (_perfLogsAutoScroll) logsEl.scrollTop = logsEl.scrollHeight;
  }
  var countEl = document.getElementById('perf-logs-count');
  if (countEl) countEl.textContent = _perfLogsCount > 0 ? '(' + _perfLogsCount + ' lines)' : '';
  document.getElementById('perf-logs-jump-wrap').style.display =
    (!_perfLogsAutoScroll && _perfLogsCount > 0) ? '' : 'none';
}

function updatePerformers(s) {
  var card = document.getElementById('performers-card');
  var isActive = (s.phase === 'monitoring_agent' || s.phase === 'monitoring_performer' || s.phase === 'relay_feedback');
  var events = s.performer_events || [];
  var logs = s.performer_logs || [];

  if (!isActive && events.length === 0 && logs.length === 0) {
    card.style.display = 'none';
    showPerfList();
    return;
  }
  card.style.display = '';

  // Detect session change — reset both logs
  if (s.agent_session_id !== _perfSessionId) {
    _perfSessionId = s.agent_session_id;
    _perfEventCount = 0;
    _perfAutoScroll = true;
    _perfLogsCount = 0;
    _perfLogsAutoScroll = true;
    document.getElementById('perf-log').innerHTML =
      '<div style="padding:8px;color:#8b949e;font-style:italic">Waiting for events&hellip;</div>';
    document.getElementById('perf-logs').innerHTML =
      '<div style="padding:8px;color:#8b949e;font-style:italic">No logs yet&hellip;</div>';
    // Restore detail view if the user had it open before refresh
    if (sessionStorage.getItem('perfDetailOpen')) {
      showPerfDetail();
    } else {
      showPerfList();
    }
  }

  // Always render the list view row
  var backend = s.performer_backend || 'performer';
  var dotCls = isActive ? 'perf-dot perf-running' : 'perf-dot perf-idle';
  var age = (isActive && s.agent_dispatch_at) ? fmtAge(s.agent_dispatch_at) : '—';
  var sessionShort = s.agent_session_id ? s.agent_session_id.slice(0, 8) + '…' : '—';
  var stage = s.performer_stage || '';
  var lifecycle = s.lifecycle_sequence || [];
  var stageIdx = lifecycle.indexOf(stage);
  var stageLabel = stage ? (stage + (stageIdx >= 0 ? ' (' + (stageIdx + 1) + '/' + lifecycle.length + ')' : '')) : '—';
  var listEl = document.getElementById('perf-list');
  listEl.innerHTML =
    '<div class="perf-list-row" onclick="showPerfDetail()">' +
      '<span class="' + dotCls + '"></span>' +
      '<span class="badge badge-required">' + esc(stageLabel) + '</span>' +
      '<span style="font-size:12px;color:#8b949e">' + esc(backend) + '</span>' +
      '<span style="font-size:12px;color:#58a6ff">' + esc(age) + '</span>' +
      '<span class="perf-list-chevron">&#8250;</span>' +
    '</div>';

  // Only update detail view internals when it's open (avoid wasted renders)
  if (!_perfDetailOpen) return;

  // Process logs (stderr drain)
  updatePerformerLogs(logs);

  // Status dot
  var dot = document.getElementById('perf-dot');
  dot.className = 'perf-dot ' + (isActive ? 'perf-running' : 'perf-idle');

  // Stage + Backend badge + session + uptime
  var backend = s.performer_backend || 'performer';
  document.getElementById('perf-backend').textContent = stageLabel + ' — ' + backend;
  document.getElementById('perf-session').textContent = s.agent_session_id || '—';
  document.getElementById('perf-age').textContent =
    (isActive && s.agent_dispatch_at) ? (fmtAge(s.agent_dispatch_at) || '—') : '—';

  // Metrics
  var m = s.performer_metrics || {};
  document.getElementById('perf-mem').textContent = fmtBytes(m.memory_bytes);
  document.getElementById('perf-cpu').textContent =
    m.cpu_percent != null ? m.cpu_percent.toFixed(1) + '%' : '—';
  document.getElementById('perf-tokens').textContent =
    m.tokens_processed != null ? m.tokens_processed.toLocaleString() : '—';
  document.getElementById('perf-pid').textContent = m.pid != null ? String(m.pid) : '—';

  // Append only new events (incremental)
  var log = document.getElementById('perf-log');
  var newEvents = events.slice(_perfEventCount);
  if (newEvents.length > 0) {
    // Clear placeholder if this is the first real event
    if (_perfEventCount === 0) log.innerHTML = '';
    newEvents.forEach(function(ev) {
      var row = document.createElement('div');
      row.className = 'perf-log-row';
      var t = ev.timestamp ? new Date(ev.timestamp).toLocaleTimeString() : '';
      var evType = ev.type || 'output';
      row.innerHTML =
        '<span class="perf-log-time">' + esc(t) + '</span>' +
        '<span class="badge ev-' + esc(evType) + '">' + esc(evType.replace(/_/g,' ')) + '</span>' +
        '<span class="perf-log-text">' + esc(ev.text || '') + '</span>';
      log.appendChild(row);
    });
    _perfEventCount = events.length;
    if (_perfAutoScroll) log.scrollTop = log.scrollHeight;
  }
}

// Refresh session age counter every 10s while monitoring_agent
var _lastState = null;
setInterval(function() {
  if (!_lastState) return;
  if ((_lastState.phase === 'monitoring_agent' || _lastState.phase === 'monitoring_performer') && _lastState.agent_dispatch_at) {
    var age = fmtAge(_lastState.agent_dispatch_at) || '—';
    document.getElementById('session-age').textContent = 'Agent running for: ' + age;
    // Refresh uptime in detail view header
    var ageSpan = document.getElementById('perf-age');
    if (ageSpan) ageSpan.textContent = age;
    // Refresh uptime in list row (re-render cheaply)
    if (!_perfDetailOpen) updatePerformers(_lastState);
  }
}, 10000);

async function forcePoll() {
  var btn = document.getElementById('force-poll-btn');
  var msg = document.getElementById('force-poll-msg');
  btn.disabled = true;
  msg.textContent = '';
  try {
    var res = await fetch('/api/force-poll', { method: 'POST' });
    if (res.status === 409) {
      msg.textContent = 'Cycle already running';
      btn.disabled = false;
    }
    // 202: stay disabled until SSE delivers cycle_active=false
  } catch(err) {
    msg.textContent = 'Could not reach server';
    btn.disabled = false;
  }
}

// ---------------------------------------------------------------------------
// Personas panel (018-performer-personas)
// ---------------------------------------------------------------------------

async function loadPersonas() {
  var section = document.getElementById('personas-section');
  if (!section) return;
  try {
    var res = await fetch('/api/personas');
    if (!res.ok) {
      section.innerHTML = '<span class="empty-state">Could not load personas: ' + esc(res.status + ' ' + res.statusText) + '</span>';
      return;
    }
    var personas = await res.json();
    renderPersonas(personas);
  } catch(err) {
    section.innerHTML = '<span class="empty-state">Could not load personas: ' + esc(String(err)) + '</span>';
  }
}

function renderPersonas(personas) {
  var section = document.getElementById('personas-section');
  if (!section) return;
  var rows = personas.map(function(p) {
    var badge = p.is_default
      ? '<span style="font-size:11px;color:#8b949e;margin-left:6px">using default</span>'
      : '<span style="font-size:11px;color:#58a6ff;margin-left:6px">custom</span>';
    return '<div style="margin-bottom:16px;border-bottom:1px solid #21262d;padding-bottom:14px">' +
      '<div style="display:flex;align-items:center;margin-bottom:6px">' +
        '<strong style="font-size:13px">' + esc(p.role) + '</strong>' + badge +
      '</div>' +
      '<textarea id="persona-ta-' + esc(p.role) + '" rows="4" style="width:100%;box-sizing:border-box;' +
        'background:#0d1117;border:1px solid #30363d;color:#c9d1d9;padding:6px;font-family:monospace;' +
        'font-size:12px;border-radius:4px;resize:vertical">' + esc(p.instructions) + '</textarea>' +
      '<div style="margin-top:6px;display:flex;gap:8px;align-items:center">' +
        '<button onclick="savePersona(\\\'' + esc(p.role) + '\\\')" ' +
          'id="persona-save-' + esc(p.role) + '" ' +
          'style="background:#238636;border:none;color:#fff;padding:4px 12px;border-radius:4px;cursor:pointer">' +
          'Save</button>' +
        '<button onclick="resetPersona(\\\'' + esc(p.role) + '\\\')" ' +
          'id="persona-reset-' + esc(p.role) + '" ' +
          'style="background:#21262d;border:1px solid #30363d;color:#c9d1d9;padding:4px 12px;' +
          'border-radius:4px;cursor:pointer">Reset to defaults</button>' +
        '<span id="persona-msg-' + esc(p.role) + '" style="font-size:12px;color:#8b949e"></span>' +
      '</div>' +
    '</div>';
  }).join('');
  section.innerHTML = rows || '<span class="empty-state">No personas found.</span>';
}

async function savePersona(role) {
  var ta = document.getElementById('persona-ta-' + role);
  var btn = document.getElementById('persona-save-' + role);
  var msg = document.getElementById('persona-msg-' + role);
  if (!ta || !btn) return;
  btn.disabled = true;
  msg.textContent = 'Saving...';
  try {
    var res = await fetch('/api/personas/' + encodeURIComponent(role), {
      method: 'PUT',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({instructions: ta.value}),
    });
    var data = res.headers.get('content-type') && res.headers.get('content-type').includes('application/json')
      ? await res.json() : {};
    if (!res.ok) {
      msg.textContent = 'Error: ' + (data.error || res.statusText || res.status);
      msg.style.color = '#f85149';
    } else {
      msg.textContent = 'Saved.';
      msg.style.color = '#3fb950';
      setTimeout(function() { if (msg) { msg.textContent = ''; msg.style.color = '#8b949e'; }}, 3000);
      loadPersonas();
    }
  } catch(err) {
    msg.textContent = 'Network error';
    msg.style.color = '#f85149';
  }
  btn.disabled = false;
}

async function resetPersona(role) {
  var btn = document.getElementById('persona-reset-' + role);
  var msg = document.getElementById('persona-msg-' + role);
  if (!btn) return;
  btn.disabled = true;
  msg.textContent = 'Resetting...';
  try {
    var res = await fetch('/api/personas/' + encodeURIComponent(role), {method: 'DELETE'});
    if (res.status === 204 || res.ok) {
      msg.textContent = 'Reset to defaults.';
      msg.style.color = '#3fb950';
      setTimeout(function() { if (msg) { msg.textContent = ''; msg.style.color = '#8b949e'; }}, 3000);
      loadPersonas();
    } else {
      var data = await res.json().catch(function() { return {}; });
      msg.textContent = 'Error: ' + (data.error || res.status);
      msg.style.color = '#f85149';
    }
  } catch(err) {
    msg.textContent = 'Network error';
    msg.style.color = '#f85149';
  }
  btn.disabled = false;
}

// Load personas on page load
loadPersonas();

var banner = document.getElementById('disconnected-banner');
var es = new EventSource('events');
es.addEventListener('state_update', function(e) {
  try {
    _lastState = JSON.parse(e.data);
    renderState(_lastState);
  } catch(err) { console.error('parse error', err); }
});
es.onerror = function() {
  banner.style.display = 'block';
  var fpBtn = document.getElementById('force-poll-btn');
  if (fpBtn) fpBtn.disabled = true;
};
es.onopen = function() { banner.style.display = 'none'; };
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# 031 — Active phases for override validation
# ---------------------------------------------------------------------------
_ACTIVE_PHASES = {"monitoring_performer", "monitoring_agent", "dispatching"}

# ---------------------------------------------------------------------------
# FastAPI app factory
# ---------------------------------------------------------------------------


def create_dashboard_app(
    store: DashboardStore,
    daemon: CoordinareDaemon,
    metrics: CoordinareMetrics,
    health: HealthRegistry,
    config_path: Path | None = None,
) -> FastAPI:
    """Create the dashboard FastAPI application.

    Endpoints:
        GET /        — serves the dashboard HTML page
        GET /events  — SSE stream of state_update events
        GET /api/personas          — list all role personas (018)
        PUT /api/personas/{role}   — update persona for a role (018)
        DELETE /api/personas/{role} — reset persona to defaults (018)
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

    @app.get("/api/performer-logs")
    async def performer_logs_stream() -> StreamingResponse:
        """Stream the active performer's stderr log buffer, then tail new lines.

        Sends all buffered lines immediately, then polls every second and pushes
        any new lines until the client disconnects.  Plain text, one line per row.
        """
        async def _generate() -> AsyncGenerator[str, None]:
            agent_service = daemon.state.get("agent_service")
            getter = getattr(agent_service, "get_agent_logs", None)
            if not callable(getter):
                yield "no performer active (agent_service does not support log buffering)\n"
                return
            sent = 0
            while True:
                logs: list[str] = getter()
                new = logs[sent:]
                for line in new:
                    yield line + "\n"
                sent = len(logs)
                await asyncio.sleep(1.0)

        return StreamingResponse(_generate(), media_type="text/plain")

    @app.post("/api/force-poll")
    async def force_poll() -> JSONResponse:
        """Trigger an immediate board poll cycle (016-force-poll).

        Returns 202 and fires the daemon's webhook_trigger when idle.
        Returns 409 when a cycle is already in progress.
        """
        if daemon._cycle_active:
            return JSONResponse({"status": "cycle_in_progress"}, status_code=409)
        daemon._webhook_trigger.set()
        return JSONResponse({"status": "accepted"}, status_code=202)

    @app.post("/api/cancel")
    async def cancel_card() -> JSONResponse:
        """Cancel the currently active card (026-card-cancellation).

        Stops the performer, cleans up workspace, moves card to TODO.
        Returns 200 with cancellation result. Returns 409 if a cycle is active.
        """
        if daemon._cycle_active:
            return JSONResponse({"status": "cycle_in_progress"}, status_code=409)

        from coordinare.cancel import cancel_active_card

        result = await cancel_active_card(daemon.state)
        return JSONResponse(result)

    @app.get("/events")
    async def sse_events() -> StreamingResponse:
        return StreamingResponse(
            store.sse_stream(daemon, metrics, health),
            media_type="text/event-stream",
        )

    # -----------------------------------------------------------------------
    # 031 — Human Override Controls API endpoints
    # -----------------------------------------------------------------------

    @app.post("/api/skip-role")
    async def skip_role() -> JSONResponse:
        """Queue a skip-role override for the next graph cycle (031)."""
        if daemon.state.get("phase") not in _ACTIVE_PHASES:
            return JSONResponse({"error": "No active card to override"}, status_code=400)
        daemon.state["pending_override"] = {"action": "skip"}
        return JSONResponse({"status": "override_queued", "action": "skip"})

    @app.post("/api/restart-from/{role}")
    async def restart_from(role: str) -> JSONResponse:
        """Queue a restart-from override for the next graph cycle (031).

        Accepts both role nouns (e.g. ``architect``) and stage names
        (e.g. ``architecting``).
        """
        if daemon.state.get("phase") not in _ACTIVE_PHASES:
            return JSONResponse({"error": "No active card to override"}, status_code=400)
        lifecycle = list(daemon.state.get("lifecycle_sequence") or [])
        # Accept role nouns (architect) as well as stage names (architecting)
        from coordinare.graph.nodes.classify_human_feedback import _resolve_stage
        resolved = _resolve_stage(role, lifecycle)
        if resolved not in lifecycle:
            return JSONResponse(
                {"error": f"Role {role!r} not in lifecycle: {lifecycle}"},
                status_code=400,
            )
        daemon.state["pending_override"] = {"action": "restart", "target_stage": resolved}
        return JSONResponse({"status": "override_queued", "action": "restart", "target_stage": resolved})

    @app.post("/api/veto")
    async def veto() -> JSONResponse:
        """Queue a veto override for the next graph cycle (031)."""
        if daemon.state.get("phase") not in _ACTIVE_PHASES:
            return JSONResponse({"error": "No active card to override"}, status_code=400)
        daemon.state["pending_override"] = {"action": "veto"}
        return JSONResponse({"status": "override_queued", "action": "veto"})

    # -----------------------------------------------------------------------
    # 018 — Personas API endpoints
    # -----------------------------------------------------------------------

    @app.get("/api/personas")
    async def get_personas() -> JSONResponse:
        """Return effective instructions and is_default flag for all 8 roles.

        Reads config_path on every request for hot-reload behaviour.
        Falls back to the daemon's last-known config so the UI reflects the
        effective personas actually in use rather than bare defaults.
        """
        from coordinare.services.persona_service import (
            VALID_ROLES,
            get_effective_instructions,
            load_personas_hot,
        )

        personas = load_personas_hot(config_path, daemon.state.get("config"))

        result: list[dict[str, Any]] = []
        for role in sorted(VALID_ROLES):
            instructions = get_effective_instructions(role, personas)
            is_default = not getattr(personas, role).instructions.strip()
            result.append({"role": role, "instructions": instructions, "is_default": is_default})
        return JSONResponse(result)

    @app.put("/api/personas/{role}")
    async def update_persona(role: str, request: Request) -> JSONResponse:
        """Update persona instructions for a role. Returns 404 for unknown roles, 400 for oversized instructions."""
        from coordinare.config import PERSONA_MAX_LENGTH
        from coordinare.services.persona_service import (
            VALID_ROLES,
            save_persona,
        )

        if role not in VALID_ROLES:
            return JSONResponse({"error": f"Unknown role: {role!r}"}, status_code=404)

        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

        if not isinstance(body, dict):
            return JSONResponse({"error": "Request body must be a JSON object"}, status_code=400)

        if "instructions" not in body:
            return JSONResponse({"error": "Missing required field: instructions"}, status_code=400)
        raw_instructions = body["instructions"]
        if not isinstance(raw_instructions, str):
            return JSONResponse({"error": "instructions must be a string"}, status_code=400)
        instructions: str = raw_instructions
        if len(instructions) > PERSONA_MAX_LENGTH * 2 or len(instructions.strip()) > PERSONA_MAX_LENGTH:
            return JSONResponse(
                {"error": f"Instructions exceed maximum length ({PERSONA_MAX_LENGTH} chars)"},
                status_code=400,
            )

        if config_path is None or not config_path.is_file():
            return JSONResponse({"error": "Config file not found"}, status_code=500)

        try:
            save_persona(role, instructions, config_path)
        except ValueError as exc:
            # ValueError here means malformed config shape (role/length already
            # validated above); treat as validation error per API contract.
            return JSONResponse({"error": str(exc)}, status_code=400)
        except OSError as exc:
            return JSONResponse({"error": f"Failed to write config: {exc}"}, status_code=500)

        # Re-read from config to return what's actually stored/effective.
        from coordinare.services.persona_service import get_effective_instructions, load_personas_hot

        personas = load_personas_hot(config_path, daemon.state.get("config"))
        effective = get_effective_instructions(role, personas)
        is_default = not getattr(personas, role).instructions.strip()
        return JSONResponse({"role": role, "instructions": effective, "is_default": is_default})

    # -----------------------------------------------------------------------
    # 038 — Dry-Run Mode API endpoint
    # -----------------------------------------------------------------------

    @app.post("/api/dry-run/{card_id}")
    async def dry_run_card(card_id: str) -> JSONResponse:
        """Run a dry-run preview for a card and return the result as JSON (038).

        Builds the lifecycle plan without making any GitHub API calls or
        spawning any performers.
        """
        from coordinare.dry_run import execute_dry_run

        cfg = daemon.state.get("config")
        if cfg is None:
            return JSONResponse({"error": "Config not available"}, status_code=500)

        try:
            result = await execute_dry_run(card_id, cfg)
        except Exception:
            _log.exception("dry_run.api_error", card_id=card_id)
            return JSONResponse({"error": "Internal error during dry-run"}, status_code=500)

        return JSONResponse(result.model_dump())

    @app.delete("/api/personas/{role}", status_code=204)
    async def reset_persona_endpoint(role: str) -> Response:
        """Reset a role's persona to built-in defaults (clears custom instructions)."""
        from coordinare.services.persona_service import VALID_ROLES, reset_persona

        if role not in VALID_ROLES:
            return JSONResponse({"error": f"Unknown role: {role!r}"}, status_code=404)

        if config_path is None or not config_path.is_file():
            return JSONResponse({"error": "Config file not found"}, status_code=500)

        try:
            reset_persona(role, config_path)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except OSError as exc:
            return JSONResponse({"error": f"Failed to write config: {exc}"}, status_code=500)

        return Response(status_code=204)

    return app


# ---------------------------------------------------------------------------
# Port conflict check (called from __main__ before starting uvicorn)
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Webhook support (015)
# ---------------------------------------------------------------------------


def verify_github_signature(body: bytes, secret: str, header: str | None) -> bool:
    """Return True iff the X-Hub-Signature-256 header matches the HMAC-SHA256 of body.

    Uses stdlib hmac + hashlib; constant-time comparison via hmac.compare_digest.
    Returns False (never raises) when header is missing or malformed.
    """
    import hashlib
    import hmac

    if not header:
        return False
    if not header.startswith("sha256="):
        return False
    expected_sig = "sha256=" + hmac.new(
        secret.encode(), body, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected_sig, header)


def register_webhook_route(
    app: FastAPI,
    path: str,
    secret: str,
    trigger: asyncio.Event,
) -> None:
    """Register a POST route on *app* that validates GitHub webhook delivery.

    Valid deliveries set *trigger* and return HTTP 200 ``{"status": "ok"}``.
    Invalid/missing signatures return HTTP 401 and are structured-logged.
    """
    from fastapi import Response

    @app.post(path, include_in_schema=False)
    async def _webhook_handler(request: Request) -> Response:
        body = await request.body()
        sig_header = request.headers.get("X-Hub-Signature-256")
        if not verify_github_signature(body, secret, sig_header):
            _log.warning(
                "webhook_signature_invalid",
                path=path,
                sig_header=sig_header,
            )
            return Response(content='{"error":"invalid signature"}', status_code=401, media_type="application/json")
        trigger.set()
        _log.info("webhook_received", path=path)
        return JSONResponse({"status": "ok"})


def check_port_available(host: str, port: int, *, label: str = "server") -> None:
    """Probe that a TCP port is available before starting a uvicorn server.

    Prints a short human-readable error and calls sys.exit(1) if the port is
    already in use — avoids the full uvicorn/asyncio traceback that would
    otherwise appear.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
        except OSError:
            _log.error(
                "dashboard_port_conflict",
                label=label,
                host=host,
                port=port,
                hint=f"Address {host}:{port} is already in use. "
                     "Stop the process holding that port and try again.",
            )
            sys.exit(1)
