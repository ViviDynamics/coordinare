"""Live web dashboard for the coordinare daemon (010).

Serves a single-page dashboard at / and an SSE stream at /events.
No new dependencies — uses FastAPI/Starlette StreamingResponse (already present).
"""
from __future__ import annotations

import asyncio
import contextlib
import copy
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


def _json_default(o: Any) -> Any:
    """JSON encoder fallback for non-serializable types appearing in snapshots.

    Session dicts carry ``set``-typed fields (processed_issue_comment_ids,
    processed_review_ids, advocate_history); coerce to a sorted list when
    elements are orderable, otherwise plain list.
    """
    if isinstance(o, set):
        try:
            return sorted(o)
        except TypeError:
            return list(o)
    if isinstance(o, datetime):
        return o.isoformat()
    from pathlib import PurePath
    if isinstance(o, PurePath):
        return str(o)
    raise TypeError(f"Object of type {type(o).__name__} is not JSON serializable")


def format_phase_label(phase: str) -> str:
    """Convert a raw phase string to a human-readable label.

    Examples:
        "monitoring_agent" -> "Monitoring Agent"
        "relay_feedback"   -> "Relay Feedback"
        "idle"             -> "Idle"
    """
    return phase.replace("_", " ").title()


def is_session_stale(agent_dispatch_at_iso: str | None, threshold_minutes: int = 30) -> bool:
    """Return True if the session has been running longer than threshold_minutes."""
    if not agent_dispatch_at_iso:
        return False
    try:
        dispatched = datetime.fromisoformat(agent_dispatch_at_iso.replace("Z", "+00:00"))
        elapsed = datetime.now(UTC) - dispatched
        return elapsed.total_seconds() > threshold_minutes * 60
    except (ValueError, TypeError):
        return False


def compute_overall_health(subsystems: list[dict]) -> str:
    """Return overall health of required subsystems.

    Returns "healthy", "degraded", or "unavailable".
    Non-required subsystems are excluded from the computation.
    """
    required = [s for s in subsystems if s.get("required")]
    # Empty required list (no required subsystems) is intentionally "healthy"
    if not required or all(s.get("status") == "healthy" for s in required):
        return "healthy"
    if any(s.get("status") == "unavailable" for s in required):
        return "unavailable"
    return "degraded"


# ---------------------------------------------------------------------------
# Performer pool widget (spec 056, T040)
# ---------------------------------------------------------------------------


def render_performer_pool_widget(pool: Any) -> dict[str, Any]:
    """Render performer pool state for the dashboard snapshot.

    Args:
        pool: the PerformerPool instance, or None if not available.

    Returns:
        A dict with performer registrations and metadata, suitable for JSON
        serialization and inclusion in the dashboard state snapshot.
    """
    if pool is None:
        return {
            "performers": [],
            "total_registered": 0,
            "total_excluded": 0,
            "total_idle": 0,
            "total_busy": 0,
        }

    performers = []
    total_excluded = 0
    total_idle = 0
    total_busy = 0

    for state in pool.list_all():
        is_excluded = state.excluded_until_recovery
        if is_excluded:
            total_excluded += 1

        if state.availability == "idle":
            total_idle += 1
        elif state.availability == "busy":
            total_busy += 1

        performer_entry = {
            "id": state.id,
            "mode": state.mode,
            "availability": state.availability,
            "endpoint": str(state.endpoint) if state.endpoint else None,
            "current_job_id": state.current_job_id,
            "capabilities": (
                {
                    "backends": state.capabilities.backends,
                    "tool_flags": state.capabilities.tool_flags,
                }
                if state.capabilities
                else None
            ),
            "consecutive_failures": state.consecutive_failures,
            "excluded_until_recovery": is_excluded,
            "last_status_at": (
                state.last_status_at.isoformat()
                if state.last_status_at
                else None
            ),
        }
        performers.append(performer_entry)

    return {
        "performers": performers,
        "total_registered": len(performers),
        "total_excluded": total_excluded,
        "total_idle": total_idle,
        "total_busy": total_busy,
    }


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
        # 065 US1: mid-cycle active_sessions watcher. The daemon only broadcasts
        # at cycle end (daemon.py:1655), so mutations made by in-cycle code
        # (kickbacks, stage advances, new dispatches) would otherwise be
        # invisible to SSE subscribers for up to a full cycle. A single shared
        # poll-and-fingerprint task started on first subscribe makes any
        # mutation observable within ~100 ms without requiring every call site
        # to notify explicitly.
        self._watcher_task: asyncio.Task | None = None
        self._watcher_fingerprint: tuple | None = None

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
        if self._watcher_task is not None and not self._watcher_task.done():
            self._watcher_task.cancel()
            self._watcher_task = None

    @staticmethod
    def _active_sessions_fingerprint(daemon: CoordinareDaemon) -> tuple:
        """Identity-changing fields for each active session.

        Compared between watcher ticks to decide whether a broadcast is needed.
        Includes the fields the Active Performers panel renders (title, stage,
        phase, urls) plus dispatch identity (container_id) so a re-dispatch
        of the same card_id still trips a broadcast.
        """
        active = daemon.state.get("active_sessions") or {}
        parts: list[tuple] = []
        for sid in sorted(active.keys()):
            sess = active[sid] or {}
            card = sess.get("current_card") or {}
            dispatch = sess.get("agent_dispatch") or {}
            parts.append((
                sid,
                card.get("title"),
                card.get("issue_url"),
                card.get("pr_url"),
                sess.get("phase"),
                sess.get("performer_stage"),
                dispatch.get("container_id"),
            ))
        return tuple(parts)

    async def _watch_active_sessions(
        self,
        daemon: CoordinareDaemon,
        metrics: CoordinareMetrics,
        health: HealthRegistry,
    ) -> None:
        """Poll active_sessions at 100 ms; broadcast when the fingerprint changes.

        ~100 ms is well under the 1 s contract in
        ``test_065_active_performer_staleness`` and small relative to user
        perception of a "live" panel. Snapshot building only happens on change,
        so an idle daemon costs one dict comparison per tick.

        The initial fingerprint must be captured by the caller *before*
        scheduling this task — otherwise a mutation that lands between
        ``create_task`` and the first event-loop turn would be silently
        baked into the baseline and never trigger a broadcast.
        """
        try:
            while True:
                await asyncio.sleep(0.1)
                fp = self._active_sessions_fingerprint(daemon)
                if fp == self._watcher_fingerprint:
                    continue
                self._watcher_fingerprint = fp
                with contextlib.suppress(Exception):
                    self.broadcaster.broadcast(
                        self.build_snapshot(daemon, metrics, health)
                    )
        except asyncio.CancelledError:
            return

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
        # 065 US1: lazily start the active_sessions watcher on first subscribe
        # so mid-cycle mutations are broadcast without waiting for cycle end.
        if self._watcher_task is None or self._watcher_task.done():
            # Capture baseline synchronously — see _watch_active_sessions docstring.
            self._watcher_fingerprint = self._active_sessions_fingerprint(daemon)
            self._watcher_task = asyncio.create_task(
                self._watch_active_sessions(daemon, metrics, health)
            )
        try:
            # Send current state immediately on connect (FR-011)
            snapshot = self.build_snapshot(daemon, metrics, health)
            yield f"event: state_update\ndata: {json.dumps(snapshot, default=_json_default)}\n\n"
            while True:
                try:
                    payload = await asyncio.wait_for(q.get(), timeout=15.0)
                    if payload is None:
                        # Shutdown sentinel — exit the generator cleanly
                        break
                    yield f"event: state_update\ndata: {json.dumps(payload, default=_json_default)}\n\n"
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
        # Aggregate board snapshots: top-level first, then symphony states as fallback.
        # When coordinare runs in symphony mode it has no top-level board of its own,
        # so we must fold symphony board_snapshots together to get accurate counts.
        _top_board = daemon.state.get("board_snapshot")
        if not _top_board:
            _agg_board: dict[str, list] = {}
            for _ss in (daemon.state.get("symphony_states") or {}).values():
                _sb = getattr(_ss, "board_snapshot", None)
                if isinstance(_sb, dict):
                    for _col, _items in _sb.items():
                        if isinstance(_items, list):
                            _agg_board.setdefault(_col, []).extend(_items)
            # Use None (not {}) so _build_board_summary treats missing board correctly.
            _top_board = _agg_board or None
        board_summary = self._build_board_summary(_top_board)
        # Prefer top-level last_poll_at; fall back to the most-recent symphony poll.
        # The outer `is None` guard means last_poll_at_raw starts as None in the loop
        # and is only ever set to a datetime (isinstance check on _sp), so the
        # `_sp > last_poll_at_raw` comparison is always datetime vs datetime — safe.
        last_poll_at_raw = daemon.state.get("last_poll_at")
        if last_poll_at_raw is None:
            for _ss in (daemon.state.get("symphony_states") or {}).values():
                _sp = getattr(_ss, "last_poll_at", None)
                if isinstance(_sp, datetime) and (last_poll_at_raw is None or _sp > last_poll_at_raw):
                    last_poll_at_raw = _sp
        last_poll_at = (
            last_poll_at_raw.isoformat()
            if isinstance(last_poll_at_raw, datetime)
            else (str(last_poll_at_raw) if last_poll_at_raw else None)
        )
        # Coerce performer_stage to "" when None / non-string before
        # stringifying — ``str(None)`` returns the literal "None" which
        # would then show up as a real stage label in the UI and break
        # the stage-progress calculation. Same bug class as the notify
        # and _build_snapshot fixes in this PR.
        for sid, sess in active_sessions_raw.items():
            sess_card = sess.get("current_card") or {}
            _sess_raw_stage = sess.get("performer_stage")
            _sess_stage = _sess_raw_stage if isinstance(_sess_raw_stage, str) else ""
            _sess_dispatch = sess.get("agent_dispatch_at")
            _sess_agent_dispatch = sess.get("agent_dispatch") or {}
            active_session_summaries.append({
                "card_id": sid,
                "card_title": str(sess_card.get("title", "")),
                "issue_number": sess_card.get("issue_number"),
                "issue_url": str(sess_card.get("issue_url", "")) or None,
                "pr_url": str(sess_card.get("pr_url", "")) or None,
                "phase": str(sess.get("phase", "idle")),
                "performer_stage": _sess_stage,
                "card_tokens_total": sess.get("card_tokens_total", 0),
                "card_cost_estimate": sess.get("card_cost_estimate", 0.0),
                "feedback_cycle_count": int(sess.get("feedback_cycle_count") or 0),
                "total_feedback_cycles": int(sess.get("total_feedback_cycles") or 0),
                "triage_blocks": int(sess.get("triage_blocks") or 0),
                "agent_dispatch_at": (
                    _sess_dispatch.isoformat()
                    if isinstance(_sess_dispatch, datetime)
                    else None
                ),
                "container_id": _sess_agent_dispatch.get("container_id"),
            })
        # Single-session mode compatibility: when multi-card ``active_sessions``
        # is empty but we're actively monitoring a performer, synthesize one
        # summary row from top-level state so the dashboard doesn't show
        # "No active performers" during in-flight work.
        if not active_session_summaries and phase in {
            "monitoring_agent",
            "monitoring_performer",
            "monitoring_pr",   # performer pushed a PR and is now watching CI/review
            "relay_feedback",
        }:
            _top_stage_raw = daemon.state.get("performer_stage")
            _top_stage = _top_stage_raw if isinstance(_top_stage_raw, str) else ""
            _top_dispatch = daemon.state.get("agent_dispatch_at")
            _dispatch = daemon.state.get("agent_dispatch") or {}
            _top_card_id = str(card_dict.get("id") or _dispatch.get("session_id") or "active")
            _top_card_title = str(card_dict.get("title") or (snapshot.active_card_title if snapshot else "") or "")
            active_session_summaries.append({
                "card_id": _top_card_id,
                "card_title": _top_card_title,
                "issue_number": card_dict.get("issue_number"),
                "issue_url": str(card_dict.get("issue_url", "")) or None,
                "pr_url": str(card_dict.get("pr_url", "")) or (snapshot.pr_url if snapshot else None),
                "phase": phase,
                "performer_stage": _top_stage,
                "card_tokens_total": daemon.state.get("card_tokens_total", 0),
                "card_cost_estimate": daemon.state.get("card_cost_estimate", 0.0),
                "feedback_cycle_count": int(daemon.state.get("feedback_cycle_count") or 0),
                "total_feedback_cycles": int(daemon.state.get("total_feedback_cycles") or 0),
                "triage_blocks": int(daemon.state.get("triage_blocks") or 0),
                "agent_dispatch_at": (
                    _top_dispatch.isoformat()
                    if isinstance(_top_dispatch, datetime)
                    else None
                ),
                "container_id": _dispatch.get("container_id"),
            })
        active_session_count = len(active_session_summaries)

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
            "overall_health": compute_overall_health(subsystems),
            "project_name": getattr(_cfg, "project_name", "") if (_cfg := daemon.state.get("config")) else "",
            "project_board_url": f"https://github.com/orgs/{_cfg.github_org}/projects/{_cfg.github_project_number}" if _cfg else "",
            "cycles_completed": cycles_completed,
            "last_cycle_duration_seconds": self.last_cycle_duration,
            "consecutive_error_count": error_count,
            "daemon_start_time": started_at,
            "cycle_history": list(self.history),
            "cycle_active": daemon._cycle_active,
            "daemon_running": daemon.running,
            # 046: Dependency state for the current card — list of blocker dicts
            # consumed by the dashboard JS to render "Blocked by #N (COLUMN)" badges.
            "blocked_by_dependencies": list(daemon.state.get("blocked_by_dependencies") or []),
            # 047: Last rebase round for per-card rebase status display.
            "last_rebase_round": daemon.state.get("last_rebase_round"),
            # 048: Per-role performer utilization for scaling visibility.
            "role_utilization": self._build_role_utilization(daemon),
            # 050: Active assignee filter for dashboard idle-state hint.
            "assignee_filter": getattr(daemon.state.get("config"), "assignee_filter", None),
            # 053: Idle observability summary from board snapshot + poll timing.
            "board_summary": board_summary,
            "last_poll_at": last_poll_at,
            # 052: Backend transparency — live URL and session stats.
            "backend_ui_url": daemon.state.get("backend_ui_url"),
            "session_stats": self._serialise_session_stats(daemon.state.get("session_stats")),
            # 054: Per-cycle skip reasons for ineligible sessions.
            "session_skip_reasons": copy.deepcopy(daemon.state.get("session_skip_reasons") or {}),
            # 057: Multi-symphony orchestration
            "symphonies": self._build_symphonies_data(daemon),
            "coordinare": {
                "current_symphony": daemon.state.get("current_symphony"),
                "config_version": daemon.state.get("config_version", 0),
            },
        }

    @staticmethod
    def _serialise_session_stats(stats: Any) -> dict | None:
        """Convert a SessionStats dataclass to a JSON-serialisable dict, or None."""
        if stats is None:
            return None
        try:
            return {
                "title": getattr(stats, "title", None),
                "files_changed": getattr(stats, "files_changed", 0),
                "lines_added": getattr(stats, "lines_added", 0),
                "lines_removed": getattr(stats, "lines_removed", 0),
            }
        except Exception:
            return None

    @staticmethod
    def _build_role_utilization(daemon: Any) -> list[dict]:
        """Build per-role utilization with queued counts from active_sessions."""
        slot_mgr = daemon.state.get("slot_manager")
        if slot_mgr is None or not hasattr(slot_mgr, "utilization"):
            return []
        # Count cards in dispatching phase per performer_stage
        queued: dict[str, int] = {}
        for session in (daemon.state.get("active_sessions") or {}).values():
            if isinstance(session, dict) and session.get("phase") == "dispatching":
                stage = session.get("performer_stage", "")
                if stage:
                    queued[stage] = queued.get(stage, 0) + 1
        return slot_mgr.utilization(queued_by_stage=queued)

    @staticmethod
    def _build_board_summary(board_snapshot: Any) -> dict[str, int]:
        """Derive stable board column counts from state.board_snapshot."""
        snapshot = board_snapshot if isinstance(board_snapshot, dict) else {}
        columns = ("TODO", "IN_PROGRESS", "IN_REVIEW", "DONE", "BLOCKED", "BACKLOG")
        summary: dict[str, int] = {}
        for column in columns:
            value = snapshot.get(column)
            summary[column] = len(value) if isinstance(value, list) else 0
        return summary

    @staticmethod
    def _build_symphonies_data(daemon: Any) -> list[dict]:
        """Build symphony state for the dashboard snapshot (spec 057)."""
        symphony_configs = daemon.state.get("symphony_configs") or {}
        symphony_states = daemon.state.get("symphony_states") or {}
        symphonies_data = []
        for i, (sym_name, sym_cfg) in enumerate(symphony_configs.items()):
            sym_state = symphony_states.get(sym_name)
            last_poll_raw = getattr(sym_state, "last_poll_at", None) if sym_state else None
            sym_entry = {
                "name": sym_name,
                "priority": i,
                "github_project_number": getattr(sym_cfg, "github_project_number", None),
                "env_bootstrap_performer_id": getattr(sym_cfg, "env_bootstrap_performer_id", None),
                "cycle_count": getattr(sym_state, "cycle_count", 0) if sym_state else 0,
                "error_count": getattr(sym_state, "error_count", 0) if sym_state else 0,
                "last_poll_at": (
                    last_poll_raw.isoformat() if isinstance(last_poll_raw, datetime) else None
                ),
                "state": {
                    "last_error": getattr(sym_state, "last_error", None) if sym_state else None,
                    "active_card": getattr(sym_state, "active_card", None) if sym_state else None,
                    "board_snapshot": getattr(sym_state, "board_snapshot", None) if sym_state else None,
                    "board_titles": getattr(sym_state, "board_titles", None) or {},
                    "board_issue_numbers": getattr(sym_state, "board_issue_numbers", None) or {},
                    "board_issue_urls": getattr(sym_state, "board_issue_urls", None) or {},
                    "board_pr_urls": getattr(sym_state, "board_pr_urls", None) or {},
                    "active_sessions": getattr(sym_state, "active_sessions", None) or {},
                } if sym_state is not None else None,
            }
            symphonies_data.append(sym_entry)
        return symphonies_data


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
:root {
  --color-bg-base:            #0d1117;
  --color-bg-surface:         #161b22;
  --color-bg-elevated:        #21262d;
  --color-border:             #30363d;
  --color-border-subtle:      #21262d;
  --color-text-primary:       #c9d1d9;
  --color-text-muted:         #8b949e;
  --color-accent-blue:        #58a6ff;
  --color-accent-green:       #3fb950;
  --color-accent-yellow:      #d29922;
  --color-accent-red:         #f85149;
  --color-accent-orange:      #f0883e;
  --color-healthy:            var(--color-accent-green);
  --color-degraded:           var(--color-accent-yellow);
  --color-error:              var(--color-accent-red);
  --color-active:             var(--color-accent-blue);
  --color-bg-healthy:         #1f4a1f;
  --color-bg-degraded:        #4a3a1f;
  --color-bg-error:           #4a1f1f;
  --color-bg-accent:          #1a2a3a;
  --color-bg-subtle:          #1e1e2e;
  --color-ev-progress:        #1a2a1a;
  --color-ev-thinking:        #2a2a1a;
  --color-ev-error:           #2a1a1a;
  --color-ev-output:          #1e1e1e;
  --color-bg-row-hover:       #132035;
  --color-bg-row-selected:    #1b2940;
  --color-bg-pill:            #111827;
  --color-accent-blue-subtle: #58a6ff33;
  --color-bar-track:          #333;
  --color-bar-fill:           #27ae60;
  --color-bar-full:           #e74c3c;
  --color-btn-success:        #238636;
  --color-bg-delete:          #3d1f1f;
  --color-border-delete:      #6e2e2e;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
body { font-family: monospace; font-size: 14px; background: var(--color-bg-base); color: var(--color-text-primary); padding: 16px; }
h1 { font-size: 18px; color: var(--color-accent-blue); margin-bottom: 16px; }
h2 { font-size: 13px; color: var(--color-text-muted); text-transform: uppercase; letter-spacing: 0.05em; margin-bottom: 8px; margin-top: 16px; }
.grid { display: grid; grid-template-columns: 1fr; gap: 12px; }
@media (min-width: 900px) {
  .grid { grid-template-columns: 1fr 1fr; }
  .full { grid-column: 1 / -1; }
}
.card { background: var(--color-bg-surface); border: 1px solid var(--color-border); border-radius: 6px; padding: 12px; }
.phase { font-size: 22px; font-weight: bold; }
.phase-idle { color: var(--color-text-muted); }
.phase-dispatching, .phase-monitoring { color: var(--color-accent-blue); }
.phase-merging { color: var(--color-accent-green); }
.phase-blocked { color: var(--color-accent-yellow); }
.phase-recovery { color: var(--color-accent-red); }
.phase-desc { font-size: 12px; color: var(--color-text-muted); margin-top: 4px; }
.badge { display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 12px; margin-right: 4px; }
.badge-healthy { background: var(--color-bg-healthy); color: var(--color-accent-green); }
.badge-degraded { background: var(--color-bg-degraded); color: var(--color-accent-yellow); }
.badge-unavailable { background: var(--color-bg-error); color: var(--color-accent-red); }
.badge-success { background: var(--color-bg-healthy); color: var(--color-accent-green); }
.badge-error { background: var(--color-bg-error); color: var(--color-accent-red); }
.badge-required { background: var(--color-bg-accent); color: var(--color-accent-blue); }
.badge-optional { background: var(--color-bg-subtle); color: var(--color-text-muted); }
.label { color: var(--color-text-muted); margin-right: 6px; }
a { color: var(--color-accent-blue); text-decoration: none; }
a:hover { text-decoration: underline; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th { text-align: left; color: var(--color-text-muted); padding: 4px 8px; border-bottom: 1px solid var(--color-border); }
td { padding: 4px 8px; border-bottom: 1px solid var(--color-bg-elevated); }
.empty-state { color: var(--color-text-muted); font-style: italic; padding: 8px 0; }
.card-warning { border-color: var(--color-accent-yellow) !important; }
.empty-state-warning { color: var(--color-accent-yellow); font-weight: bold; padding: 8px 0; }
.swimlane-grid { display: grid; grid-template-columns: repeat(4, minmax(0, 1fr)); gap: 8px; }
.swimlane-col { background: var(--color-bg-base); border: 1px solid var(--color-border); border-radius: 4px; min-height: 60px; display: flex; flex-direction: column; }
.swimlane-col-header { font-size: 11px; text-transform: uppercase; letter-spacing: 0.05em; color: var(--color-text-muted); padding: 6px 10px; border-bottom: 1px solid var(--color-border); display: flex; justify-content: space-between; align-items: center; }
.swimlane-col-count { background: var(--color-bg-pill); border: 1px solid var(--color-border); border-radius: 10px; padding: 0 6px; font-size: 10px; color: var(--color-text-primary); }
.swimlane-col-body { padding: 6px; display: flex; flex-direction: column; gap: 6px; }
.swimlane-card { background: var(--color-bg-elevated); border: 1px solid var(--color-border); border-radius: 4px; padding: 8px 10px; }
.swimlane-card[tabindex]:hover { background: var(--color-bg-row-hover); border-color: var(--color-accent-blue); }
.swimlane-empty { color: var(--color-text-muted); font-style: italic; font-size: 12px; padding: 4px 6px; }
.swimlane-tabs { display: flex; gap: 2px; border-bottom: 1px solid var(--color-border); margin-bottom: 10px; }
.swimlane-tab { background: transparent; border: none; border-bottom: 2px solid transparent; color: var(--color-text-muted); padding: 6px 12px; font-size: 12px; cursor: pointer; font-family: inherit; }
.swimlane-tab:hover { color: var(--color-text-primary); }
.swimlane-tab.active { color: var(--color-accent-blue); border-bottom-color: var(--color-accent-blue); }
.swimlane-tab-count { background: var(--color-bg-pill); border: 1px solid var(--color-border); border-radius: 10px; padding: 0 6px; font-size: 10px; margin-left: 4px; color: var(--color-text-primary); }
.metric-row { display: flex; gap: 24px; flex-wrap: wrap; }
.metric { display: flex; flex-direction: column; }
.metric-value { font-size: 20px; font-weight: bold; color: var(--color-text-primary); }
.metric-label { font-size: 11px; color: var(--color-text-muted); }
.daemon-start { font-size: 13px; color: var(--color-text-muted); margin-top: 8px; }
.daemon-start strong { color: var(--color-accent-yellow); }
.action-btn {
  margin-top: 10px; padding: 5px 12px; font-size: 12px; font-weight: 600;
  background: var(--color-bg-elevated); color: var(--color-accent-blue); border: 1px solid var(--color-border);
  border-radius: 6px; cursor: pointer;
}
.action-btn:hover:not(:disabled) { background: var(--color-border); }
.action-btn:disabled { opacity: 0.45; cursor: not-allowed; }
.action-msg { font-size: 11px; color: var(--color-text-muted); margin-top: 4px; min-height: 14px; }
#disconnected-banner {
  display: none; position: fixed; top: 0; left: 0; right: 0;
  background: var(--color-bg-error); color: var(--color-accent-red); text-align: center;
  padding: 8px; font-weight: bold; z-index: 999;
}
#flow-chart { overflow-x: auto; }
#flow-chart svg { max-width: 100%; height: auto; }
.qa-round { margin-bottom: 10px; padding: 8px; background: var(--color-bg-base); border-radius: 4px; border-left: 3px solid var(--color-border); }
.qa-round-q { color: var(--color-text-muted); font-size: 12px; margin-bottom: 4px; }
.qa-round-q li { margin-left: 16px; line-height: 1.6; }
.qa-round-a { color: var(--color-text-primary); font-size: 13px; margin-top: 4px; white-space: pre-wrap; }
.session-age { font-size: 12px; color: var(--color-accent-blue); margin-top: 6px; }
.ev-progress  { background: var(--color-ev-progress); color: var(--color-accent-green); }
.ev-tool_use  { background: var(--color-bg-accent); color: var(--color-accent-blue); }
.ev-thinking  { background: var(--color-ev-thinking); color: var(--color-accent-yellow); }
.ev-cost      { background: var(--color-bg-subtle); color: var(--color-text-muted); }
.ev-error     { background: var(--color-ev-error); color: var(--color-accent-red); }
.ev-output    { background: var(--color-ev-output); color: var(--color-text-muted); }
/* Performers card */
.perf-header { display: flex; align-items: center; gap: 10px; margin-bottom: 10px; flex-wrap: wrap; }
.perf-dot { width: 10px; height: 10px; border-radius: 50%; display: inline-block; flex-shrink: 0; }
.perf-running { background: var(--color-accent-green); animation: pulse-dot 1.5s ease-in-out infinite; }
.perf-idle    { background: var(--color-text-muted); }
.perf-error   { background: var(--color-accent-red); }
@keyframes pulse-dot { 0%,100% { opacity: 1; box-shadow: 0 0 0 0 rgba(63,185,80,.5); } 50% { opacity: 0.8; box-shadow: 0 0 0 5px rgba(63,185,80,0); } }
.perf-metrics { display: flex; gap: 20px; flex-wrap: wrap; font-size: 12px; margin-bottom: 10px; padding: 8px; background: var(--color-bg-base); border-radius: 4px; }
.perf-metric { display: flex; flex-direction: column; }
.perf-metric-value { font-size: 15px; font-weight: bold; color: var(--color-text-primary); }
.perf-metric-label { font-size: 10px; color: var(--color-text-muted); }
.perf-log-header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 4px; font-size: 12px; color: var(--color-text-muted); border-top: 1px solid var(--color-bg-elevated); padding-top: 8px; }
.perf-log { max-height: 300px; overflow-y: auto; background: var(--color-bg-base); border: 1px solid var(--color-bg-elevated); border-radius: 4px; font-size: 12px; }
.perf-log-row { display: grid; grid-template-columns: 68px 82px 1fr; gap: 4px; padding: 3px 6px; border-bottom: 1px solid var(--color-bg-surface); align-items: start; }
.perf-log-row:last-child { border-bottom: none; }
.perf-log-time { color: var(--color-text-muted); white-space: nowrap; font-size: 11px; padding-top: 2px; }
.perf-log-text { word-break: break-word; color: var(--color-text-primary); }
.jump-btn { background: var(--color-bg-elevated); border: 1px solid var(--color-border); color: var(--color-accent-blue); border-radius: 3px; padding: 2px 8px; cursor: pointer; font-size: 11px; font-family: monospace; }
.perf-list-row { display: flex; align-items: center; gap: 10px; padding: 10px; background: var(--color-bg-base); border: 1px solid var(--color-bg-elevated); border-radius: 4px; cursor: pointer; transition: border-color .15s; }
.perf-list-row:hover { border-color: var(--color-accent-blue); }
.perf-list-chevron { margin-left: auto; color: var(--color-text-muted); font-size: 14px; }
.perf-back-btn { background: none; border: none; color: var(--color-accent-blue); cursor: pointer; font-size: 13px; font-family: monospace; padding: 0; margin-bottom: 10px; display: flex; align-items: center; gap: 4px; }
/* 049: Navbar */
#navbar { display: flex; align-items: center; gap: 0; background: var(--color-bg-surface); border-bottom: 1px solid var(--color-border); padding: 0 16px; margin: -16px -16px 16px -16px; position: sticky; top: 0; z-index: 100; }
#navbar .nav-brand { color: var(--color-accent-blue); font-weight: bold; font-size: 15px; padding: 12px 16px 12px 0; margin-right: 8px; border-right: 1px solid var(--color-border); white-space: nowrap; }
#navbar a.nav-link { color: var(--color-text-muted); text-decoration: none; padding: 12px 14px; font-size: 13px; border-bottom: 2px solid transparent; transition: color .15s, border-color .15s; display: inline-block; }
#navbar a.nav-link:hover { color: var(--color-text-primary); text-decoration: none; }
#navbar a.nav-link.nav-active { color: var(--color-accent-blue); border-bottom-color: var(--color-accent-blue); }
#navbar .nav-spacer { flex: 1; }
#navbar .nav-status-dot { width: 8px; height: 8px; border-radius: 50%; background: var(--color-accent-green); display: inline-block; margin-right: 6px; }
#navbar .nav-status-dot.disconnected { background: var(--color-accent-red); }
#navbar-hamburger { display: none; background: none; border: none; color: var(--color-text-muted); cursor: pointer; font-size: 20px; padding: 10px; margin-left: auto; }
#navbar-menu { display: flex; align-items: center; gap: 0; }
@media (max-width: 767px) {
  #navbar { flex-wrap: wrap; }
  #navbar-hamburger { display: block; }
  #navbar-menu { display: none; width: 100%; flex-direction: column; align-items: flex-start; padding: 8px 0; }
  #navbar-menu.open { display: flex; }
  #navbar a.nav-link { padding: 10px 16px; width: 100%; }
}
/* 049: Active-performer tiles */
#active-performers { margin-bottom: 4px; }
.ap-tiles { display: grid; grid-template-columns: 1fr; gap: 10px; }
.ap-tile { background: var(--color-bg-base); border: 1px solid var(--color-border); border-radius: 6px; padding: 12px; transition: border-color .15s; }
.ap-tile:hover { border-color: var(--color-accent-blue-subtle); }
.ap-tile-header { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-bottom: 8px; }
.ap-tile-role { font-size: 11px; text-transform: uppercase; letter-spacing: .06em; color: var(--color-text-muted); display: flex; align-items: center; gap: 6px; }
.ap-tile-phase { font-size: 11px; color: var(--color-text-muted); background: var(--color-bg-surface); border: 1px solid var(--color-border); border-radius: 999px; padding: 2px 8px; }
.ap-tile-title { font-size: 14px; font-weight: bold; color: var(--color-text-primary); margin-bottom: 8px; line-height: 1.35; word-break: break-word; }
.ap-tile-meta { display: flex; gap: 8px; flex-wrap: wrap; }
.ap-pill { display: inline-flex; align-items: center; gap: 4px; border: 1px solid var(--color-border); border-radius: 999px; padding: 2px 8px; font-size: 11px; color: var(--color-text-muted); background: var(--color-bg-pill); }
.ap-pill strong { color: var(--color-text-primary); font-weight: 600; }
.ap-tile-elapsed { color: var(--color-accent-blue); }
.ap-idle { background: var(--color-bg-base); border: 1px dashed var(--color-border); border-radius: 6px; padding: 12px; }
.ap-idle-title { color: var(--color-text-primary); font-size: 13px; font-weight: bold; margin-bottom: 8px; }
.ap-idle-rows { display: flex; flex-direction: column; gap: 8px; }
.ap-idle-row { display: flex; gap: 8px; flex-wrap: wrap; }
/* 053: Workflow card readability without expand/collapse controls */
#flow-chart { max-height: none; overflow-x: auto; overflow-y: hidden; cursor: default; position: relative; }
/* 053: Performers page list/detail drilldown */
#performers-page-content table { table-layout: fixed; }
#performers-page tbody tr.performers-row { cursor: pointer; }
#performers-page tbody tr.performers-row:hover { background: var(--color-bg-row-hover); }
#performers-page tbody tr.performers-row.row-selected { background: var(--color-bg-row-selected); }
#performers-page tbody tr.performers-row:focus-visible { outline: 2px solid var(--color-accent-blue); outline-offset: -2px; }
#performers-page-detail-view { margin-top: 12px; border-top: 1px solid var(--color-border); padding-top: 12px; }
#performers-page-detail { font-size: 12px; line-height: 1.5; }
#performers-page-back:focus-visible { outline: 2px solid var(--color-accent-blue); outline-offset: 2px; border-radius: 3px; }
#performers-page .muted { color: var(--color-text-muted); }
#performers-page .detail-badge { display: inline-block; margin-left: 8px; }
#performers-page .detail-block { background: var(--color-bg-base); border: 1px solid var(--color-border); border-radius: 6px; padding: 10px; margin-top: 10px; }
#performers-page .detail-log { max-height: 160px; overflow-y: auto; font-family: monospace; font-size: 11px; }
#performers-page .detail-list { margin: 6px 0 0 0; padding-left: 18px; }
.role-status-badge { display: inline-block; padding: 1px 7px; border-radius: 3px; font-size: 11px; font-weight: bold; }
.role-active { background: var(--color-bg-healthy); color: var(--color-accent-green); }
.role-idle { background: var(--color-bg-subtle); color: var(--color-text-muted); }
.perf-page-events { max-height: 120px; overflow-y: auto; font-size: 11px; color: var(--color-text-muted); }
</style>
</head>
<body>
<div id="disconnected-banner">&#9888; Disconnected — reconnecting...</div>
<nav id="navbar">
  <span class="nav-brand">&#9670; Coordinare</span>
  <button id="navbar-hamburger" onclick="toggleNavMenu()" aria-label="Menu">&#9776;</button>
  <div id="navbar-menu">
    <a href="/" class="nav-link" onclick="navigate(event,'/')">Dashboard</a>
    <a href="/performers" class="nav-link" onclick="navigate(event,'/performers')">Performers</a>
    <a href="/personas" class="nav-link" onclick="navigate(event,'/personas')">Personas</a>
    <a href="/history" class="nav-link" onclick="navigate(event,'/history')">History</a>
    <a href="/symphonies" class="nav-link" onclick="navigate(event,'/symphonies')">Symphonies</a>
    <a href="/admin/config" class="nav-link" onclick="navigate(event,'/admin/config')">Global Config</a>
  </div>
  <span class="nav-spacer"></span>
  <span role="status" aria-live="polite"><span id="nav-sse-dot" class="nav-status-dot" title="SSE connected"></span><span id="project-link" style="font-size:12px;color:var(--color-text-muted)"></span></span>
</nav>
<div id="main-content">
<!-- 049: Dashboard page -->
<div id="dashboard-page">
<main class="grid">

<div id="active-performers" class="card" style="display:none">
  <h2>Active Performers</h2>
  <div id="active-performer-tiles" class="ap-tiles"></div>
</div>

<div class="card">
  <h2>Phase</h2>
  <div id="phase" class="phase phase-idle">Idle</div>
  <div id="phase-desc" class="phase-desc"></div>
  <div id="session-age" class="session-age" style="display:none"></div>
  <button id="force-poll-btn" class="action-btn" onclick="forcePoll()"
    aria-label="Trigger immediate board poll">Check Board Now</button>
  <div id="force-poll-msg" class="action-msg"></div>
</div>

<div class="card" id="active-work-card">
  <h2>Board</h2>
  <div id="swimlane-tabs" class="swimlane-tabs" style="display:none"></div>
  <div id="swimlane-section">
    <span class="empty-state">Waiting for board snapshot&hellip;</span>
  </div>
  <div id="card-detail-view" style="display:none">
    <button class="perf-back-btn" onclick="closeCardDetail()">&#8592; Board</button>
    <div id="card-detail-content"></div>
  </div>
</div>

<div id="idle-panel" class="card" style="display:none">
  <h2>Coordinare is Idle</h2>
  <div id="idle-panel-content"></div>
</div>

<div class="card">
  <h2>Workflow</h2>
  <div id="flow-chart"><span class="empty-state">Loading flowchart...</span></div>
</div>

<div id="questions-card" class="card" style="display:none">
  <h2>Open Questions</h2>
  <ul id="questions-list" style="padding-left:20px;line-height:1.8"></ul>
</div>

<div id="clarifications-card" class="card" style="display:none">
  <h2>Clarification History</h2>
  <div id="clarifications-list" style="max-height:300px;overflow-y:auto"></div>
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

<div class="card">
  <h2>Recent Cycles</h2>
  <div id="history-section"><span class="empty-state">Loading...</span></div>
</div>

<!-- 049: Persona editor moved to /personas page — hidden on main dashboard -->
<div class="card full" id="personas-main-card" style="display:none">
  <h2>Personas</h2>
  <div id="personas-section"><span class="empty-state">Loading...</span></div>
</div>

</main>
</div><!-- /#dashboard-page -->

<!-- 049: Performers page -->
<div id="performers-page" style="display:none">
  <div id="performers-page-content">
    <h2 style="margin-bottom:12px">Performers</h2>
    <div id="performers-page-list-view">
      <table>
        <thead><tr><th>Role</th><th>Status</th><th>Card</th><th>Active / Idle / Max</th></tr></thead>
        <tbody id="performers-page-tbody"><tr><td colspan="4" class="empty-state">Loading...</td></tr></tbody>
      </table>
    </div>
    <div id="performers-page-detail-view" style="display:none">
      <button id="performers-page-back" class="perf-back-btn" onclick="hidePerformerRoleDetail()">&#8592; Back to performers</button>
      <div id="performers-page-detail"><span class="empty-state">Select a role to view details.</span></div>
    </div>
  </div>
</div>

<!-- 049: Personas page -->
<div id="personas-page" style="display:none">
  <div class="card">
    <h2>Personas</h2>
    <div id="personas-page-section"><span class="empty-state">Loading...</span></div>
  </div>
</div>

<!-- 049: History page -->
<div id="history-page" style="display:none">
  <div class="card">
    <h2>History</h2>
    <div id="history-page-section"><span class="empty-state">Loading...</span></div>
  </div>
</div>

<!-- 057: Symphonies page -->
<div id="symphonies-page" style="display:none">
  <div class="card">
    <h2>Symphonies</h2>
    <div id="symphonies-page-section"><span class="empty-state">Loading...</span></div>
  </div>
</div>

<div id="admin-config-page" style="display:none">
  <div class="card">
    <h2>Global Config</h2>
    <div id="admin-config-page-section"><span class="empty-state">Loading...</span></div>
  </div>
</div>

</div><!-- /#main-content -->

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

function humanPhase(phase) {
  return esc(String(phase || '').replace(/_/g, ' ').replace(/\\b\\w/g, function(c) { return c.toUpperCase(); }));
}
var formatPhaseLabel = humanPhase;

function computeOverallHealth(subsystems) {
  var required = (subsystems || []).filter(function(s) { return s.required; });
  if (!required.length || required.every(function(s) { return s.status === 'healthy'; })) return 'healthy';
  if (required.some(function(s) { return s.status === 'unavailable'; })) return 'unavailable';
  return 'degraded';
}

function isIdle(s) {
  var sessions = Array.isArray(s.active_sessions) ? s.active_sessions : [];
  return sessions.length === 0 && s.phase === 'idle';
}

function renderCycleHistoryInto(container, cycleHistory) {
  if (!container) return;
  var history = Array.isArray(cycleHistory) ? cycleHistory : [];
  if (history.length === 0) {
    container.innerHTML = '<span class="empty-state">No cycles completed yet</span>';
    return;
  }
  container.innerHTML = '<div style="overflow-x:auto"><table><thead><tr>' +
    '<th>Time</th><th>Phase</th><th>Duration</th><th>Outcome</th>' +
    '</tr></thead><tbody>' +
    history.map(function(e) {
      return '<tr>' +
        '<td>' + fmtTime(e.timestamp) + '</td>' +
        '<td>' + humanPhase(e.phase) + '</td>' +
        '<td>' + fmtDuration(e.duration_seconds) + '</td>' +
        '<td><span class="badge badge-' + esc(e.outcome || 'success') + '">' + esc(e.outcome || 'success') + '</span></td>' +
        '</tr>';
    }).join('') +
    '</tbody></table></div>';
}

function findActivePerformerSession(s) {
  // Multi-card mode: flat top-level fields can reflect any session's last sync.
  // For aggregate UI indicators, derive from active_sessions instead.
  var sessions = Array.isArray(s.active_sessions) ? s.active_sessions : [];
  for (var i = 0; i < sessions.length; i++) {
    var p = sessions[i].phase;
    if (p === 'monitoring_performer' || p === 'monitoring_agent' || p === 'relay_feedback') {
      return sessions[i];
    }
  }
  return null;
}

function renderState(s) {
  // Phase — prefer aggregate session state over flat top-level fields, which
  // get clobbered by whichever session last synced to CoordinareState.
  var activeSess = findActivePerformerSession(s);
  var effectivePhase = activeSess ? activeSess.phase : s.phase;
  var effectiveDispatchAt = activeSess ? activeSess.agent_dispatch_at : s.agent_dispatch_at;
  var phaseEl = document.getElementById('phase');
  phaseEl.textContent = activeSess
    ? (formatPhaseLabel(activeSess.phase) || s.phase_label || s.phase)
    : (s.phase_label || s.phase);
  phaseEl.className = 'phase ' + phaseClass(effectivePhase);
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
  document.getElementById('phase-desc').textContent = phaseDescriptions[effectivePhase] || '';

  // Session age (shown when a performer is actively running on any session)
  var ageEl = document.getElementById('session-age');
  if (activeSess && effectiveDispatchAt) {
    ageEl.style.display = '';
    ageEl.textContent = 'Agent running for: ' + (fmtAge(effectiveDispatchAt) || '—');
  } else {
    ageEl.style.display = 'none';
  }

  // Flowchart (async)
  updateFlowChart(s.phase);

  // Active Work + Awaiting Review panels
  renderActiveWorkPanels(s);

  // Idle state panel (059 Phase G)
  var idlePanel = document.getElementById('idle-panel');
  var workCard = document.getElementById('active-work-card');
  if (idlePanel) {
    if (isIdle(s)) {
      idlePanel.style.display = '';
      if (workCard) workCard.style.display = '';
      var idleSummary = (s.board_summary && typeof s.board_summary === 'object') ? s.board_summary : {};
      var bCount = function(k) { var v = idleSummary[k]; return Number.isFinite(v) ? v : 0; };
      var idleTotal = bCount('TODO') + bCount('IN_PROGRESS') + bCount('IN_REVIEW') + bCount('DONE');
      var idleInProg = bCount('IN_PROGRESS');
      var idlePoll = s.last_poll_at ? esc(fmtTime(s.last_poll_at)) : '\\u2014';
      var idleCycles = s.cycles_completed != null ? esc(String(s.cycles_completed)) : '\\u2014';
      var idleFilter = s.assignee_filter ? ' <span style="color:var(--color-text-muted);font-size:12px">(filter: ' + esc(s.assignee_filter) + ')</span>' : '';
      document.getElementById('idle-panel-content').innerHTML =
        '<table style="border-collapse:collapse;font-size:13px">' +
          '<tr><td style="padding:4px 12px 4px 0;color:var(--color-text-muted)">Board total</td>' +
              '<td style="padding:4px 0"><strong>' + idleTotal + '</strong>' + idleFilter + '</td></tr>' +
          '<tr><td style="padding:4px 12px 4px 0;color:var(--color-text-muted)">In progress</td>' +
              '<td style="padding:4px 0"><strong>' + idleInProg + '</strong></td></tr>' +
          '<tr><td style="padding:4px 12px 4px 0;color:var(--color-text-muted)">Last poll</td>' +
              '<td style="padding:4px 0"><strong>' + idlePoll + '</strong></td></tr>' +
          '<tr><td style="padding:4px 12px 4px 0;color:var(--color-text-muted)">Cycles completed</td>' +
              '<td style="padding:4px 0"><strong>' + idleCycles + '</strong></td></tr>' +
        '</table>';
    } else {
      idlePanel.style.display = 'none';
      if (workCard) workCard.style.display = '';
    }
  }

  // T032: Live-update card detail view if one is open
  if (_selectedCardId) {
    var sessions = Array.isArray(s.active_sessions) ? s.active_sessions : [];
    var activeSess = sessions.find(function(ss) { return ss.card_id === _selectedCardId; });
    if (activeSess) {
      var detailContent = document.getElementById('card-detail-content');
      if (detailContent) detailContent.innerHTML = renderCardDetailContent(activeSess, s);
    } else {
      // Session ended while detail was open — close back to the list.
      closeCardDetail();
    }
  }

  // 046: Dependency blockers
  var depCard = document.getElementById('dependency-blockers-card');
  if (!depCard) {
    depCard = document.createElement('div');
    depCard.id = 'dependency-blockers-card';
    depCard.className = 'card';
    depCard.style.display = 'none';
    depCard.innerHTML = '<h3>Blocked by Dependencies</h3><ul id="dependency-blockers-list"></ul>';
    var questionsCard = document.getElementById('questions-card');
    if (questionsCard && questionsCard.parentNode) {
      questionsCard.parentNode.insertBefore(depCard, questionsCard);
    }
  }
  var depList = document.getElementById('dependency-blockers-list');
  if (s.blocked_by_dependencies && s.blocked_by_dependencies.length > 0) {
    depCard.style.display = '';
    depList.innerHTML = s.blocked_by_dependencies.map(function(d) {
      var label = '#' + d.issue_number;
      if (d.title) label += ' — ' + esc(d.title);
      var col = d.column || 'off-board';
      var link = d.issue_url && /^https?:\\/\\//i.test(d.issue_url)
        ? '<a href="' + esc(d.issue_url) + '" target="_blank" rel="noopener">' + esc(label) + '</a>'
        : esc(label);
      return '<li>' + link + ' <span style="opacity:0.7">(' + esc(col) + ')</span></li>';
    }).join('');
  } else {
    depCard.style.display = 'none';
  }

  // 048: Performer utilization
  var utilCard = document.getElementById('role-utilization-card');
  if (!utilCard) {
    utilCard = document.createElement('div');
    utilCard.id = 'role-utilization-card';
    utilCard.className = 'card';
    utilCard.style.display = 'none';
    utilCard.innerHTML = '<h3>Performer Utilization</h3><table id="role-utilization-table" style="width:100%;border-collapse:collapse"><thead><tr><th style="text-align:left">Role</th><th>Active / Idle / Max</th></tr></thead><tbody></tbody></table>';
    var qCardRef = document.getElementById('questions-card');
    if (qCardRef && qCardRef.parentNode) { qCardRef.parentNode.insertBefore(utilCard, qCardRef); }
  }
  var utilTbody = utilCard.querySelector('tbody');
  if (s.role_utilization && s.role_utilization.length > 0) {
    utilCard.style.display = '';
    utilTbody.innerHTML = s.role_utilization.map(function(r) {
      var pct = r.max > 0 ? Math.min(100, Math.round(r.active / r.max * 100)) : 0;
      var bar = '<div style="background:var(--color-bar-track);border-radius:3px;height:8px;width:60px;display:inline-block;vertical-align:middle"><div style="background:' + (pct >= 100 ? 'var(--color-bar-full)' : 'var(--color-bar-fill)') + ';height:100%;width:' + pct + '%;border-radius:3px"></div></div>';
      var idle = Math.max(0, r.max - r.active);
      return '<tr><td>' + esc(r.role) + '</td><td>' + bar + ' ' + r.active + ' / ' + idle + ' / ' + r.max + '</td></tr>';
    }).join('');
  } else {
    utilCard.style.display = 'none';
  }

  // Open questions (blocked phase)
  var qCard = document.getElementById('questions-card');
  var qList = document.getElementById('questions-list');
  if (s.open_questions && s.open_questions.length > 0) {
    qCard.style.display = '';
    var issueHref = s.issue_url && /^https?:\\/\\//i.test(s.issue_url) ? s.issue_url : null;
    qList.innerHTML = s.open_questions.map(function(q) {
      var link = issueHref
        ? ' <a href="' + esc(issueHref) + '" target="_blank" rel="noopener" style="font-size:0.85em;white-space:nowrap">View issue &#8599;</a>'
        : '';
      return '<li>' + esc(q) + link + '</li>';
    }).join('');
  } else {
    qCard.style.display = 'none';
  }

  // 047: Rebase status
  var rebaseCard = document.getElementById('rebase-status-card');
  if (!rebaseCard) {
    rebaseCard = document.createElement('div');
    rebaseCard.id = 'rebase-status-card';
    rebaseCard.className = 'card';
    rebaseCard.style.display = 'none';
    rebaseCard.innerHTML = '<h3>Last Rebase Round</h3><ul id="rebase-status-list"></ul>';
    var anchor = document.getElementById('questions-card');
    if (anchor && anchor.parentNode) {
      anchor.parentNode.insertBefore(rebaseCard, anchor);
    }
  }
  var rebaseList = document.getElementById('rebase-status-list');
  if (s.last_rebase_round && s.last_rebase_round.jobs && s.last_rebase_round.jobs.length > 0) {
    rebaseCard.style.display = '';
    var outcomeEmoji = {clean: '\\u2705', performer_resolved: '\\ud83d\\udee0', blocked: '\\ud83d\\udeab', skipped: '\\u23e9', failed: '\\u274c'};
    rebaseList.innerHTML = s.last_rebase_round.jobs.map(function(j) {
      var emoji = outcomeEmoji[j.outcome] || '\\u2753';
      var sha = j.post_rebase_sha ? j.post_rebase_sha.substring(0, 8) : '';
      var label = emoji + ' ' + esc(j.branch.split('/').pop() || j.branch) + ' — ' + esc(j.outcome);
      if (sha) label += ' (' + esc(sha) + ')';
      if (j.conflicted_files && j.conflicted_files.length > 0) {
        label += ' [' + j.conflicted_files.map(esc).join(', ') + ']';
      }
      return '<li>' + label + '</li>';
    }).join('');
  } else {
    rebaseCard.style.display = 'none';
  }

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
        '<div style="font-size:11px;color:var(--color-text-muted);margin-bottom:4px">Round ' + (i+1) + '</div>' +
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
    a.style.cssText = 'color:var(--color-accent-blue);text-decoration:none';
    a.textContent = s.project_name + ' Board';
    linkEl.appendChild(a);
  }
  var cardTokensTotal = (s.card_tokens_total || 0);
  if (!(cardTokensTotal > 0)) {
    var fallbackTokens = derivePerformerTokenTotal(s);
    if (fallbackTokens != null) cardTokensTotal = fallbackTokens;
  }
  document.getElementById('card-tokens-total').textContent =
    cardTokensTotal.toLocaleString();
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
    var overall = s.overall_health || computeOverallHealth(s.subsystems);
    var isHealthy = overall === 'healthy';
    var summaryIcon = isHealthy ? '&#9679;' : (overall === 'unavailable' ? '&#10005;' : '&#9888;');
    var summaryClass = isHealthy ? 'color:var(--color-healthy)' : (overall === 'unavailable' ? 'color:var(--color-error)' : 'color:var(--color-degraded)');
    var degradedCount = s.subsystems.filter(function(sub) { return sub.required && sub.status !== 'healthy'; }).length;
    var summaryText = isHealthy
      ? 'All systems healthy'
      : (degradedCount + (degradedCount === 1 ? ' system ' : ' systems ') + (overall === 'unavailable' ? 'unavailable' : 'degraded'));
    var rows = s.subsystems.map(function(sub) {
      var rowStyle = sub.status !== 'healthy' ? ' style="background:var(--color-bg-' + (sub.status === 'unavailable' ? 'error' : 'degraded') + ')"' : '';
      return '<tr' + rowStyle + '>' +
        '<td>' + esc(sub.name) + '</td>' +
        '<td><span class="badge badge-' + esc(sub.status) + '">' + esc(sub.status) + '</span></td>' +
        '<td><span class="badge ' + (sub.required ? 'badge-required' : 'badge-optional') + '">' +
          (sub.required ? 'required' : 'optional') + '</span></td>' +
        '<td>' + esc(sub.details || '') + '</td>' +
        '</tr>';
    }).join('');
    subsEl.innerHTML =
      '<details' + (isHealthy ? '' : ' open') + '>' +
        '<summary style="cursor:pointer;list-style:none;padding:2px 0">' +
          '<span style="' + summaryClass + '">' + summaryIcon + ' ' + esc(summaryText) + '</span>' +
        '</summary>' +
        '<div style="margin-top:8px">' +
          '<table><thead><tr>' +
            '<th>Subsystem</th><th>Status</th><th>Required</th><th>Details</th>' +
          '</tr></thead><tbody>' + rows + '</tbody></table>' +
        '</div>' +
      '</details>';
  }

  // Force-poll button state (016-force-poll)
  var fpBtn = document.getElementById('force-poll-btn');
  var fpMsg = document.getElementById('force-poll-msg');
  if (fpBtn) {
    var shouldDisable = s.cycle_active || !s.daemon_running;
    fpBtn.disabled = shouldDisable;
    if (!shouldDisable) fpMsg.textContent = '';
  }

  renderCycleHistoryInto(document.getElementById('history-section'), s.cycle_history);
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

var STALE_THRESHOLD_MS = 30 * 60 * 1000;  // 30 minutes

var _selectedCardId = null;   // card_id shown in card-detail-view
var _swimlaneTab = null;      // currently selected symphony tab (null = no symphonies)
function selectSwimlaneTab(name) {
  _swimlaneTab = name;
  if (_lastState) renderActiveWorkPanels(_lastState);
}

function parseTokenCount(text) {
  var msg = String(text || '');
  var tagged = msg.match(/([\\d,]+)\\s+tokens\\s+total/i);
  if (tagged && tagged[1]) {
    var taggedNum = parseInt(tagged[1].replace(/,/g, ''), 10);
    if (!isNaN(taggedNum)) return taggedNum;
  }
  var generic = msg.match(/([\\d,]+)/);
  if (generic && generic[1]) {
    var genericNum = parseInt(generic[1].replace(/,/g, ''), 10);
    if (!isNaN(genericNum)) return genericNum;
  }
  return null;
}

function derivePerformerTokenTotal(s) {
  var m = s.performer_metrics || {};
  if (typeof m.tokens_processed === 'number' && isFinite(m.tokens_processed) && m.tokens_processed >= 0) {
    return Math.floor(m.tokens_processed);
  }
  var ss = s.session_stats || {};
  if (typeof ss.total_tokens === 'number' && isFinite(ss.total_tokens) && ss.total_tokens >= 0) {
    return Math.floor(ss.total_tokens);
  }
  var events = Array.isArray(s.performer_events) ? s.performer_events : [];
  for (var i = events.length - 1; i >= 0; i--) {
    var ev = events[i] || {};
    if ((ev.type || '') !== 'cost') continue;
    var parsed = parseTokenCount(ev.text || '');
    if (parsed != null) return parsed;
  }
  if (typeof s.card_tokens_total === 'number' && isFinite(s.card_tokens_total) && s.card_tokens_total > 0) {
    return Math.floor(s.card_tokens_total);
  }
  return null;
}

// Refresh session age counter every 10s while monitoring_agent
var _lastState = null;
var _performersSelectedRole = null;
var _performersDetailOpen = false;

// 049: Client-side router
function showPage(pageId) {
  var pages = ['dashboard-page','performers-page','personas-page','history-page','symphonies-page','admin-config-page'];
  pages.forEach(function(id) {
    var el = document.getElementById(id);
    if (el) el.style.display = id === pageId ? '' : 'none';
  });
}

function setActiveNav(path) {
  var links = document.querySelectorAll('.nav-link');
  links.forEach(function(link) {
    var href = link.getAttribute('href');
    var active = (path === '/' && href === '/') || (path !== '/' && href !== '/' && path.startsWith(href));
    link.classList.toggle('nav-active', active);
    if (active) { link.setAttribute('aria-current', 'page'); }
    else { link.removeAttribute('aria-current'); }
  });
}

function updateNavActive(path) {
  setActiveNav(path);
  var titles = {'/':'Dashboard — Coordinare','/performers':'Performers — Coordinare','/personas':'Personas — Coordinare','/history':'History — Coordinare','/symphonies':'Symphonies — Coordinare','/admin/config':'Global Config — Coordinare'};
  document.title = titles[path] || (path.startsWith('/symphonies/') ? 'Symphony — Coordinare' : 'Coordinare');
}

function renderSymphoniesPage(s) {
  var el = document.getElementById('symphonies-page-section');
  if (!el) return;
  var syms = Array.isArray(s.symphonies) ? s.symphonies : [];
  var path = location.pathname;
  var detail = path.startsWith('/symphonies/') ? decodeURIComponent(path.slice('/symphonies/'.length)) : null;
  if (detail) {
    // On SSE updates, avoid re-fetching if the page is already rendered:
    // update only the volatile status table cells from the SSE state payload.
    var sym = syms.find(function(x) { return x.name === detail; });
    var tbl = el.querySelector('table[data-detail]');
    if (tbl && sym) {
      var rows = tbl.querySelectorAll('tbody tr');
      function _setTd(row, val) { if (row) { var td = row.querySelector('td'); if (td) td.textContent = val; } }
      _setTd(rows[1], sym.cycle_count != null ? sym.cycle_count : '—');
      _setTd(rows[2], sym.error_count != null ? sym.error_count : '—');
      _setTd(rows[3], sym.state && sym.state.active_card ? (sym.state.active_card.title || sym.state.active_card.id || '') : '—');
      _setTd(rows[4], sym.last_poll_at ? fmtTime(sym.last_poll_at) : '—');
      return;
    }
    loadSymphonyDetail(detail, el);
    return;
  }
  // List view
  var addFormHtml = '<div id="sym-add-form" style="display:none;margin-top:16px;padding:12px;background:var(--color-bg-surface);border:1px solid var(--color-border);border-radius:6px">'
    + '<div style="font-weight:bold;color:var(--color-text-primary);margin-bottom:10px;font-size:13px">Add Symphony</div>'
    + '<div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-bottom:10px">'
    + '<div><label style="font-size:11px;color:var(--color-text-muted);display:block;margin-bottom:3px">Name (alphanumeric + dash)</label>'
    + '<input id="sym-add-name" type="text" placeholder="e.g. frontend" style="width:100%;box-sizing:border-box;background:var(--color-bg-base);color:var(--color-text-primary);border:1px solid var(--color-border);border-radius:4px;padding:5px 8px;font-size:12px"></div>'
    + '<div><label style="font-size:11px;color:var(--color-text-muted);display:block;margin-bottom:3px">GitHub Project Number</label>'
    + '<input id="sym-add-proj" type="number" min="1" placeholder="e.g. 42" style="width:100%;box-sizing:border-box;background:var(--color-bg-base);color:var(--color-text-primary);border:1px solid var(--color-border);border-radius:4px;padding:5px 8px;font-size:12px"></div>'
    + '</div>'
    + '<div style="display:flex;gap:8px;align-items:center">'
    + '<button class="action-btn" onclick="submitAddSymphony()">Add</button>'
    + '<button class="action-btn" onclick="document.getElementById(\\'sym-add-form\\').style.display=\\'none\\'">Cancel</button>'
    + '<span id="sym-add-msg" class="action-msg"></span>'
    + '</div></div>';
  if (!syms.length) {
    el.innerHTML = '<span class="empty-state">No symphonies configured.</span>'
      + '<div style="margin-top:12px"><button class="action-btn" onclick="document.getElementById(\\'sym-add-form\\').style.display=\\'block\\'">+ Add Symphony</button></div>'
      + addFormHtml;
    return;
  }
  var rows = syms.map(function(sym) {
    var bootBtn = sym.env_bootstrap_performer_id
      ? '<button class="action-btn sym-row-bootstrap" data-sym="' + esc(sym.name) + '" style="padding:2px 8px;font-size:11px">Bootstrap</button>'
      : '<span style="color:var(--color-text-muted)">—</span>';
    // 077: at-a-glance bootstrap status badge (colored dot + label) so a failing
    // env bootstrap is visible on the overview without opening the symphony.
    var bootStatus = '';
    if (sym.env_bootstrap_performer_id) {
      var bsColor, bsText;
      if (sym.bootstrap_in_flight) { bsColor = 'var(--color-accent-blue)'; bsText = 'bootstrapping…'; }
      else if (sym.last_bootstrap_succeeded === false) { bsColor = 'var(--color-accent-red)'; bsText = 'failed'; }
      else if (sym.cache_dir_ready && sym.last_bootstrap_succeeded === true) { bsColor = 'var(--color-accent-green)'; bsText = 'ready'; }
      else { bsColor = 'var(--color-text-muted)'; bsText = 'not bootstrapped'; }
      var bsTitle = (sym.last_bootstrap_succeeded === false && sym.last_bootstrap_error)
        ? ' title="' + esc(sym.last_bootstrap_error) + '"' : '';
      bootStatus = '<span' + bsTitle + ' style="display:inline-flex;align-items:center;gap:5px;margin-right:8px;font-size:11px;color:' + bsColor + '">'
        + '<span style="width:8px;height:8px;border-radius:50%;background:' + bsColor + ';display:inline-block;flex:none"></span>'
        + bsText + '</span>';
    }
    return '<tr>'
      + '<td><a href="/symphonies/' + encodeURIComponent(sym.name) + '" onclick="navigate(event,this.pathname)" style="color:var(--color-accent-blue)">' + esc(sym.name) + '</a></td>'
      + '<td style="color:var(--color-text-muted)">' + (sym.github_project_number != null ? '#' + sym.github_project_number : '—') + '</td>'
      + '<td>' + (sym.priority != null ? sym.priority : '—') + '</td>'
      + '<td>' + (sym.cycle_count != null ? sym.cycle_count : '—') + '</td>'
      + '<td>' + (sym.error_count != null ? sym.error_count : '—') + '</td>'
      + '<td>' + (sym.last_poll_at ? esc(fmtTime(sym.last_poll_at)) : '—') + '</td>'
      + '<td>' + bootStatus + bootBtn + '</td>'
      + '</tr>';
  }).join('');
  el.innerHTML = '<div style="overflow-x:auto"><table style="width:100%;font-size:12px"><thead><tr>'
    + '<th style="text-align:left">Name</th><th style="text-align:left">Project</th><th style="text-align:left">Priority</th><th style="text-align:left">Cycles</th><th style="text-align:left">Errors</th><th style="text-align:left">Last poll</th><th style="text-align:left">Env</th>'
    + '</tr></thead><tbody>' + rows + '</tbody></table></div>'
    + '<div id="sym-list-msg" class="action-msg" style="margin-top:8px"></div>'
    + '<div style="margin-top:14px"><button class="action-btn" onclick="document.getElementById(\\'sym-add-form\\').style.display=\\'block\\'">+ Add Symphony</button></div>'
    + addFormHtml;
  el.querySelectorAll('.sym-row-bootstrap').forEach(function(b) {
    b.addEventListener('click', async function() {
      var n = b.getAttribute('data-sym');
      var msg = document.getElementById('sym-list-msg');
      b.disabled = true;
      msg.textContent = 'Dispatching ' + n + '...';
      msg.style.color = 'var(--color-text-muted)';
      try {
        var r = await fetch('/api/symphonies/' + encodeURIComponent(n) + '/env-bootstrap', {method: 'POST'});
        var d = await r.json();
        if (r.ok) { msg.textContent = n + ': accepted'; msg.style.color = 'var(--color-accent-green)'; }
        else { msg.textContent = n + ': ' + (d.error || ('error ' + r.status)); msg.style.color = 'var(--color-accent-red)'; }
      } catch(e) { msg.textContent = n + ': network error'; msg.style.color = 'var(--color-accent-red)'; }
      b.disabled = false;
    });
  });
}

async function loadSymphonyDetail(name, el) {
  el.innerHTML = '<span class="empty-state">Loading...</span>';
  var backLink = '<a href="/symphonies" onclick="navigate(event,this.pathname)" style="color:var(--color-accent-blue);font-size:13px">&#8592; All symphonies</a>';
  var res, data;
  try {
    res = await fetch('/api/symphonies/' + encodeURIComponent(name));
    data = await res.json();
  } catch(e) {
    el.innerHTML = backLink + '<div class="empty-state" style="margin-top:12px">Failed to load symphony</div>';
    return;
  }
  if (!res.ok) {
    el.innerHTML = backLink + '<div class="empty-state" style="margin-top:12px">' + esc(data.error || 'Symphony not found') + '</div>';
    return;
  }
  var st = data.state || {};
  var ov = data.overrides || {};
  var personas = data.personas || {};
  var overrideFields = [
    {key:'max_concurrent_cards', label:'Max concurrent cards', type:'number', placeholder:'1'},
    {key:'poll_interval_seconds', label:'Poll interval (seconds)', type:'number', placeholder:'30'},
    {key:'max_feedback_cycles', label:'Max feedback cycles', type:'number', placeholder:'5'},
    {key:'max_closed_pr_attempts_per_issue', label:'Max closed PR attempts', type:'number', placeholder:'3'},
    {key:'assignee_filter', label:'Assignee filter (GitHub login)', type:'text', placeholder:'(no filter)'},
  ];
  var personaRoles = ['assessor','architect','implementer','reviewer','security','qa','tech_writer','closer'];
  var statusRows = [
    ['GitHub project', data.github_project_number != null ? '#' + data.github_project_number : '—'],
    ['Cycles', st.cycle_count != null ? st.cycle_count : '—'],
    ['Errors', st.error_count != null ? st.error_count : '—'],
    ['Active card', st.active_card ? esc(st.active_card.title || st.active_card.id || '') : '—'],
    ['Last poll', st.last_poll_at ? esc(fmtTime(st.last_poll_at)) : '—'],
    ['Last error', st.last_error ? esc(st.last_error) : '—'],
  ].map(function(r) {
    return '<tr><th style="text-align:left;padding:3px 10px 3px 0;color:var(--color-text-muted);font-weight:normal;white-space:nowrap">' + r[0] + '</th><td style="font-size:12px">' + r[1] + '</td></tr>';
  }).join('');
  var overrideInputs = overrideFields.map(function(f) {
    var val = ov[f.key] != null ? ov[f.key] : '';
    return '<div style="margin-bottom:8px">'
      + '<label style="font-size:11px;color:var(--color-text-muted);display:block;margin-bottom:3px">' + esc(f.label) + '</label>'
      + '<input data-override-key="' + esc(f.key) + '" type="' + f.type + '" value="' + esc(String(val)) + '" placeholder="' + esc(f.placeholder) + '" style="width:100%;box-sizing:border-box;background:var(--color-bg-base);color:var(--color-text-primary);border:1px solid var(--color-border);border-radius:4px;padding:5px 8px;font-size:12px">'
      + '</div>';
  }).join('');
  var enabledChecked = data.enabled !== false ? 'checked' : '';
  var specFiles = data.env_spec_files || ['README.md'];
  var specFileTags = specFiles.map(function(f) {
    return '<span data-spec-file="' + esc(f) + '" style="display:inline-flex;align-items:center;gap:4px;background:var(--color-bg-elevated);border:1px solid var(--color-border);border-radius:12px;padding:2px 8px;font-size:11px;margin:2px">'
      + esc(f)
      + '<button type="button" onclick="this.parentElement.remove()" style="background:none;border:none;cursor:pointer;color:var(--color-text-muted);font-size:13px;padding:0 0 0 2px;line-height:1">&times;</button>'
      + '</span>';
  }).join('');
  var personaInputs = personaRoles.map(function(role) {
    var instr = (personas[role] && personas[role].instructions) ? personas[role].instructions : '';
    return '<div style="margin-bottom:10px">'
      + '<label style="font-size:11px;color:var(--color-text-muted);display:block;margin-bottom:3px">' + esc(role) + '</label>'
      + '<textarea data-persona-role="' + esc(role) + '" rows="3" placeholder="(inherits global default)" style="width:100%;box-sizing:border-box;font-family:monospace;font-size:11px;background:var(--color-bg-base);color:var(--color-text-primary);border:1px solid var(--color-border);border-radius:4px;padding:6px 8px;resize:vertical">' + esc(instr) + '</textarea>'
      + '</div>';
  }).join('');
  var ec = data.env_cache;
  var ecBootstrapId = data.env_bootstrap_performer_id;
  var ecSection = '';
  if (ecBootstrapId) {
    var ecRows;
    if (ec) {
      var inFlight = ec.bootstrap_in_flight;
      var ready = ec.cache_dir_ready;
      var lastOk = ec.last_bootstrap_succeeded;
      var statusColor;
      var statusText;
      if (inFlight) { statusColor = 'var(--color-accent-blue)'; statusText = 'in flight'; }
      else if (lastOk === true && ready) { statusColor = 'var(--color-accent-green)'; statusText = 'ready'; }
      else if (lastOk === false) { statusColor = 'var(--color-accent-red)'; statusText = 'last bootstrap failed'; }
      else { statusColor = 'var(--color-text-muted)'; statusText = 'not yet bootstrapped'; }
      var infRow;
      if (ec.last_inference_skipped_reason) {
        infRow = '<span style="color:var(--color-text-muted)">skipped: '
          + esc(ec.last_inference_skipped_reason) + '</span>';
      } else if (ec.last_inference_succeeded === true) {
        var svcs = (ec.last_inference_services || []).join(', ') || '(none)';
        infRow = '<span style="color:var(--color-accent-green)">ok</span> '
          + esc(svcs)
          + ' <span style="color:var(--color-text-muted)">('
          + esc(String(ec.last_inference_attempts || '?')) + ' attempt'
          + ((ec.last_inference_attempts || 0) === 1 ? '' : 's') + ', '
          + esc(ec.last_inference_agent_version || '?') + ')</span>';
      } else if (ec.last_inference_succeeded === false) {
        infRow = '<span style="color:var(--color-accent-red)">failed</span> '
          + '<span style="color:var(--color-text-muted)">('
          + esc(String(ec.last_inference_attempts || '?')) + ' attempts, '
          + esc(ec.last_inference_agent_version || '?') + ')</span>';
      } else {
        infRow = '—';
      }
      ecRows = [
        ['Status', '<span style="color:' + statusColor + '">' + statusText + '</span>'],
        ['Performer', esc(ecBootstrapId)],
        ['Cache dir', esc(ec.cache_dir || '—')],
        ['Cache ready', ready ? 'yes' : 'no'],
        ['Bootstrap in flight', inFlight ? 'yes' : 'no'],
        ['Last bootstrap', ec.last_bootstrap_at ? esc(fmtTime(ec.last_bootstrap_at)) : '—'],
        ['Recorded SHA', ec.readme_sha ? esc(ec.readme_sha) : '—'],
        ['Service inference', infRow],
        ['Inference at', ec.last_inference_at ? esc(fmtTime(ec.last_inference_at)) : '—'],
      ].map(function(r) {
        return '<tr><th style="text-align:left;padding:3px 10px 3px 0;color:var(--color-text-muted);font-weight:normal;white-space:nowrap">' + r[0] + '</th><td style="font-size:12px">' + r[1] + '</td></tr>';
      }).join('');
    } else {
      ecRows = '<tr><td style="font-size:12px;color:var(--color-text-muted)">Env cache state not initialised yet.</td></tr>';
    }
    var disabled = (ec && ec.bootstrap_in_flight) ? 'disabled' : '';
    // 077: prominent error banner when the last bootstrap failed, with the reason
    // (verify failure / dispatch error / reap reason) so an operator sees WHY.
    var ecErrBanner = (ec && ec.last_bootstrap_succeeded === false && ec.last_bootstrap_error)
      ? '<div style="background:rgba(220,50,50,0.12);border:1px solid var(--color-accent-red);'
        + 'border-radius:6px;padding:8px 10px;margin-bottom:10px;font-size:12px;color:var(--color-accent-red)">'
        + '⚠ <strong>Env bootstrap failing for ' + esc(name) + ':</strong> '
        + esc(ec.last_bootstrap_error)
        + '</div>'
      : '';
    ecSection = '<div class="card" style="margin-bottom:14px">'
      + '<div style="font-weight:bold;margin-bottom:10px;font-size:13px">Env bootstrap</div>'
      + ecErrBanner
      + '<table style="font-size:12px;margin-bottom:10px"><tbody>' + ecRows + '</tbody></table>'
      + '<button class="action-btn" id="sym-env-bootstrap-btn" ' + disabled + '>Force bootstrap now</button>'
      + ' <span id="sym-env-bootstrap-msg" class="action-msg"></span>'
      + '</div>';
  }
  el.innerHTML = backLink
    + '<h3 style="margin:12px 0 4px">' + esc(name) + '</h3>'
    + '<table data-detail style="font-size:12px;margin-bottom:18px"><tbody>' + statusRows + '</tbody></table>'
    + ecSection
    + '<div style="background:var(--color-bg-surface);border:1px solid var(--color-border);border-radius:6px;padding:14px;margin-bottom:14px">'
    + '<div style="font-weight:bold;color:var(--color-text-primary);margin-bottom:12px;font-size:13px">Configuration</div>'
    + '<div style="margin-bottom:10px">'
    + '<label style="display:flex;align-items:center;gap:8px;font-size:12px;color:var(--color-text-primary);cursor:pointer">'
    + '<input id="sym-enabled" type="checkbox" ' + enabledChecked + '> Enabled</label>'
    + '</div>'
    + overrideInputs
    + '<div style="margin-top:4px;border-top:1px solid var(--color-bg-elevated);padding-top:12px">'
    + '<div style="font-size:12px;color:var(--color-text-muted);margin-bottom:8px">Per-symphony persona overrides (leave blank to inherit global defaults)</div>'
    + personaInputs
    + '</div>'
    + '<div style="margin-top:4px;border-top:1px solid var(--color-bg-elevated);padding-top:12px">'
    + '<div style="font-size:12px;color:var(--color-text-muted);margin-bottom:6px">Env spec files (any change triggers a bootstrap)</div>'
    + '<div id="sym-spec-files-list" style="display:flex;flex-wrap:wrap;gap:4px;margin-bottom:8px">' + specFileTags + '</div>'
    + '<div style="display:flex;gap:6px">'
    + '<input id="sym-spec-file-input" type="text" placeholder="e.g. pyproject.toml" style="flex:1;background:var(--color-bg-base);color:var(--color-text-primary);border:1px solid var(--color-border);border-radius:4px;padding:4px 8px;font-size:12px">'
    + '<button type="button" class="action-btn" id="sym-spec-file-add-btn" style="padding:4px 10px;font-size:12px">Add</button>'
    + '</div>'
    + '</div>'
    + '<div style="display:flex;gap:8px;align-items:center;margin-top:4px">'
    + '<button class="action-btn" id="sym-save-btn">Save</button>'
    + '<button class="action-btn" id="sym-delete-btn" style="background:var(--color-bg-delete);border-color:var(--color-border-delete);color:var(--color-accent-red)">Delete symphony</button>'
    + '<span id="sym-save-msg" class="action-msg"></span>'
    + '</div>'
    + '</div>';
  var ecBtn = document.getElementById('sym-env-bootstrap-btn');
  if (ecBtn) {
    ecBtn.addEventListener('click', async function() {
      var msg = document.getElementById('sym-env-bootstrap-msg');
      ecBtn.disabled = true;
      msg.textContent = 'Dispatching...';
      msg.style.color = 'var(--color-text-muted)';
      try {
        var r = await fetch('/api/symphonies/' + encodeURIComponent(name) + '/env-bootstrap', {method: 'POST'});
        var d = await r.json();
        if (r.ok) {
          msg.textContent = 'Accepted — bootstrap will fire on the next cycle';
          msg.style.color = 'var(--color-accent-green)';
          setTimeout(function() { loadSymphonyDetail(name, el); }, 1500);
        } else {
          msg.textContent = d.error || ('Error ' + r.status);
          msg.style.color = 'var(--color-accent-red)';
          ecBtn.disabled = false;
        }
      } catch(e) {
        msg.textContent = 'Network error';
        msg.style.color = 'var(--color-accent-red)';
        ecBtn.disabled = false;
      }
    });
  }
  document.getElementById('sym-spec-file-add-btn').addEventListener('click', function() {
    var inp = document.getElementById('sym-spec-file-input');
    var val = inp.value.trim();
    if (!val) return;
    var list = document.getElementById('sym-spec-files-list');
    var tag = document.createElement('span');
    tag.setAttribute('data-spec-file', val);
    tag.setAttribute('style', 'display:inline-flex;align-items:center;gap:4px;background:var(--color-bg-elevated);border:1px solid var(--color-border);border-radius:12px;padding:2px 8px;font-size:11px;margin:2px');
    tag.innerHTML = esc(val) + '<button type="button" onclick="this.parentElement.remove()" style="background:none;border:none;cursor:pointer;color:var(--color-text-muted);font-size:13px;padding:0 0 0 2px;line-height:1">&times;</button>';
    list.appendChild(tag);
    inp.value = '';
  });
  document.getElementById('sym-save-btn').addEventListener('click', async function() {
    var msg = document.getElementById('sym-save-msg');
    var overrides = {};
    el.querySelectorAll('[data-override-key]').forEach(function(inp) {
      var k = inp.getAttribute('data-override-key');
      var v = inp.value.trim();
      if (v === '') return;
      var numFields = ['max_concurrent_cards','poll_interval_seconds','max_feedback_cycles','max_closed_pr_attempts_per_issue'];
      overrides[k] = numFields.includes(k) ? Number(v) : v;
    });
    var personas = {};
    el.querySelectorAll('[data-persona-role]').forEach(function(ta) {
      var role = ta.getAttribute('data-persona-role');
      var v = ta.value.trim();
      if (v) personas[role] = {instructions: v};
    });
    var specFilesList = [];
    el.querySelectorAll('[data-spec-file]').forEach(function(tag) {
      specFilesList.push(tag.getAttribute('data-spec-file'));
    });
    if (specFilesList.length === 0) specFilesList = ['README.md'];
    var enabled = document.getElementById('sym-enabled').checked;
    try {
      var r = await fetch('/api/symphonies/' + encodeURIComponent(name), {
        method: 'PUT',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({overrides: Object.keys(overrides).length ? overrides : null, personas: Object.keys(personas).length ? personas : null, enabled: enabled, env_spec_files: specFilesList}),
      });
      var d = await r.json();
      if (r.ok) {
        msg.textContent = 'Saved';
        msg.style.color = 'var(--color-accent-green)';
      } else {
        msg.textContent = d.error || ('Error ' + r.status);
        msg.style.color = 'var(--color-accent-red)';
      }
    } catch(e) { msg.textContent = 'Network error'; msg.style.color = 'var(--color-accent-red)'; }
    setTimeout(function(){ if(msg) msg.textContent=''; }, 4000);
  });
  document.getElementById('sym-delete-btn').addEventListener('click', async function() {
    if (!confirm('Delete symphony "' + name + '"? This cannot be undone.')) return;
    var msg = document.getElementById('sym-save-msg');
    try {
      var r = await fetch('/api/symphonies/' + encodeURIComponent(name), {method: 'DELETE'});
      var d = await r.json();
      if (r.ok) {
        navigate(null, '/symphonies');
      } else {
        msg.textContent = d.error || ('Error ' + r.status);
        msg.style.color = 'var(--color-accent-red)';
      }
    } catch(e) { msg.textContent = 'Network error'; msg.style.color = 'var(--color-accent-red)'; }
  });
}

async function submitAddSymphony() {
  var nameVal = (document.getElementById('sym-add-name') || {}).value || '';
  var projVal = (document.getElementById('sym-add-proj') || {}).value || '';
  var msg = document.getElementById('sym-add-msg');
  if (!nameVal.trim() || !projVal.trim()) { msg.textContent = 'Name and project number are required'; msg.style.color='var(--color-accent-red)'; return; }
  try {
    var r = await fetch('/api/symphonies', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({name: nameVal.trim(), github_project_number: Number(projVal)}),
    });
    var d = await r.json();
    if (r.ok) {
      navigate(null, '/symphonies/' + encodeURIComponent(nameVal.trim()));
    } else {
      msg.textContent = d.error || ('Error ' + r.status);
      msg.style.color = 'var(--color-accent-red)';
    }
  } catch(e) { msg.textContent = 'Network error'; msg.style.color='var(--color-accent-red)'; }
}

async function loadGlobalConfigPage() {
  var el = document.getElementById('admin-config-page-section');
  el.innerHTML = '<span class="empty-state">Loading...</span>';
  var res, data;
  try {
    res = await fetch('/api/config/global');
    data = await res.json();
  } catch(e) {
    el.innerHTML = '<div class="empty-state">Failed to load config</div>';
    return;
  }
  if (!res.ok) {
    el.innerHTML = '<div class="empty-state">' + esc(data.error || 'Error loading config') + '</div>';
    return;
  }
  var numFields = [
    {key:'poll_interval_seconds', label:'Poll interval (seconds)', min:0, max:3600},
    {key:'heartbeat_interval_seconds', label:'Heartbeat interval (seconds)', min:5, max:300},
    {key:'max_concurrent_cards', label:'Max concurrent cards', min:1, max:20},
    {key:'max_feedback_cycles', label:'Max feedback cycles', min:0, max:50},
    {key:'max_closed_pr_attempts_per_issue', label:'Max closed PR attempts per issue', min:0, max:100},
  ];
  var selectFields = [
    {key:'log_level', label:'Log level', options:['debug','info','warning','error']},
    {key:'output_mode', label:'Output mode', options:['human','structured']},
  ];
  var textFields = [
    {key:'assignee_filter', label:'Assignee filter (GitHub login)', placeholder:'(no filter)'},
  ];
  var listFields = [
    {key:'human_reviewers', label:'Human reviewers (comma-separated GitHub logins)'},
    {key:'trusted_bot_reviewers', label:'Trusted bot reviewers (comma-separated)'},
  ];
  var envCacheFields = [
    {key:'env_cache_root', label:'Env cache root (host path)', placeholder:'~/.coordinare/env-caches'},
  ];
  function fieldRow(label, inputHtml) {
    return '<div style="margin-bottom:10px">'
      + '<label style="font-size:11px;color:var(--color-text-muted);display:block;margin-bottom:3px">' + esc(label) + '</label>'
      + inputHtml
      + '</div>';
  }
  var inp_style = 'width:100%;box-sizing:border-box;background:var(--color-bg-base);color:var(--color-text-primary);border:1px solid var(--color-border);border-radius:4px;padding:5px 8px;font-size:12px';
  var numHtml = numFields.map(function(f) {
    return fieldRow(f.label, '<input data-cfg-key="' + esc(f.key) + '" data-cfg-type="number" type="number" min="' + f.min + '" max="' + f.max + '" value="' + esc(String(data[f.key] != null ? data[f.key] : '')) + '" style="' + inp_style + '">');
  }).join('');
  var selHtml = selectFields.map(function(f) {
    var opts = f.options.map(function(o) {
      return '<option value="' + esc(o) + '"' + (String(data[f.key] != null ? data[f.key] : '').toLowerCase() === o ? ' selected' : '') + '>' + esc(o) + '</option>';
    }).join('');
    return fieldRow(f.label, '<select data-cfg-key="' + esc(f.key) + '" data-cfg-type="select" style="' + inp_style + '">' + opts + '</select>');
  }).join('');
  var txtHtml = textFields.map(function(f) {
    var v = data[f.key] != null ? data[f.key] : '';
    return fieldRow(f.label, '<input data-cfg-key="' + esc(f.key) + '" data-cfg-type="text" type="text" value="' + esc(String(v)) + '" placeholder="' + esc(f.placeholder || '') + '" style="' + inp_style + '">');
  }).join('');
  var lstHtml = listFields.map(function(f) {
    var v = Array.isArray(data[f.key]) ? data[f.key].join(', ') : (data[f.key] || '');
    return fieldRow(f.label, '<input data-cfg-key="' + esc(f.key) + '" data-cfg-type="list" type="text" value="' + esc(v) + '" style="' + inp_style + '">');
  }).join('');
  var envCacheHtml = envCacheFields.map(function(f) {
    var v = data[f.key] != null ? data[f.key] : '';
    return fieldRow(f.label, '<input data-cfg-key="' + esc(f.key) + '" data-cfg-type="text" type="text" value="' + esc(String(v)) + '" placeholder="' + esc(f.placeholder || '') + '" style="' + inp_style + '">');
  }).join('');
  el.innerHTML = '<div style="background:var(--color-bg-surface);border:1px solid var(--color-border);border-radius:6px;padding:14px">'
    + '<div style="font-weight:bold;color:var(--color-text-primary);margin-bottom:14px;font-size:13px">Operational</div>'
    + numHtml
    + '<div style="border-top:1px solid var(--color-bg-elevated);margin:14px 0"></div>'
    + '<div style="font-weight:bold;color:var(--color-text-primary);margin-bottom:14px;font-size:13px">Behavior</div>'
    + selHtml + txtHtml
    + '<div style="border-top:1px solid var(--color-bg-elevated);margin:14px 0"></div>'
    + '<div style="font-weight:bold;color:var(--color-text-primary);margin-bottom:14px;font-size:13px">Reviewers</div>'
    + lstHtml
    + '<div style="border-top:1px solid var(--color-bg-elevated);margin:14px 0"></div>'
    + '<div style="font-weight:bold;color:var(--color-text-primary);margin-bottom:14px;font-size:13px">Environment Caching</div>'
    + envCacheHtml
    + '<div style="display:flex;gap:8px;align-items:center;margin-top:8px">'
    + '<button class="action-btn" id="gcfg-save-btn">Save</button>'
    + '<span id="gcfg-save-msg" class="action-msg"></span>'
    + '</div>'
    + '</div>';
  document.getElementById('gcfg-save-btn').addEventListener('click', async function() {
    var msg = document.getElementById('gcfg-save-msg');
    var payload = {};
    el.querySelectorAll('[data-cfg-key]').forEach(function(inp) {
      var k = inp.getAttribute('data-cfg-key');
      var t = inp.getAttribute('data-cfg-type');
      var v = inp.value.trim();
      if (t === 'number') payload[k] = v === '' ? null : Number(v);
      else if (t === 'list') payload[k] = v ? v.split(',').map(function(s){ return s.trim(); }).filter(Boolean) : [];
      else payload[k] = v === '' ? null : v;
    });
    try {
      var r = await fetch('/api/config/global', {
        method: 'PUT',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify(payload),
      });
      var d = await r.json();
      if (r.ok) {
        msg.textContent = 'Saved — reload triggered';
        msg.style.color = 'var(--color-accent-green)';
      } else {
        msg.textContent = d.error || ('Error ' + r.status);
        msg.style.color = 'var(--color-accent-red)';
      }
    } catch(e) { msg.textContent = 'Network error'; msg.style.color = 'var(--color-accent-red)'; }
    setTimeout(function(){ if(msg) msg.textContent=''; }, 5000);
  });
}

function router() {
  var path = location.pathname;
  updateNavActive(path);
  if (path === '/performers') {
    showPage('performers-page');
    if (_lastState) renderPerformersPage(_lastState);
  } else if (path === '/personas') {
    showPage('personas-page');
    loadPersonasPage();
  } else if (path === '/history') {
    showPage('history-page');
    if (_lastState) renderHistoryPage(_lastState);
  } else if (path === '/symphonies' || path.startsWith('/symphonies/')) {
    showPage('symphonies-page');
    if (_lastState) renderSymphoniesPage(_lastState);
  } else if (path === '/admin/config') {
    showPage('admin-config-page');
    loadGlobalConfigPage();
  } else {
    showPage('dashboard-page');
    if (_lastState) renderDashboardExtras(_lastState);
  }
}

function navigate(e, path) {
  // Only intercept plain left-clicks; let ctrl/cmd/middle-click open new tabs normally
  if (e && (e.ctrlKey || e.metaKey || e.shiftKey || e.altKey || e.button !== 0)) return;
  if (e) { e.preventDefault(); }
  history.pushState(null, '', path);
  var menu = document.getElementById('navbar-menu');
  if (menu) menu.classList.remove('open');
  router();
}

function toggleNavMenu() {
  var menu = document.getElementById('navbar-menu');
  menu.classList.toggle('open');
}

window.addEventListener('popstate', function() { router(); });

// 062: Swimlane view across symphonies — TODO / BLOCKED / IN_PROGRESS / IN_REVIEW.
// BACKLOG and DONE are intentionally hidden; the swimlane is meant to show only
// in-flight work the coordinare is reasoning about.
function renderActiveWorkPanels(s) {
  var workEl = document.getElementById('swimlane-section');
  var workCard = document.getElementById('active-work-card');
  var tabsEl = document.getElementById('swimlane-tabs');
  if (!workEl) return;

  var COLUMNS = ['TODO', 'BLOCKED', 'IN_PROGRESS', 'IN_REVIEW'];
  var COLUMN_LABELS = {TODO: 'To Do', BLOCKED: 'Blocked', IN_PROGRESS: 'In Progress', IN_REVIEW: 'In Review'};

  // Index live sessions by card_id so swimlane cards can show phase/elapsed.
  var sessions = Array.isArray(s.active_sessions) ? s.active_sessions : [];
  var sessionsByCard = {};
  sessions.forEach(function(sess) { if (sess && sess.card_id) sessionsByCard[sess.card_id] = sess; });

  // Aggregate per-card data across all symphonies (item_id is globally unique).
  // Track which symphony each card belongs to for tab filtering.
  var titles = {}, issueNums = {}, issueUrls = {}, prUrls = {};
  var symphonies = Array.isArray(s.symphonies) ? s.symphonies : [];
  var perSymCounts = {};  // symphony name → total card count
  var allEntries = {TODO: [], BLOCKED: [], IN_PROGRESS: [], IN_REVIEW: []};
  symphonies.forEach(function(sym) {
    var st = sym && sym.state ? sym.state : {};
    Object.assign(titles, st.board_titles || {});
    Object.assign(issueNums, st.board_issue_numbers || {});
    Object.assign(issueUrls, st.board_issue_urls || {});
    Object.assign(prUrls, st.board_pr_urls || {});
    var snap = st.board_snapshot || {};
    var symName = sym.name || '';
    perSymCounts[symName] = 0;
    COLUMNS.forEach(function(col) {
      var items = Array.isArray(snap[col]) ? snap[col] : [];
      items.forEach(function(iid) {
        allEntries[col].push({iid: iid, symphony: symName});
        perSymCounts[symName] += 1;
      });
    });
  });

  // Determine which symphony tab is active (default: first symphony).
  var symphonyNames = symphonies.map(function(sym) { return sym.name || ''; }).filter(Boolean);
  if (symphonyNames.length > 0) {
    if (!_swimlaneTab || symphonyNames.indexOf(_swimlaneTab) === -1) {
      _swimlaneTab = symphonyNames[0];
    }
  } else {
    _swimlaneTab = null;
  }

  // Filter to the active tab (only one symphony's cards visible at a time).
  var columnItems = {TODO: [], BLOCKED: [], IN_PROGRESS: [], IN_REVIEW: []};
  COLUMNS.forEach(function(col) {
    columnItems[col] = _swimlaneTab
      ? allEntries[col].filter(function(e) { return e.symphony === _swimlaneTab; })
      : allEntries[col].slice();
  });

  // Render tab bar when there are 2+ symphonies; hide otherwise.
  if (tabsEl) {
    if (symphonyNames.length > 1) {
      tabsEl.style.display = '';
      tabsEl.innerHTML = symphonyNames.map(function(name) {
        var cls = 'swimlane-tab' + (name === _swimlaneTab ? ' active' : '');
        var count = perSymCounts[name] || 0;
        return '<button type="button" class="' + cls + '" data-symphony="' + esc(name) + '" onclick="selectSwimlaneTab(this.getAttribute(&quot;data-symphony&quot;))">' +
          esc(name) + '<span class="swimlane-tab-count">' + count + '</span></button>';
      }).join('');
    } else {
      tabsEl.style.display = 'none';
      tabsEl.innerHTML = '';
    }
  }

  var totalCards = 0;
  COLUMNS.forEach(function(c) { totalCards += columnItems[c].length; });

  // Fallback when no symphony board data is available: synthesize column entries
  // from active_sessions. Each in-flight session contributes one card placed by
  // phase (blocked → BLOCKED, monitoring_pr / merging / relay_feedback → IN_REVIEW,
  // everything else → IN_PROGRESS). This keeps the swimlane usable in legacy /
  // single-symphony deployments where board_titles/board_snapshot aren't populated.
  if (totalCards === 0 && sessions.length > 0) {
    function colForPhase(ph) {
      if (ph === 'blocked') return 'BLOCKED';
      if (ph === 'monitoring_pr' || ph === 'merging' || ph === 'relay_feedback') return 'IN_REVIEW';
      return 'IN_PROGRESS';
    }
    sessions.forEach(function(sess) {
      var iid = sess && sess.card_id;
      if (!iid) return;
      titles[iid] = sess.card_title || '';
      if (sess.issue_url) issueUrls[iid] = sess.issue_url;
      if (sess.issue_number) issueNums[iid] = sess.issue_number;
      columnItems[colForPhase(sess.phase)].push({iid: iid, symphony: ''});
      totalCards++;
    });
  }

  if (totalCards === 0) {
    var idleAndEmpty = s.phase === 'idle';
    workEl.innerHTML = idleAndEmpty
      ? '<span class="empty-state-warning">&#9888; No cards in the TODO column &mdash; add a card to your GitHub Project board with status <code>TODO</code> to start work.</span>'
      : '<span class="empty-state">Waiting for board snapshot&hellip;</span>';
    if (workCard) workCard.classList.toggle('card-warning', idleAndEmpty);
    return;
  }
  if (workCard) workCard.classList.remove('card-warning');

  function renderCard(entry) {
    var iid = entry.iid;
    var title = titles[iid] || '';
    var num = issueNums[iid] || 0;
    var issueUrl = issueUrls[iid] || '';
    var prUrl = prUrls[iid] || '';
    var sess = sessionsByCard[iid];

    var label = num ? ('#' + num + ' ' + title) : (title || iid);
    var titleHtml = issueUrl && /^https?:\\/\\//i.test(issueUrl)
      ? '<a href="' + esc(issueUrl) + '" target="_blank" rel="noopener" style="color:var(--color-accent-blue);text-decoration:none">' + esc(label) + ' &#8599;</a>'
      : esc(label);

    var prBadge = '';
    if (prUrl && /^https?:\\/\\//i.test(prUrl)) {
      prBadge = ' <a href="' + esc(prUrl) + '" target="_blank" rel="noopener" style="display:inline-block;font-size:10px;padding:1px 6px;margin-left:4px;background:var(--color-bg-pill);border:1px solid var(--color-border);border-radius:10px;color:var(--color-accent-blue);text-decoration:none">PR &#8599;</a>';
    }

    var liveLine = '';
    if (sess) {
      var phaseLabel = formatPhaseLabel(sess.phase || '');
      var stage = sess.performer_stage ? formatPhaseLabel(sess.performer_stage) : '';
      var elapsed = sess.agent_dispatch_at ? (fmtAge(sess.agent_dispatch_at) || '') : '';
      var stale = sess.agent_dispatch_at && (Date.now() - new Date(sess.agent_dispatch_at).getTime()) > STALE_THRESHOLD_MS;
      var elapsedHtml = elapsed
        ? (stale ? '<span style="color:var(--color-degraded)">⚠ ' + esc(elapsed) + '</span>' : esc(elapsed))
        : '';
      var costStr = sess.agent_dispatch_at
        ? '$' + (Number(sess.card_cost_estimate) || 0).toFixed(4)
        : '—';
      var parts = [phaseLabel];
      if (stage) parts.push('<code>' + stage + '</code>');
      if (elapsedHtml) parts.push(elapsedHtml);
      parts.push(esc(costStr));
      liveLine = '<div style="font-size:11px;color:var(--color-text-muted);margin-top:4px">' + parts.join(' · ') + '</div>';
    }

    var clickable = !!sess;
    var clickAttrs = clickable
      ? ' style="cursor:pointer" tabindex="0" data-card-id="' + esc(iid) + '" onclick="showPerformerDetail(this.getAttribute(&quot;data-card-id&quot;))" onkeydown="if(event.key===&quot;Enter&quot;||event.key===&quot; &quot;){showPerformerDetail(this.getAttribute(&quot;data-card-id&quot;))}"'
      : '';

    return '<div class="swimlane-card"' + clickAttrs + '>' +
      '<div style="font-size:13px;line-height:1.35">' + titleHtml + prBadge + '</div>' +
      liveLine +
      '</div>';
  }

  var html = '<div class="swimlane-grid">';
  COLUMNS.forEach(function(col) {
    var entries = columnItems[col];
    var body = entries.length === 0
      ? '<div class="swimlane-empty">&mdash;</div>'
      : entries.map(renderCard).join('');
    html += '<div class="swimlane-col">' +
      '<div class="swimlane-col-header">' + esc(COLUMN_LABELS[col]) + ' <span class="swimlane-col-count">' + entries.length + '</span></div>' +
      '<div class="swimlane-col-body">' + body + '</div>' +
      '</div>';
  });
  html += '</div>';
  workEl.innerHTML = html;
}

function renderCardDetailContent(sess, s) {
  var phaseLabel = formatPhaseLabel(sess.phase || '');
  var cardLink = sess.issue_url && /^https?:\\/\\//i.test(sess.issue_url)
    ? '<a href="' + esc(sess.issue_url) + '" target="_blank" rel="noopener">#' + esc(String(sess.issue_number || '')) + ' ' + esc(sess.card_title || '—') + ' &#8599;</a>'
    : esc(sess.card_title || sess.card_id || '—');
  var rawElapsed = sess.agent_dispatch_at ? (fmtAge(sess.agent_dispatch_at) || '—') : '—';
  var stale = sess.agent_dispatch_at && (Date.now() - new Date(sess.agent_dispatch_at).getTime()) > STALE_THRESHOLD_MS;
  var elapsedHtml = stale
    ? '<span style="color:var(--color-degraded)">⚠ ' + esc(rawElapsed) + '</span>'
    : esc(rawElapsed);
  var cost = sess.agent_dispatch_at
    ? '$' + (sess.card_cost_estimate || 0).toFixed(4) : '—';
  // TODO(054): performer_logs is a flat list from the single agent service; when
  // multi-card is fully live this should be keyed by sess.card_id.
  var logs = Array.isArray(s.performer_logs) ? s.performer_logs.slice(-20) : [];
  var logsHtml = logs.length
    ? logs.map(function(line) { return '<div style="font-family:monospace;font-size:11px;padding:1px 0;white-space:pre-wrap;word-break:break-all">' + esc(line) + '</div>'; }).join('')
    : '<div style="color:var(--color-text-muted);font-style:italic;padding:4px 0">No log entries yet.</div>';
  return '<table style="border-collapse:collapse;font-size:13px;margin-bottom:10px">' +
    '<tr><td style="padding:3px 12px 3px 0;color:var(--color-text-muted)">Phase</td><td style="padding:3px 0">' + phaseLabel + '</td></tr>' +
    '<tr><td style="padding:3px 12px 3px 0;color:var(--color-text-muted)">Card</td><td style="padding:3px 0">' + cardLink + '</td></tr>' +
    '<tr><td style="padding:3px 12px 3px 0;color:var(--color-text-muted)">Elapsed</td><td style="padding:3px 0">' + elapsedHtml + '</td></tr>' +
    '<tr><td style="padding:3px 12px 3px 0;color:var(--color-text-muted)">Cost</td><td style="padding:3px 0">' + esc(cost) + '</td></tr>' +
    '</table>' +
    '<div style="font-size:12px;color:var(--color-text-muted);margin-bottom:4px;text-transform:uppercase;letter-spacing:0.05em">Live Log (last 20 lines)</div>' +
    '<div style="background:var(--color-bg-base);border:1px solid var(--color-border);border-radius:4px;padding:8px;max-height:240px;overflow-y:auto">' + logsHtml + '</div>';
}

function showPerformerDetail(cardId) {
  _selectedCardId = cardId;
  var workSection = document.getElementById('swimlane-section');
  var detailView = document.getElementById('card-detail-view');
  if (workSection) workSection.style.display = 'none';
  if (detailView) {
    detailView.style.display = '';
    var content = document.getElementById('card-detail-content');
    if (!_lastState) {
      if (content) content.innerHTML = '<span class="empty-state">Loading&hellip;</span>';
      return;
    }
    var sessions = Array.isArray(_lastState.active_sessions) ? _lastState.active_sessions : [];
    var sess = sessions.find(function(s) { return s.card_id === cardId; });
    if (sess) {
      if (content) content.innerHTML = renderCardDetailContent(sess, _lastState);
    }
  }
}

function closeCardDetail() {
  _selectedCardId = null;
  var workSection = document.getElementById('swimlane-section');
  var detailView = document.getElementById('card-detail-view');
  if (workSection) workSection.style.display = '';
  if (detailView) detailView.style.display = 'none';
}

function renderActivePerformers(s) {
  var container = document.getElementById('active-performer-tiles');
  var section = document.getElementById('active-performers');
  if (!container || !section) return;
  // active_sessions is a list of {card_id, card_title, phase, performer_stage, ...}
  var sessions = Array.isArray(s.active_sessions) ? s.active_sessions : [];
  var activeSessions = sessions.filter(function(sess) {
    return sess.phase === 'monitoring_performer' || sess.phase === 'monitoring_agent';
  });
  if (activeSessions.length === 0) {
    section.style.display = '';
    var phase = s.phase_label || s.phase || 'idle';
    var cycleCount = s.cycles_completed != null ? s.cycles_completed : '—';
    var summary = (s.board_summary && typeof s.board_summary === 'object') ? s.board_summary : {};
    function count(key) {
      var value = summary[key];
      return Number.isFinite(value) ? value : 0;
    }
    var pollHint = s.last_poll_at ? esc(fmtTime(s.last_poll_at)) : '—';
    var filterHint = s.assignee_filter ? esc(s.assignee_filter) : '';
    container.innerHTML =
      '<div class="ap-idle">' +
        '<div class="ap-idle-title">No active performers</div>' +
        '<div class="ap-idle-rows">' +
          '<div class="ap-idle-row">' +
            '<span class="ap-pill">Phase: <strong>' + esc(phase) + '</strong></span>' +
            '<span class="ap-pill">Cycles: <strong>' + esc(String(cycleCount)) + '</strong></span>' +
            '<span class="ap-pill">Last poll: <strong>' + pollHint + '</strong></span>' +
            (filterHint ? '<span class="ap-pill">Filter: <strong>' + filterHint + '</strong></span>' : '') +
          '</div>' +
          '<div class="ap-idle-row">' +
            '<span class="ap-pill">TODO <strong>' + String(count('TODO')) + '</strong></span>' +
            '<span class="ap-pill">IN_PROGRESS <strong>' + String(count('IN_PROGRESS')) + '</strong></span>' +
            '<span class="ap-pill">IN_REVIEW <strong>' + String(count('IN_REVIEW')) + '</strong></span>' +
            '<span class="ap-pill">DONE <strong>' + String(count('DONE')) + '</strong></span>' +
          '</div>' +
        '</div>' +
      '</div>';
    return;
  }
  section.style.display = '';
  var html = '';
  activeSessions.forEach(function(sess) {
    var stage = sess.performer_stage ? formatPhaseLabel(sess.performer_stage) : '—';
    var title = sess.card_title || sess.card_id || '—';
    var dispatchAt = sess.agent_dispatch_at || s.agent_dispatch_at;
    var elapsed = dispatchAt ? (fmtAge(dispatchAt) || '—') : '—';
    var phaseLabel = formatPhaseLabel(sess.phase || 'active');
    var cardId = sess.card_id || '—';
    html += '<div class="ap-tile">' +
      '<div class="ap-tile-header">' +
        '<div class="ap-tile-role"><span class="perf-dot perf-running"></span>' + stage + '</div>' +
        '<div class="ap-tile-phase">' + phaseLabel + '</div>' +
      '</div>' +
      '<div class="ap-tile-title">' + esc(title) + '</div>' +
      '<div class="ap-tile-meta">' +
        '<span class="ap-pill ap-tile-elapsed">&#9201; <strong>' + esc(elapsed) + '</strong></span>' +
        '<span class="ap-pill">Card: <strong>' + esc(cardId) + '</strong></span>' +
      '</div>' +
      '</div>';
  });
  container.innerHTML = html;
}

function renderDashboardExtras(s) {
  renderActivePerformers(s);
}

function hidePerformerRoleDetail() {
  _performersDetailOpen = false;
  var listView = document.getElementById('performers-page-list-view');
  var detailView = document.getElementById('performers-page-detail-view');
  if (listView) listView.style.display = '';
  if (detailView) detailView.style.display = 'none';
}

function showPerformerRoleDetail(role) {
  _performersSelectedRole = role;
  _performersDetailOpen = true;
  if (_lastState) renderPerformersPage(_lastState);
}

// 049 + 053: Performers page list + drilldown detail
function renderPerformersPage(s) {
  var tbody = document.getElementById('performers-page-tbody');
  if (!tbody) return;
  var util = s.role_utilization || [];
  var listView = document.getElementById('performers-page-list-view');
  var detailView = document.getElementById('performers-page-detail-view');
  var detailEl = document.getElementById('performers-page-detail');
  if (util.length === 0) {
    tbody.innerHTML = '<tr><td colspan="4" class="empty-state">No performer roles configured</td></tr>';
    if (listView) listView.style.display = '';
    if (detailView) detailView.style.display = 'none';
    return;
  }

  // active_sessions is a list of {card_id, card_title, phase, performer_stage, ...}
  var sessions = Array.isArray(s.active_sessions) ? s.active_sessions : [];
  var activeByStage = {};
  sessions.forEach(function(sess) {
    var stage = sess.performer_stage || '';
    var phase = sess.phase || '';
    if ((phase === 'monitoring_performer' || phase === 'monitoring_agent') && stage) {
      activeByStage[stage] = activeByStage[stage] || [];
      activeByStage[stage].push(sess);
    }
  });
  var utilByRole = {};
  util.forEach(function(r) { utilByRole[r.role] = r; });
  if (_performersSelectedRole == null && util.length > 0) {
    _performersSelectedRole = util[0].role;
  }

  tbody.innerHTML = util.map(function(r) {
    var isActive = r.active > 0;
    var isSelected = _performersDetailOpen && _performersSelectedRole === r.role;
    var badge = '<span class="role-status-badge ' + (isActive ? 'role-active' : 'role-idle') + '">' + (isActive ? 'active' : 'idle') + '</span>';
    var cards = (activeByStage[r.role] || []).map(function(sess) {
      return esc((sess.card_title || sess.card_id || '').substring(0, 40));
    }).join('<br>') || '<span class="empty-state">—</span>';
    var maxVal = r.max > 0 ? r.max : 1;
    var activeVal = isActive ? (r.active || 1) : 0;
    var idleVal = Math.max(0, maxVal - activeVal);
    var utilStr = activeVal + ' / ' + idleVal + ' / ' + maxVal;
    var queued = r.queued > 0 ? ' <span style="color:var(--color-accent-yellow)">(+' + r.queued + ' queued)</span>' : '';
    return '<tr class="performers-row' + (isSelected ? ' row-selected' : '') + '" data-role="' + esc(r.role) + '" tabindex="0" role="button" aria-label="Open details for ' + esc(r.role) + '">' +
      '<td style="font-weight:bold">' + esc(r.role) + '</td><td>' + badge + '</td><td style="font-size:12px">' + cards + '</td><td>' + utilStr + queued + '</td></tr>';
  }).join('');

  tbody.querySelectorAll('tr[data-role]').forEach(function(row) {
    row.addEventListener('click', function() {
      var role = row.getAttribute('data-role') || '';
      showPerformerRoleDetail(role);
    });
    row.addEventListener('keydown', function(event) {
      if (event.key === 'Enter' || event.key === ' ') {
        event.preventDefault();
        var role = row.getAttribute('data-role') || '';
        showPerformerRoleDetail(role);
      }
    });
  });

  if (!_performersDetailOpen || !_performersSelectedRole) {
    if (listView) listView.style.display = '';
    if (detailView) detailView.style.display = 'none';
    return;
  }
  if (!listView || !detailView || !detailEl) return;

  var selectedRole = _performersSelectedRole;
  var row = utilByRole[selectedRole] || {role: selectedRole, active: 0, max: 0, queued: 0};
  var selectedSessions = activeByStage[selectedRole] || [];
  var isSelectedRoleActive = selectedSessions.length > 0;
  var statusBadge = '<span class="role-status-badge ' + (isSelectedRoleActive ? 'role-active' : 'role-idle') + ' detail-badge">' + (isSelectedRoleActive ? 'active' : 'idle') + '</span>';
  var roleCardsHtml = selectedSessions.length
    ? '<ul class="detail-list">' + selectedSessions.map(function(sess) {
      var cid = sess.container_id ? ' <span class="muted" style="font-family:monospace;font-size:11px">[' + esc(sess.container_id.slice(0, 12)) + ']</span>' : '';
      return '<li>' + esc(sess.card_title || sess.card_id || '—') + cid + '</li>';
    }).join('') + '</ul>'
    : '<div class="muted">No active session currently running for this role.</div>';
  var sessionStats = s.session_stats;
  var statsLine = sessionStats
    ? esc((sessionStats.title || '') + (sessionStats.title ? ' · ' : '') + sessionStats.files_changed + ' files · +' + sessionStats.lines_added + '/-' + sessionStats.lines_removed + ' lines')
    : 'No session stats available yet.';
  var metrics = s.performer_metrics || {};
  var tokenTotal = derivePerformerTokenTotal(s);
  var metricsLine = (metrics && metrics.pid != null)
    ? (
      'PID ' + String(metrics.pid) +
      ' · CPU ' + esc(metrics.cpu_percent != null ? metrics.cpu_percent.toFixed(1) + '%' : '—') +
      ' · Memory ' + esc(fmtBytes(metrics.memory_bytes)) +
      ' · Tokens ' + esc(tokenTotal != null ? tokenTotal.toLocaleString() : '—')
    )
    : 'No live metrics available yet.';
  var events = Array.isArray(s.performer_events) ? s.performer_events.slice(-12) : [];
  var eventsHtml = events.length
    ? '<ul class="detail-list">' + events.map(function(ev) {
      return '<li><strong>' + esc(ev.type || 'event') + ':</strong> ' + esc(ev.text || '') + '</li>';
    }).join('') + '</ul>'
    : '<div class="muted">No live events yet.</div>';
  // TODO(054): performer_logs is a flat list from the single agent service; when
  // multi-card is fully live this should be keyed by the active session's card_id.
  var logs = Array.isArray(s.performer_logs) ? s.performer_logs.slice(-20) : [];
  var logsHtml = logs.length
    ? '<div class="detail-log">' + logs.map(function(line) { return '<div>' + esc(line) + '</div>'; }).join('') + '</div>'
    : '<div class="muted">No stderr logs yet.</div>';

  var backendLine = esc(s.performer_backend || 'performer');
  if (s.backend_ui_url) {
    try {
      var parsed = new URL(s.backend_ui_url);
      if (parsed.protocol === 'http:' && (parsed.hostname === '127.0.0.1' || parsed.hostname === 'localhost')) {
        backendLine += ' &middot; <a href="' + esc(parsed.href) + '" target="_blank" rel="noopener noreferrer">Open in browser &#8599;</a>';
      }
    } catch (_ignore) {}
  }

  // Build skip-reason diagnostics for sessions associated with this role only.
  var skipReasons = s.session_skip_reasons || {};
  var roleSkips = [];
  selectedSessions.forEach(function(sess) {
    var reason = skipReasons[sess.card_id];
    if (reason) roleSkips.push({card: sess.card_title || sess.card_id, reason: reason});
  });
  var skipHtml = roleSkips.length
    ? '<ul class="detail-list">' + roleSkips.map(function(sr) {
        var r = sr.reason || {};
        var label = esc(r.reason || 'skipped');
        var blockers = Array.isArray(r.blockers) && r.blockers.length
          ? ' (blocked by #' + r.blockers.map(function(n) { return esc(String(n)); }).join(', #') + ')' : '';
        return '<li><span style="color:var(--color-accent-orange)">' + esc(sr.card) + '</span> — ' + label + blockers + '</li>';
      }).join('') + '</ul>'
    : '';

  detailEl.innerHTML =
    '<div><strong>' + humanPhase(selectedRole) + '</strong>' + statusBadge + '</div>' +
    '<div class="muted" style="margin-top:4px">Active / Idle / Max: ' + String(row.active != null ? row.active : 0) + ' / ' + String(Math.max(0, (row.max != null ? row.max : 0) - (row.active != null ? row.active : 0))) + ' / ' + String(row.max != null ? row.max : 0) + '</div>' +
    '<div class="detail-block"><strong>Card Context</strong>' + roleCardsHtml + (skipHtml ? '<div style="margin-top:8px"><span style="color:var(--color-accent-yellow);font-size:12px">Skipped this cycle:</span>' + skipHtml + '</div>' : '') + '</div>' +
    '<div class="detail-block"><strong>Session</strong><div class="muted" style="margin-top:4px">Session: ' + esc(s.agent_session_id || '—') + ' &middot; Uptime: ' + esc(fmtAge(s.agent_dispatch_at) || '—') + '</div><div class="muted" style="margin-top:4px">' + backendLine + '</div><div class="muted" style="margin-top:4px">' + statsLine + '</div></div>' +
    '<div class="detail-block"><strong>Metrics</strong><div class="muted" style="margin-top:4px">' + metricsLine + '</div></div>' +
    '<div class="detail-block"><strong>Live Events</strong>' + eventsHtml + '</div>' +
    '<div class="detail-block"><strong>Process Logs (stderr)</strong>' + logsHtml + '</div>';
  listView.style.display = 'none';
  detailView.style.display = '';
}

function renderHistoryPage(s) {
  renderCycleHistoryInto(document.getElementById('history-page-section'), s.cycle_history);
}

// 049: Personas page
var _personasPageLoaded = false;
function loadPersonasPage(force) {
  var section = document.getElementById('personas-page-section');
  if (!section) return;
  if (_personasPageLoaded && !force) return;  // skip if already loaded
  _personasPageLoaded = true;
  section.innerHTML = '<span class="empty-state">Loading...</span>';
  fetch('/api/personas').then(function(r){ return r.json(); }).then(function(data) {
    var html = data.map(function(p, idx) {
      return '<div style="margin-bottom:16px" data-persona-idx="' + idx + '">' +
        '<label style="font-weight:bold;color:var(--color-text-primary);display:block;margin-bottom:4px">' + esc(p.role) + '</label>' +
        '<textarea class="persona-page-ta" rows="4" style="width:100%;font-family:monospace;font-size:12px;background:var(--color-bg-base);color:var(--color-text-primary);border:1px solid var(--color-border);border-radius:4px;padding:8px;resize:vertical">' + esc(p.instructions || '') + '</textarea>' +
        '<div style="margin-top:4px;display:flex;gap:8px">' +
        '<button class="action-btn persona-save-btn">Save</button>' +
        '<button class="action-btn persona-reset-btn">Reset</button>' +
        '</div><div class="persona-page-msg action-msg"></div></div>';
    }).join('');
    section.innerHTML = html || '<span class="empty-state">No personas configured</span>';
    // Wire save/reset via event delegation (safe for all role names)
    section.querySelectorAll('[data-persona-idx]').forEach(function(el, i) {
      var role = data[i] && data[i].role;
      if (!role) return;
      var ta = el.querySelector('.persona-page-ta');
      var msg = el.querySelector('.persona-page-msg');
      el.querySelector('.persona-save-btn').addEventListener('click', async function() {
        if (!ta || !msg) return;
        try {
          var res = await fetch('/api/personas/' + encodeURIComponent(role), { method: 'PUT', headers: {'Content-Type':'application/json'}, body: JSON.stringify({instructions: ta.value}) });
          msg.textContent = res.ok ? 'Saved' : 'Error saving';
          setTimeout(function(){ msg.textContent=''; }, 3000);
        } catch(e) { msg.textContent = 'Error'; }
      });
      el.querySelector('.persona-reset-btn').addEventListener('click', async function() {
        if (!confirm('Reset ' + role + ' instructions to default?')) return;
        try {
          var res = await fetch('/api/personas/' + encodeURIComponent(role), { method: 'DELETE' });
          if (res.ok) { loadPersonasPage(true); }
          else if (msg) msg.textContent = 'Error resetting';
        } catch(e) { if (msg) msg.textContent = 'Error'; }
      });
    });
  }).catch(function() { section.innerHTML = '<span class="empty-state">Failed to load personas</span>'; });
}

// resetPersonaPage wired inline via event delegation in loadPersonasPage()

setInterval(function() {
  if (!_lastState) return;
  var activeSess = findActivePerformerSession(_lastState);
  var dispatchAt = activeSess ? activeSess.agent_dispatch_at : null;
  if (dispatchAt) {
    var age = fmtAge(dispatchAt) || '—';
    var ageEl = document.getElementById('session-age');
    if (ageEl) ageEl.textContent = 'Agent running for: ' + age;
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
      ? '<span style="font-size:11px;color:var(--color-text-muted);margin-left:6px">using default</span>'
      : '<span style="font-size:11px;color:var(--color-accent-blue);margin-left:6px">custom</span>';
    return '<div style="margin-bottom:16px;border-bottom:1px solid var(--color-bg-elevated);padding-bottom:14px">' +
      '<div style="display:flex;align-items:center;margin-bottom:6px">' +
        '<strong style="font-size:13px">' + esc(p.role) + '</strong>' + badge +
      '</div>' +
      '<textarea id="persona-ta-' + esc(p.role) + '" rows="4" style="width:100%;box-sizing:border-box;' +
        'background:var(--color-bg-base);border:1px solid var(--color-border);color:var(--color-text-primary);padding:6px;font-family:monospace;' +
        'font-size:12px;border-radius:4px;resize:vertical">' + esc(p.instructions) + '</textarea>' +
      '<div style="margin-top:6px;display:flex;gap:8px;align-items:center">' +
        '<button onclick="savePersona(\\\'' + esc(p.role) + '\\\')" ' +
          'id="persona-save-' + esc(p.role) + '" ' +
          'style="background:var(--color-btn-success);border:none;color:var(--color-text-primary);padding:4px 12px;border-radius:4px;cursor:pointer">' +
          'Save</button>' +
        '<button onclick="resetPersona(\\\'' + esc(p.role) + '\\\')" ' +
          'id="persona-reset-' + esc(p.role) + '" ' +
          'style="background:var(--color-bg-elevated);border:1px solid var(--color-border);color:var(--color-text-primary);padding:4px 12px;' +
          'border-radius:4px;cursor:pointer">Reset to defaults</button>' +
        '<span id="persona-msg-' + esc(p.role) + '" style="font-size:12px;color:var(--color-text-muted)"></span>' +
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
      msg.style.color = 'var(--color-accent-red)';
    } else {
      msg.textContent = 'Saved.';
      msg.style.color = 'var(--color-accent-green)';
      setTimeout(function() { if (msg) { msg.textContent = ''; msg.style.color = 'var(--color-text-muted)'; }}, 3000);
      loadPersonas();
    }
  } catch(err) {
    msg.textContent = 'Network error';
    msg.style.color = 'var(--color-accent-red)';
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
      msg.style.color = 'var(--color-accent-green)';
      setTimeout(function() { if (msg) { msg.textContent = ''; msg.style.color = 'var(--color-text-muted)'; }}, 3000);
      loadPersonas();
    } else {
      var data = await res.json().catch(function() { return {}; });
      msg.textContent = 'Error: ' + (data.error || res.status);
      msg.style.color = 'var(--color-accent-red)';
    }
  } catch(err) {
    msg.textContent = 'Network error';
    msg.style.color = 'var(--color-accent-red)';
  }
  btn.disabled = false;
}

// 049: Personas load lazily on /personas page visit via loadPersonasPage().
// The original loadPersonas() targets #personas-section on the main page (now hidden).
// The new loadPersonasPage() targets #personas-page-section and uses event delegation.

var banner = document.getElementById('disconnected-banner');
var es = new EventSource('/events');
es.addEventListener('state_update', function(e) {
  try {
    _lastState = JSON.parse(e.data);
    renderState(_lastState);
    // 049: re-render current page with new state
    var path = location.pathname;
    if (path === '/performers') renderPerformersPage(_lastState);
    else if (path === '/history') renderHistoryPage(_lastState);
    else if (path === '/symphonies' || path.startsWith('/symphonies/')) renderSymphoniesPage(_lastState);
    else renderDashboardExtras(_lastState);
  } catch(err) { console.error('parse error', err); }
});
es.onerror = function() {
  banner.style.display = 'block';
  var dot = document.getElementById('nav-sse-dot');
  if (dot) { dot.classList.add('disconnected'); dot.title = 'SSE disconnected — reconnecting'; }
  var fpBtn = document.getElementById('force-poll-btn');
  if (fpBtn) fpBtn.disabled = true;
};
es.onopen = function() {
  banner.style.display = 'none';
  var dot = document.getElementById('nav-sse-dot');
  if (dot) { dot.classList.remove('disconnected'); dot.title = 'SSE connected'; }
};

document.addEventListener('DOMContentLoaded', function() {
  router(); // initial route
});

document.addEventListener('keydown', function(e) {
  if (e.key === 'Escape' && _selectedCardId) closeCardDetail();
});
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

    # 049: Multi-page routes — same HTML shell, JS router handles rendering
    @app.get("/performers", response_class=HTMLResponse)
    async def dashboard_performers() -> HTMLResponse:
        return HTMLResponse(_DASHBOARD_HTML)

    @app.get("/personas", response_class=HTMLResponse)
    async def dashboard_personas() -> HTMLResponse:
        return HTMLResponse(_DASHBOARD_HTML)

    @app.get("/history", response_class=HTMLResponse)
    async def dashboard_history() -> HTMLResponse:
        return HTMLResponse(_DASHBOARD_HTML)

    # 057: Symphony management routes
    @app.get("/symphonies", response_class=HTMLResponse)
    async def dashboard_symphonies() -> HTMLResponse:
        """Display the symphonies list page (Task 8)."""
        return HTMLResponse(_DASHBOARD_HTML)

    @app.get("/symphonies/{name}", response_class=HTMLResponse)
    async def dashboard_symphony_detail(name: str) -> HTMLResponse:
        """Display the detail page for a specific symphony (Task 8)."""
        return HTMLResponse(_DASHBOARD_HTML)

    @app.get("/admin/config", response_class=HTMLResponse)
    async def dashboard_admin_config() -> HTMLResponse:
        """Display the admin configuration page (Task 11)."""
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
            return JSONResponse(
                    {"error": "A cycle is in progress — please try again shortly", "status": "cycle_in_progress"},
                    status_code=409,
                )
        daemon._webhook_trigger.set()
        return JSONResponse({"status": "accepted"}, status_code=202)

    @app.post("/api/cancel")
    async def cancel_card() -> JSONResponse:
        """Cancel the currently active card (026-card-cancellation).

        Stops the performer, cleans up workspace, moves card to TODO.
        Returns 200 with cancellation result. Returns 409 if a cycle is active.
        """
        if daemon._cycle_active:
            return JSONResponse(
                    {"error": "A cycle is in progress — please try again shortly", "status": "cycle_in_progress"},
                    status_code=409,
                )

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
    # 057 — Symphony Management API endpoints (Tasks 9-10)
    # -----------------------------------------------------------------------

    def _persist_symphony_configs(sym_configs: dict) -> None:
        """Atomically write the current symphony list to config.yaml (FR-020)."""
        import os
        import stat
        import tempfile

        import yaml

        if config_path is None or not config_path.is_file():
            msg = "Config file not available; symphony changes are in-memory only"
            raise ValueError(msg)

        sym_list = [s.model_dump(exclude_none=True) for s in sym_configs.values()]

        loaded = yaml.safe_load(config_path.read_text())
        if not isinstance(loaded, dict):
            msg = "Config file does not contain a YAML mapping"
            raise ValueError(msg)
        loaded["symphonies"] = sym_list

        original_mode = stat.S_IMODE(os.stat(config_path).st_mode)
        tmp_fd, tmp_path = tempfile.mkstemp(
            dir=config_path.parent, prefix=".coordinare_config_", suffix=".yaml.tmp"
        )
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                yaml.safe_dump(
                    loaded, f, default_flow_style=False, allow_unicode=True, sort_keys=False
                )
                f.flush()
                os.fsync(f.fileno())
            os.chmod(tmp_path, original_mode)
            os.replace(tmp_path, str(config_path))
        except Exception:
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
            raise

    @app.get("/api/symphonies")
    async def get_symphonies() -> JSONResponse:
        """List all symphonies with their current state (Task 9)."""
        symphony_configs = daemon.state.get("symphony_configs") or {}
        symphony_states = daemon.state.get("symphony_states") or {}
        config_version = daemon.state.get("config_version", 0)

        env_cache_states = daemon.state.get("env_cache") or {}
        symphonies = []
        for i, (name, cfg) in enumerate(symphony_configs.items()):
            state = symphony_states.get(name)
            # 077: bootstrap status for the at-a-glance overview badge.
            _ec = env_cache_states.get(name)
            symphonies.append({
                "name": name,
                "priority": i,
                "github_project_number": getattr(cfg, "github_project_number", None),
                "env_bootstrap_performer_id": getattr(cfg, "env_bootstrap_performer_id", None),
                "bootstrap_in_flight": bool(getattr(_ec, "bootstrap_in_flight", False)) if _ec else False,
                "cache_dir_ready": bool(getattr(_ec, "cache_dir_ready", False)) if _ec else False,
                "last_bootstrap_succeeded": getattr(_ec, "last_bootstrap_succeeded", None) if _ec else None,
                "last_bootstrap_error": getattr(_ec, "last_bootstrap_error", None) if _ec else None,
                "cycle_count": getattr(state, "cycle_count", 0) if state else 0,
                "error_count": getattr(state, "error_count", 0) if state else 0,
                "last_error": getattr(state, "last_error", None) if state else None,
                "last_poll_at": (
                    getattr(state, "last_poll_at", None).isoformat()
                    if state and isinstance(getattr(state, "last_poll_at", None), datetime)
                    else None
                ),
            })

        return JSONResponse({
            "symphonies": symphonies,
            "config_version": config_version,
        })

    @app.get("/api/symphonies/{name}")
    async def get_symphony(name: str) -> JSONResponse:
        """Get detailed state for a specific symphony (Task 9)."""
        symphony_configs = daemon.state.get("symphony_configs") or {}
        symphony_states = daemon.state.get("symphony_states") or {}

        if name not in symphony_configs:
            return JSONResponse(
                {"error": f"Symphony {name!r} not found"},
                status_code=404,
            )

        cfg = symphony_configs[name]
        state = symphony_states.get(name)

        env_cache = daemon.state.get("env_cache") or {}
        ec = env_cache.get(name)
        env_cache_payload: dict | None = None
        if ec is not None:
            env_cache_payload = {
                "sanitised_name": getattr(ec, "sanitised_name", None),
                "cache_dir": str(getattr(ec, "cache_dir", "")) or None,
                "readme_sha": getattr(ec, "readme_sha", None),
                "bootstrap_in_flight": bool(getattr(ec, "bootstrap_in_flight", False)),
                "cache_dir_ready": bool(getattr(ec, "cache_dir_ready", False)),
                "last_bootstrap_at": (
                    ec.last_bootstrap_at.isoformat()
                    if isinstance(getattr(ec, "last_bootstrap_at", None), datetime)
                    else None
                ),
                "last_bootstrap_succeeded": getattr(ec, "last_bootstrap_succeeded", None),
                "last_bootstrap_error": getattr(ec, "last_bootstrap_error", None),
                # 063 T026d: service-inference summary
                "last_inference_at": (
                    ec.last_inference_at.isoformat()
                    if isinstance(getattr(ec, "last_inference_at", None), datetime)
                    else None
                ),
                "last_inference_skipped_reason": getattr(ec, "last_inference_skipped_reason", None),
                "last_inference_agent_version": getattr(ec, "last_inference_agent_version", None),
                "last_inference_attempts": getattr(ec, "last_inference_attempts", None),
                "last_inference_succeeded": getattr(ec, "last_inference_succeeded", None),
                "last_inference_services": list(getattr(ec, "last_inference_services", []) or []),
            }

        return JSONResponse({
            "name": name,
            "github_project_number": getattr(cfg, "github_project_number", None),
            "enabled": getattr(cfg, "enabled", True),
            "overrides": getattr(cfg, "overrides", None) or {},
            "personas": getattr(cfg, "personas", None) or {},
            "env_spec_files": getattr(cfg, "env_spec_files", None) or ["README.md"],
            "env_bootstrap_performer_id": getattr(cfg, "env_bootstrap_performer_id", None),
            "env_cache": env_cache_payload,
            "state": {
                "cycle_count": getattr(state, "cycle_count", 0) if state else 0,
                "error_count": getattr(state, "error_count", 0) if state else 0,
                "last_error": getattr(state, "last_error", None) if state else None,
                "last_poll_at": (
                    getattr(state, "last_poll_at", None).isoformat()
                    if state and isinstance(getattr(state, "last_poll_at", None), datetime)
                    else None
                ),
                "active_card": getattr(state, "active_card", None) if state else None,
                "board_snapshot": getattr(state, "board_snapshot", None) if state else None,
            } if state is not None else None,
        })

    @app.post("/api/symphonies")
    async def create_symphony(request: Request) -> JSONResponse:
        """Create a new symphony (Task 9)."""
        from coordinare.config import SymphonyConfig

        # Best-effort guard: rejects requests when a cycle is actively running.
        # A race between this check and the daemon starting a new cycle is possible
        # but harmless — the next cycle will load the persisted config anyway.
        if daemon._cycle_active:
            return JSONResponse(
                    {"error": "A cycle is in progress — please try again shortly", "status": "cycle_in_progress"},
                    status_code=409,
                )

        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

        if not isinstance(body, dict):
            return JSONResponse({"error": "Request body must be a JSON object"}, status_code=400)

        raw_name = body.get("name", "")
        if not isinstance(raw_name, str):
            return JSONResponse({"error": "name must be a string"}, status_code=400)
        name = raw_name.strip()
        if not name:
            return JSONResponse({"error": "name is required"}, status_code=400)

        github_project_number = body.get("github_project_number")
        if not github_project_number:
            return JSONResponse({"error": "github_project_number is required"}, status_code=400)
        if isinstance(github_project_number, bool) or not isinstance(github_project_number, int):
            return JSONResponse({"error": "github_project_number must be an integer"}, status_code=400)

        symphony_configs = dict(daemon.state.get("symphony_configs") or {})

        if name in symphony_configs:
            return JSONResponse({"error": f"Symphony {name!r} already exists"}, status_code=409)

        try:
            new_cfg = SymphonyConfig(
                name=name,
                github_project_number=int(github_project_number),
                overrides=body.get("overrides") or None,
                personas=body.get("personas") or None,
                env_spec_files=body.get("env_spec_files") or ["README.md"],
            )
        except Exception:
            _log.warning("symphony_create_validation_failed", name=name, exc_info=True)
            return JSONResponse({"error": "Invalid symphony configuration"}, status_code=400)

        coordinare_cfg = daemon.state.get("coordinare_config")
        if coordinare_cfg is not None:
            try:
                new_cfg.effective_config(coordinare_cfg.global_config)
            except Exception as exc:
                _log.warning("symphony_effective_config_failed", name=name, exc_info=True)
                from pydantic import ValidationError as PydanticValidationError
                if isinstance(exc, PydanticValidationError):
                    msg = "; ".join(
                        f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()
                    )
                else:
                    msg = str(exc)
                return JSONResponse({"error": msg}, status_code=400)

        saved_version = daemon.state.get("config_version", 0)
        symphony_configs[name] = new_cfg
        daemon.state["symphony_configs"] = symphony_configs
        daemon.state["config_version"] = saved_version + 1

        try:
            _persist_symphony_configs(symphony_configs)
        except ValueError:
            _log.debug("symphony_create_persist_skipped_no_config_file", name=name)
        except Exception:
            # Roll back in-memory state so the API contract stays atomic.
            del symphony_configs[name]
            daemon.state["symphony_configs"] = symphony_configs
            daemon.state["config_version"] = saved_version
            _log.warning("symphony_persist_failed", name=name, exc_info=True)
            return JSONResponse({"error": "Failed to persist symphony configuration"}, status_code=500)

        if hasattr(daemon, "_config_reload_trigger"):
            daemon._config_reload_trigger.set()
        if hasattr(daemon, "_webhook_trigger"):
            daemon._webhook_trigger.set()

        return JSONResponse({
            "name": name,
            "github_project_number": new_cfg.github_project_number,
            "enabled": new_cfg.enabled,
            "overrides": new_cfg.overrides or {},
            "personas": new_cfg.personas or {},
            "env_spec_files": new_cfg.env_spec_files,
        }, status_code=201)

    @app.post("/api/symphonies/{name}/validate")
    async def validate_symphony(name: str, request: Request) -> JSONResponse:
        """Validate a symphony's configuration (Task 9). Accepts optional body with proposed overrides/personas for dry-run validation."""
        from coordinare.config import SymphonyConfig

        symphony_configs = daemon.state.get("symphony_configs") or {}

        if name not in symphony_configs:
            return JSONResponse(
                {"error": f"Symphony {name!r} not found"},
                status_code=404,
            )

        cfg = symphony_configs[name]
        coordinare_cfg = daemon.state.get("coordinare_config")

        if not coordinare_cfg:
            return JSONResponse(
                {"error": "Coordinare configuration not available"},
                status_code=500,
            )

        # Accept optional body with proposed overrides/personas for dry-run validation
        proposed_overrides = getattr(cfg, "overrides", None)
        proposed_personas = getattr(cfg, "personas", None)
        raw_body = await request.body()
        if raw_body:
            try:
                body = await request.json()
            except Exception:
                return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
            if not isinstance(body, dict):
                return JSONResponse(
                    {"error": "Request body must be a JSON object"}, status_code=400
                )
            proposed_overrides = body.get("overrides", proposed_overrides)
            proposed_personas = body.get("personas", proposed_personas)

        from pydantic import ValidationError as PydanticValidationError

        try:
            candidate = SymphonyConfig(
                name=name,
                github_project_number=getattr(cfg, "github_project_number", None),
                overrides=proposed_overrides,
                personas=proposed_personas,
            )
            effective = candidate.effective_config(coordinare_cfg.global_config)
            return JSONResponse({
                "valid": True,
                "symphony": name,
                "effective_config": {
                    "github_org": effective.github_org,
                    "github_project_number": effective.github_project_number,
                    "project_name": effective.project_name,
                },
            })
        except PydanticValidationError as exc:
            return JSONResponse(
                {"valid": False, "errors": exc.errors()},
                status_code=400,
            )
        except ValueError as exc:
            return JSONResponse(
                {"valid": False, "errors": [{"msg": str(exc)}]},
                status_code=400,
            )
        except Exception:
            _log.warning("symphony_validate_unexpected_error", name=name, exc_info=True)
            return JSONResponse({"error": "Internal validation error"}, status_code=500)

    @app.put("/api/symphonies/{name}")
    async def update_symphony(name: str, request: Request) -> JSONResponse:
        """Update a symphony's overrides or personas (Task 9)."""
        from coordinare.config import SymphonyConfig

        if daemon._cycle_active:
            return JSONResponse(
                    {"error": "A cycle is in progress — please try again shortly", "status": "cycle_in_progress"},
                    status_code=409,
                )

        symphony_configs = daemon.state.get("symphony_configs") or {}

        if name not in symphony_configs:
            return JSONResponse({"error": f"Symphony {name!r} not found"}, status_code=404)

        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

        if not isinstance(body, dict):
            return JSONResponse({"error": "Request body must be a JSON object"}, status_code=400)

        cfg = symphony_configs[name]
        enabled = body.get("enabled", getattr(cfg, "enabled", True))
        try:
            updated = SymphonyConfig(
                name=name,
                github_project_number=getattr(cfg, "github_project_number", None),
                enabled=enabled,
                overrides=body.get("overrides", getattr(cfg, "overrides", None)),
                personas=body.get("personas", getattr(cfg, "personas", None)),
                env_spec_files=body.get("env_spec_files", getattr(cfg, "env_spec_files", ["README.md"])),
            )
        except Exception:
            _log.warning("symphony_config_validation_failed", name=name, exc_info=True)
            return JSONResponse({"error": "Invalid symphony configuration"}, status_code=400)

        coordinare_cfg = daemon.state.get("coordinare_config")
        if coordinare_cfg is not None:
            try:
                updated.effective_config(coordinare_cfg.global_config)
            except Exception as exc:
                _log.warning("symphony_effective_config_failed", name=name, exc_info=True)
                from pydantic import ValidationError as PydanticValidationError
                if isinstance(exc, PydanticValidationError):
                    msg = "; ".join(
                        f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()
                    )
                else:
                    msg = str(exc)
                return JSONResponse({"error": msg}, status_code=400)

        saved_version = daemon.state.get("config_version", 0)
        previous_cfg = symphony_configs.get(name)
        symphony_configs[name] = updated
        daemon.state["symphony_configs"] = symphony_configs
        daemon.state["config_version"] = saved_version + 1

        try:
            _persist_symphony_configs(symphony_configs)
        except ValueError:
            # config_path is None or file doesn't exist — in-memory-only mode, not an error.
            _log.debug("symphony_update_persist_skipped_no_config_file", name=name)
        except Exception:
            # Actual I/O error — roll back so the in-memory state stays consistent.
            if previous_cfg is not None:
                symphony_configs[name] = previous_cfg
            else:
                del symphony_configs[name]
            daemon.state["symphony_configs"] = symphony_configs
            daemon.state["config_version"] = saved_version
            _log.warning("symphony_persist_failed", name=name, exc_info=True)
            return JSONResponse({"error": "Failed to persist symphony configuration"}, status_code=500)

        if hasattr(daemon, "_config_reload_trigger"):
            daemon._config_reload_trigger.set()
        if hasattr(daemon, "_webhook_trigger"):
            daemon._webhook_trigger.set()

        return JSONResponse({
            "name": name,
            "enabled": getattr(updated, "enabled", True),
            "overrides": getattr(updated, "overrides", None) or {},
            "personas": getattr(updated, "personas", None) or {},
            "env_spec_files": getattr(updated, "env_spec_files", ["README.md"]),
        })

    @app.delete("/api/symphonies/{name}")
    async def delete_symphony(name: str) -> JSONResponse:
        """Remove a symphony (Task 9). Returns 409 if it would remove the last symphony."""
        if daemon._cycle_active:
            return JSONResponse(
                    {"error": "A cycle is in progress — please try again shortly", "status": "cycle_in_progress"},
                    status_code=409,
                )

        symphony_configs = daemon.state.get("symphony_configs") or {}

        if name not in symphony_configs:
            return JSONResponse({"error": f"Symphony {name!r} not found"}, status_code=404)

        if len(symphony_configs) <= 1:
            return JSONResponse(
                {"error": "Cannot delete the last symphony"},
                status_code=409,
            )

        symphony_states = daemon.state.get("symphony_states") or {}
        sym_state = symphony_states.get(name)
        if sym_state is not None and getattr(sym_state, "active_sessions", None):
            return JSONResponse(
                {
                    "error": "Symphony has active sessions; wait for them to complete before deleting",
                    "active_sessions": list(sym_state.active_sessions.keys()),
                },
                status_code=409,
            )

        saved_configs = dict(symphony_configs)
        saved_states = dict(symphony_states)
        saved_version = daemon.state.get("config_version", 0)

        del symphony_configs[name]
        daemon.state["symphony_configs"] = symphony_configs
        daemon.state["config_version"] = saved_version + 1

        symphony_states.pop(name, None)
        daemon.state["symphony_states"] = symphony_states

        try:
            _persist_symphony_configs(symphony_configs)
        except ValueError:
            _log.debug("symphony_persist_skipped_no_config_file", name=name)
        except Exception:
            # Roll back in-memory state so the config and disk stay in sync.
            daemon.state["symphony_configs"] = saved_configs
            daemon.state["symphony_states"] = saved_states
            daemon.state["config_version"] = saved_version
            _log.warning("symphony_persist_failed", name=name, exc_info=True)
            return JSONResponse({"error": "Failed to persist config; delete rolled back"}, status_code=500)

        if hasattr(daemon, "_config_reload_trigger"):
            daemon._config_reload_trigger.set()
        if hasattr(daemon, "_webhook_trigger"):
            daemon._webhook_trigger.set()

        return JSONResponse({"deleted": name})

    @app.post("/api/symphonies/{name}/env-bootstrap")
    async def trigger_env_bootstrap(name: str) -> JSONResponse:
        """Force an env_bootstrap dispatch for a symphony.

        Clears the recorded readme_sha so the next env_cache cycle treats the
        spec files as changed, then fires the cycle trigger. Returns 202 on
        accept; 409 if a bootstrap is already in flight.
        """
        symphony_configs = daemon.state.get("symphony_configs") or {}
        if name not in symphony_configs:
            return JSONResponse(
                {"error": f"Symphony {name!r} not found"}, status_code=404
            )

        sym_cfg = symphony_configs[name]
        if getattr(sym_cfg, "env_bootstrap_performer_id", None) is None:
            return JSONResponse(
                {
                    "error": (
                        f"Symphony {name!r} has no env_bootstrap_performer_id "
                        "configured"
                    )
                },
                status_code=400,
            )

        env_cache_svc = daemon.state.get("env_cache_service")
        if env_cache_svc is None:
            return JSONResponse(
                {"error": "Env cache service not available"}, status_code=503
            )

        performer_id = sym_cfg.env_bootstrap_performer_id
        performer_svcs = daemon.state.get("performer_services_by_id") or {}
        if performer_id not in performer_svcs:
            return JSONResponse(
                {
                    "error": (
                        f"Bootstrap performer {performer_id!r} is not "
                        "registered with the daemon — check that it is "
                        "defined in config.yaml and that coordinare loaded "
                        "it at startup."
                    ),
                    "performer_id": performer_id,
                },
                status_code=503,
            )

        env_cache = daemon.state.get("env_cache") or {}
        cache_state = env_cache.get(name)
        if cache_state is None:
            return JSONResponse(
                {
                    "error": (
                        f"Symphony {name!r} env cache state has not been "
                        "initialised yet — wait one cycle and retry"
                    )
                },
                status_code=503,
            )

        if getattr(cache_state, "bootstrap_in_flight", False):
            return JSONResponse(
                {
                    "error": "A bootstrap is already in flight for this symphony",
                    "status": "bootstrap_in_flight",
                },
                status_code=409,
            )

        # Force the next cycle to detect a SHA mismatch and re-dispatch.
        cache_state.readme_sha = None
        if hasattr(daemon, "_webhook_trigger"):
            daemon._webhook_trigger.set()

        return JSONResponse(
            {
                "status": "accepted",
                "symphony": name,
                "performer_id": performer_id,
            },
            status_code=202,
        )

    @app.get("/api/config/effective")
    async def get_effective_config(symphony: str | None = None) -> JSONResponse:
        """Get effective configuration, optionally scoped to a symphony (Task 9)."""
        coordinare_cfg = daemon.state.get("coordinare_config")
        cfg = coordinare_cfg.global_config if coordinare_cfg else daemon.state.get("config")

        if not cfg:
            return JSONResponse(
                {"error": "Config not available"},
                status_code=500,
            )

        if symphony:
            symphony_configs = daemon.state.get("symphony_configs") or {}
            if symphony not in symphony_configs:
                return JSONResponse(
                    {"error": f"Symphony {symphony!r} not found"},
                    status_code=404,
                )
            sym_cfg = symphony_configs[symphony]
            try:
                effective = sym_cfg.effective_config(cfg)
                return JSONResponse({
                    "github_org": effective.github_org,
                    "github_project_number": effective.github_project_number,
                    "project_name": effective.project_name,
                    "symphony": symphony,
                    "mode": "symphony",
                })
            except Exception:
                _log.error("effective_config_resolution_failed", symphony=symphony, exc_info=True)
                return JSONResponse(
                    {"error": "Failed to compute effective config for this symphony"},
                    status_code=500,
                )

        return JSONResponse({
            "github_org": cfg.github_org,
            "github_project_number": cfg.github_project_number,
            "project_name": cfg.project_name,
            "mode": daemon.state.get("config_mode", "legacy"),
        })

    @app.post("/api/config/reload")
    async def reload_config() -> JSONResponse:
        """Trigger a hot-reload of the configuration (Task 10)."""
        if not hasattr(daemon, "_config_reload_trigger"):
            return JSONResponse(
                {"error": "Config reload not supported"},
                status_code=501,
            )

        daemon._config_reload_trigger.set()
        # Wake the daemon loop in case it's blocked waiting on _webhook_trigger
        # (e.g. poll_interval_seconds=0 / webhook-only mode)
        if hasattr(daemon, "_webhook_trigger"):
            daemon._webhook_trigger.set()
        return JSONResponse({
            "status": "reload_triggered",
            "message": "Configuration reload in progress",
        }, status_code=202)

    _global_cfg_editable = (
        "poll_interval_seconds",
        "heartbeat_interval_seconds",
        "max_concurrent_cards",
        "max_feedback_cycles",
        "max_closed_pr_attempts_per_issue",
        "log_level",
        "output_mode",
        "assignee_filter",
        "human_reviewers",
        "trusted_bot_reviewers",
        "env_cache_root",
    )

    @app.get("/api/config/global")
    async def get_global_config() -> JSONResponse:
        """Return the editable global config fields."""
        coordinare_cfg = daemon.state.get("coordinare_config")
        cfg = coordinare_cfg.global_config if coordinare_cfg else daemon.state.get("config")
        if not cfg:
            return JSONResponse({"error": "Config not available"}, status_code=500)
        def _serialize(v: object) -> object:
            from pathlib import Path as _Path
            return str(v) if isinstance(v, _Path) else v
        return JSONResponse({k: _serialize(getattr(cfg, k, None)) for k in _global_cfg_editable})

    @app.put("/api/config/global")
    async def update_global_config(request: Request) -> JSONResponse:
        """Persist editable global config fields to config.yaml and trigger reload."""
        import os
        import stat
        import tempfile

        import yaml

        if config_path is None or not config_path.is_file():
            return JSONResponse(
                {"error": "Config file not available; changes cannot be persisted"},
                status_code=503,
            )

        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

        if not isinstance(body, dict):
            return JSONResponse({"error": "Request body must be a JSON object"}, status_code=400)

        unknown = set(body) - set(_global_cfg_editable)
        if unknown:
            return JSONResponse({"error": f"Unknown fields: {sorted(unknown)}"}, status_code=400)

        loaded = yaml.safe_load(config_path.read_text())
        if not isinstance(loaded, dict):
            return JSONResponse({"error": "Config file is not a YAML mapping"}, status_code=500)

        for k, v in body.items():
            if v is None:
                loaded.pop(k, None)
            else:
                loaded[k] = v

        # Validate by constructing a throwaway config. All editable fields are
        # included even if they happen to be lists/dicts (e.g. human_reviewers);
        # other nested sections (performers, symphonies, …) that ProjectConfiguration
        # doesn't accept are stripped out so pydantic doesn't reject them.
        try:
            from coordinare.config import ProjectConfiguration
            ProjectConfiguration(**{
                kk: vv for kk, vv in loaded.items()
                if not isinstance(vv, (dict, list)) or kk in _global_cfg_editable
            })
        except Exception as exc:
            return JSONResponse({"error": f"Validation failed: {exc}"}, status_code=400)

        dir_ = config_path.parent
        fd, tmp = tempfile.mkstemp(dir=dir_, suffix=".yaml.tmp")
        try:
            with os.fdopen(fd, "w") as fh:
                yaml.dump(loaded, fh, default_flow_style=False, allow_unicode=True)
            original_mode = stat.S_IMODE(os.stat(config_path).st_mode)
            os.chmod(tmp, original_mode)
            os.replace(tmp, config_path)
        except Exception:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)
            raise

        if hasattr(daemon, "_config_reload_trigger"):
            daemon._config_reload_trigger.set()
            if hasattr(daemon, "_webhook_trigger"):
                daemon._webhook_trigger.set()

        return JSONResponse({"status": "saved", "reload_triggered": hasattr(daemon, "_config_reload_trigger")})

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
