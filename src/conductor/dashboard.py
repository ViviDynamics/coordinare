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

from coordinare.localhost_guard import (
    PermittedOrigins,
    build_permitted,
    install_localhost_guard,
)
from coordinare.services.activity_log import ActivityLog
from coordinare.services.card_ownership import ownership_policy

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator
    from pathlib import Path

    from pydantic import SecretStr

    from coordinare.daemon import CoordinareDaemon
    from coordinare.metrics import CoordinareMetrics
    from coordinare.observability import HealthRegistry
    from coordinare.services.activity_log import ActivityEntry

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
        dispatched = datetime.fromisoformat(agent_dispatch_at_iso)
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


def ownership_hint(config: Any) -> str:
    """Name the card-ownership policy in force, for the dashboard (160 FR-010).

    Asks ``ownership_policy`` rather than reading the config fields directly, so
    the hint cannot drift from the gate it describes: whatever makes
    ``check_board`` narrow the board is exactly what makes this return a string.

    Eligibility is a union, so the hint has three shapes, and the empty one is a
    claim too -- it says no policy is in force and every card is coordinare's.
    Rendering the login alone was correct only while ``include_unassigned`` was a
    modifier that did nothing without it; as an independent opt-in it can be the
    whole policy, which is the shape a deployment authenticating as a GitHub App
    must use, since an App cannot be assigned to an issue.

    The login is shown as the operator spelled it. Matching lowercases both
    sides, but echoing their own configuration back at them is what makes a
    typo'd login findable.
    """
    policy = ownership_policy(config)
    if not policy.active:
        return ""
    raw = getattr(config, "assignee_filter", None)
    login = raw.strip() if isinstance(raw, str) else ""
    if login and policy.include_unassigned:
        return f"{login} + unassigned"
    if login:
        return login
    return "unassigned"

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

    def broadcast_activity(self, entries: list[ActivityEntry]) -> None:
        """138 T011: fan out an activity batch as a tagged payload (FR-001).

        Rides the existing broadcast path, so backpressure behaviour is
        unchanged — a slow client drops the batch and recovers on its next
        reconnect backfill (FR-004).
        """
        if not entries:
            return
        self.broadcast({
            "_event": "activity_event",
            "entries": [e.to_dict() for e in entries],
        })

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

# The size ceiling for the inlined front end, asserted by two separate test
# modules. It lives here, beside what it measures, because it previously
# existed as the literal ``160 * 1024`` in both of them: #348 and #346 were
# each green alone, and together put the page 19 bytes over, turning main red
# on a guard neither PR appeared to touch. One constant cannot diverge.
# #349 tracks moving the JS to /static so this can come back down.
DASHBOARD_HTML_BUDGET_BYTES = 168 * 1024

# 348: how many of a session's ``performer_events`` travel on each session
# summary. The detail panel renders the last 12; the token derivation scans
# backwards for the most recent ``cost`` event. 40 covers both with room to
# spare while keeping the SSE payload bounded when several cards are live.
SESSION_EVENT_LIMIT = 40


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
        # 138 T015: the activity feed's bounded history. Writers are graph nodes
        # that hold the log but no broadcaster, so this sink wiring is the only
        # place the log and the transport meet — without it every pushed entry
        # would reach an open browser only on its next reconnect backfill.
        self.activity_log = ActivityLog()
        self.activity_log.sink = self.broadcaster.broadcast_activity

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
            },
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
                # 343: a step transition is the most frequent meaningful change
                # during a turn. Without it here the trail would only refresh
                # when the stage or phase happened to move, which is minutes to
                # tens of minutes apart -- the exact gap this is meant to close.
                sess.get("workflow_step"),
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
                # 138 T024: the transition needs both sides, and the assignment
                # below discards the old one.
                previous = self._watcher_fingerprint
                self._watcher_fingerprint = fp
                with contextlib.suppress(Exception):
                    self.broadcaster.broadcast(
                        self.build_snapshot(daemon, metrics, health),
                    )
                # After the snapshot broadcast, so the state_update a client
                # expects from a tick still arrives first (065's 1 s contract).
                with contextlib.suppress(Exception):
                    self._record_stage_changes(daemon, previous, fp)
        except asyncio.CancelledError:
            return

    def _record_stage_changes(
        self,
        daemon: CoordinareDaemon,
        previous: tuple | None,
        current: tuple,
    ) -> None:
        """138 T024/T026: derive stage_change entries from the fingerprint delta.

        The watcher already fingerprints ``phase`` and ``performer_stage``, so
        the transition is free here. It is the watcher's only recording duty
        besides releasing dedup bookkeeping for departed cards (FR-024).
        """
        old = {p[0]: (p[4], p[5]) for p in (previous or ())}
        new = {p[0]: (p[4], p[5]) for p in current}
        active = daemon.state.get("active_sessions") or {}
        batch: list[dict[str, Any]] = []
        for sid, (phase, stage) in new.items():
            was = old.get(sid)
            if was == (phase, stage):
                continue
            if was is None:
                text = f"picked up in {stage or phase or 'idle'}"
            elif was[1] != stage:
                text = f"stage → {stage or 'none'}"
            else:
                text = f"phase → {phase or 'idle'}"
            card = (active.get(sid) or {}).get("current_card") or {}
            batch.append({
                "activity_type": "stage_change",
                "card_id": sid,
                "card_number": card.get("issue_number"),
                "card_title": card.get("title", ""),
                "stage": stage or "",
                "text": text,
            })
        if batch:
            self.activity_log.record_many(batch)
        for sid in old.keys() - new.keys():
            self.activity_log.forget_card(sid)

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
        # 138 T013: snapshot the backfill at subscribe time, not when the client
        # first pulls. Everything recorded before this line is in the backfill,
        # everything after arrives on the queue — no entry is sent twice and
        # none is lost in the gap.
        backfill = self.activity_log.snapshot()
        # 065 US1: lazily start the active_sessions watcher on first subscribe
        # so mid-cycle mutations are broadcast without waiting for cycle end.
        if self._watcher_task is None or self._watcher_task.done():
            # Capture baseline synchronously — see _watch_active_sessions docstring.
            self._watcher_fingerprint = self._active_sessions_fingerprint(daemon)
            self._watcher_task = asyncio.create_task(
                self._watch_active_sessions(daemon, metrics, health),
            )
        try:
            # Send current state immediately on connect (FR-011)
            snapshot = self.build_snapshot(daemon, metrics, health)
            yield f"event: state_update\ndata: {json.dumps(snapshot, default=_json_default)}\n\n"
            # Activity backfill, always *after* the initial snapshot (FR-003).
            # snapshot() is already oldest-first — reversing here would render
            # the backfill upside down given the client's prepend.
            if backfill:
                _b = {"_event": "activity_event", "entries": backfill}
                yield f"event: activity_event\ndata: {json.dumps(_b, default=_json_default)}\n\n"
            while True:
                try:
                    payload = await asyncio.wait_for(q.get(), timeout=15.0)
                    if payload is None:
                        # Shutdown sentinel — exit the generator cleanly
                        break
                    # 138 T012: tagged payloads carry their own event name;
                    # everything else serialises byte-identically to pre-138.
                    _name = payload.get("_event") or "state_update"
                    yield f"event: {_name}\ndata: {json.dumps(payload, default=_json_default)}\n\n"
                except TimeoutError:
                    # Keepalive comment — prevents proxy/browser timeout.
                    # Order is load-bearing: test_dashboard.py reads exactly one
                    # message here and asserts this exact string (SC-007).
                    yield ": keepalive\n\n"
                    # 138 T014: SSE comments never surface to EventSource
                    # listeners, so the client needs a JS-visible liveness ping
                    # to drive its silence timer (FR-026).
                    yield 'event: heartbeat\ndata: {"_event": "heartbeat"}\n\n'
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

        card_clarifications = self._annotate_clarifications(
            list(snapshot.card_clarifications) if snapshot else [],
            card_dict,
            snapshot,
        )
        performer_events = list(daemon.state.get("performer_events") or [])
        performer_metrics = daemon.state.get("performer_metrics")
        performer_backend = str(
            (daemon.state.get("agent_dispatch") or {}).get("backend") or "",
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
                # 348: per-session performer telemetry. These fields round-trip
                # through _SESSION_FIELDS, so in shared-pool/multi-symphony mode
                # the session entry is the only place they exist -- the daemon
                # aggregates active_sessions and phase and nothing else, which
                # left every live panel on /performers permanently empty.
                "performer_events": list(sess.get("performer_events") or [])[
                    -SESSION_EVENT_LIMIT:
                ],
                "performer_metrics": sess.get("performer_metrics"),
                "session_stats": self._serialise_session_stats(sess.get("session_stats")),
                "backend_ui_url": sess.get("backend_ui_url"),
                "performer_backend": str(_sess_agent_dispatch.get("backend") or "") or None,
                "session_id": _sess_agent_dispatch.get("session_id") or None,
                # 343: which workflow step, since when, and the observed trail.
                "workflow_step": sess.get("workflow_step"),
                "workflow_step_entered_at": (
                    _ws_at.isoformat()
                    if isinstance((_ws_at := sess.get("workflow_step_entered_at")), datetime)
                    else (str(_ws_at) if _ws_at else None)
                ),
                "workflow_step_trail": list(sess.get("workflow_step_trail") or []),
                "performer_logs": self._session_performer_logs(daemon, _sess_stage, sid),
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
                "slot_queued_since": (
                    _sess_queued.isoformat()
                    if isinstance((_sess_queued := sess.get("slot_queued_since")), datetime)
                    else (str(_sess_queued) if _sess_queued else None)
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
                # 348: mirror the per-session telemetry keys from top-level state
                # so the UI can read the session row unconditionally, without a
                # separate single-symphony code path.
                "performer_events": performer_events[-SESSION_EVENT_LIMIT:],
                "performer_metrics": performer_metrics,
                "session_stats": self._serialise_session_stats(
                    daemon.state.get("session_stats"),
                ),
                "backend_ui_url": daemon.state.get("backend_ui_url"),
                "performer_backend": performer_backend,
                "session_id": _dispatch.get("session_id") or None,
                "workflow_step": daemon.state.get("workflow_step"),
                "workflow_step_entered_at": (
                    _tws.isoformat()
                    if isinstance((_tws := daemon.state.get("workflow_step_entered_at")), datetime)
                    else (str(_tws) if _tws else None)
                ),
                "workflow_step_trail": list(daemon.state.get("workflow_step_trail") or []),
                "performer_logs": performer_logs,
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
                "slot_queued_since": (
                    _top_queued.isoformat()
                    if isinstance((_top_queued := daemon.state.get("slot_queued_since")), datetime)
                    else (str(_top_queued) if _top_queued else None)
                ),
                "container_id": _dispatch.get("container_id"),
            })
        active_session_count = len(active_session_summaries)

        # 138 T016: the ONE key this feature adds (FR-002 is additive-only).
        # Quiet detection's other input, agent_dispatch_at, is already on each
        # session summary above.
        _qt = getattr(
            getattr(daemon.state.get("config"), "stuck_alerts", None),
            "quiet_threshold_seconds",
            300,
        )

        return {
            "activity_quiet_threshold_seconds": _qt if isinstance(_qt, int) else 300,
            # 343: how long a single workflow step may sit before the trail
            # flags it. The stall watchdog's own threshold is the right one --
            # it is documented as "above the slowest legitimate no-output gap",
            # which is exactly the question the trail's timer asks. The quiet
            # threshold is a different question (no activity at all) and is far
            # lower; using it would paint a normal six-minute test baseline
            # amber and train the operator to ignore the colour.
            "stall_timeout_seconds": (
                _st
                if isinstance(
                    (
                        _st := getattr(
                            getattr(daemon.state.get("coordinare_config"), "dispatcher_dedup", None),
                            "stall_timeout_seconds",
                            0,
                        )
                    ),
                    int,
                )
                else 0
            ),
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
            "open_questions": self._serialize_open_questions(daemon, snapshot, card_dict),
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
            # 160: whether that filter also admits unassigned cards. Without it
            # the hint reads "Filter: coordinare-bot" on a board that is in fact
            # picking up everything nobody claimed -- true as far as it goes and
            # wrong about what coordinare will do next.
            "include_unassigned": bool(
                getattr(daemon.state.get("config"), "include_unassigned", False),
            ),
            # 160: the policy named as one string, computed from the same
            # ownership_policy() the gate consults. The two raw fields above are
            # kept for consumers that predate this key; the UI reads only this.
            "ownership_hint": ownership_hint(daemon.state.get("config")),
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
    def _serialize_open_questions(
        daemon: Any,
        snapshot: Any,
        card_dict: dict[str, Any],
    ) -> list[dict[str, Any]]:
        """355: card-attributed open questions, scoped to live blocked state.

        Each entry is ``{card_id, card_number, card_title, stage, text,
        asked_at, issue_url}`` instead of the pre-355 bare string, so the
        Open Questions panel can say which card asked what, when, and link
        to it.

        Sessions are authoritative: a question renders only while its owning
        session's phase is still ``blocked`` (FR: the panel must not outlive
        the condition that produced it — the #355 incident showed an
        env-blocker question persisting after the card had left the Blocked
        column). The legacy fallback below keeps single-card / pre-355
        snapshots renderable, gated on the daemon's LIVE phase rather than
        the snapshot's, which is what makes the stale panel go away.
        """
        questions: list[dict[str, Any]] = []
        sessions = daemon.state.get("active_sessions") or {}
        for sid, sess in sessions.items():
            if not isinstance(sess, dict) or str(sess.get("phase") or "") != "blocked":
                continue
            sess_card = sess.get("current_card") or {}
            sess_card = sess_card if isinstance(sess_card, dict) else {}
            asked_at = sess.get("last_blocked_notified_at")
            stage = sess.get("performer_stage")
            questions.extend(
                {
                    "card_id": str(sid),
                    "card_number": sess_card.get("issue_number"),
                    "card_title": str(sess_card.get("title", "")),
                    "stage": str(stage) if isinstance(stage, str) else "",
                    "text": str(q),
                    "asked_at": asked_at.isoformat() if isinstance(asked_at, datetime) else None,
                    "issue_url": str(sess_card.get("issue_url", "")) or None,
                }
                for q in sess.get("open_questions") or []
            )
        if questions or sessions:
            return questions
        # Legacy fallback: flat bare strings from the snapshot (single-card
        # mode, or a pre-355 persisted snapshot). Attributed to the top-level
        # active card and gated on the LIVE phase so a question whose card
        # has moved on never renders.
        live_phase = str(daemon.state.get("phase", "idle"))
        if live_phase != "blocked" or not snapshot or not snapshot.open_questions:
            return []
        asked_at = snapshot.last_blocked_notified_at
        return [
            {
                "card_id": snapshot.active_card_id,
                "card_number": card_dict.get("issue_number") or snapshot.active_card_issue_number,
                "card_title": str(
                    card_dict.get("title") or snapshot.active_card_title or "",
                ),
                "stage": str(snapshot.performer_stage) if snapshot.performer_stage else "",
                "text": str(q),
                "asked_at": asked_at.isoformat() if isinstance(asked_at, datetime) else None,
                "issue_url": str(card_dict.get("issue_url", "")) or snapshot.active_card_issue_url,
            }
            for q in snapshot.open_questions
        ]

    @staticmethod
    def _annotate_clarifications(
        rounds: list[Any],
        card_dict: dict[str, Any],
        snapshot: Any,
    ) -> list[dict[str, Any]]:
        """355: label each Clarification History round with its card and stage.

        The snapshot's rounds belong to the card that was active when they
        were collected (flat state mirrors the active card in both single- and
        multi-card modes), so attribution comes from the top-level card
        fields. Round content is otherwise preserved byte-identically.
        """
        if not rounds:
            return []
        card_number = card_dict.get("issue_number") or (
            snapshot.active_card_issue_number if snapshot else None
        )
        card_title = str(card_dict.get("title") or (snapshot.active_card_title if snapshot else "") or "")
        stage = (snapshot.performer_stage if snapshot else None) or ""
        card_id = snapshot.active_card_id if snapshot else None
        issue_url = str(card_dict.get("issue_url", "")) or (
            snapshot.active_card_issue_url if snapshot else None
        )
        annotated: list[dict[str, Any]] = []
        for rnd in rounds:
            if isinstance(rnd, dict):
                entry = dict(rnd)
                # setdefault, not overwrite: a round that already carries its
                # own attribution (a future writer) must not be relabelled.
                entry.setdefault("card_id", card_id)
                entry.setdefault("card_number", card_number)
                entry.setdefault("card_title", card_title)
                entry.setdefault("stage", stage)
                entry.setdefault("issue_url", issue_url)
                annotated.append(entry)
            else:
                annotated.append(rnd)
        return annotated

    @staticmethod
    def _session_performer_logs(daemon: Any, stage: str, card_id: str) -> list[str]:
        """Return the stderr/stdout buffer of the service serving this card (348).

        Mirrors monitor_performer's service resolution but is strictly
        read-only: it reads the pool's existing slot rather than calling
        ``acquire()``, so rendering the dashboard can never consume a
        performer slot.

        The buffer lives on the service *instance*, and with
        ``max_concurrency > 1`` each slot holds a different instance -- so
        resolving by slot is what makes the logs belong to this card rather
        than to whichever card the primary service happened to run last.
        When the card holds no slot there is no honest answer, so this
        returns nothing rather than another card's output. The legacy
        single-service path (no slot pools at all) keeps its old behaviour.
        """
        slot_mgr = daemon.state.get("slot_manager")
        pools = getattr(slot_mgr, "pools", None)
        service: Any = None
        if isinstance(pools, dict) and pools:
            pool = pools.get(stage)
            slots = getattr(pool, "active_slots", None) or {}
            slot = slots.get(card_id)
            services = getattr(pool, "services", None) or []
            if slot is None:
                return []
            idx = getattr(slot, "service_index", 0)
            if 0 <= idx < len(services):
                service = services[idx]
        else:
            service = daemon.state.get("agent_service")
        if service is None or not hasattr(service, "get_agent_logs"):
            return []
        with contextlib.suppress(Exception):
            return list(service.get_agent_logs())
        return []

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
            cache = (daemon.state.get("env_cache") or {}).get(sym_name)
            sym_entry = {
                "name": sym_name,
                "priority": i,
                "github_project_number": getattr(sym_cfg, "github_project_number", None),
                "env_bootstrap_performer_id": getattr(sym_cfg, "env_bootstrap_performer_id", None),
                "bootstrap_in_flight": bool(getattr(cache, "bootstrap_in_flight", False)),
                "cache_dir_ready": bool(getattr(cache, "cache_dir_ready", False)),
                "last_bootstrap_succeeded": getattr(cache, "last_bootstrap_succeeded", None),
                "last_bootstrap_error": getattr(cache, "last_bootstrap_error", None),
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
  --color-ev-quiet:           #1e2430;
  --color-ev-stall:           #2a2010;
  --color-ev-stuck:           #3a1520;
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
.ev-stage_change { background: var(--color-ev-progress); color: var(--color-accent-green); }
.ev-recovered { background: var(--color-ev-progress); color: var(--color-accent-green); }
.ev-quiet     { background: var(--color-ev-quiet); color: var(--color-text-muted); }
.ev-stall     { background: var(--color-ev-stall); color: var(--color-accent-orange); }
.ev-stuck     { background: var(--color-ev-stuck); color: var(--color-accent-red); }
/* 138: activity feed */
.af-controls { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 8px; font-size: 12px; }
.af-controls label { color: var(--color-text-muted); }
/* min-width:0 lets a long card title shrink instead of widening the row past
   the card at the narrowest supported viewport. */
#af-filter { background: var(--color-bg-base); color: var(--color-text-primary); border: 1px solid var(--color-border); border-radius: 4px; padding: 3px 6px; font-family: inherit; font-size: 12px; max-width: 100%; min-width: 0; }
#af-filter:focus-visible { outline: 2px solid var(--color-accent-blue); outline-offset: 1px; }
.af-liveness { margin-left: auto; color: var(--color-accent-green); white-space: nowrap; }
.af-liveness.af-not-live { color: var(--color-accent-yellow); }
.af-log { max-height: 320px; overflow-y: auto; overflow-x: hidden; background: var(--color-bg-base); border: 1px solid var(--color-bg-elevated); border-radius: 4px; font-size: 12px; }
.af-raw { white-space: pre-wrap; overflow-wrap: anywhere; padding-left: 12px; }
.af-tool { white-space: pre-wrap; overflow-wrap: anywhere; }
.af-raw-label { color: var(--color-text-muted); padding: 6px 12px; }
.af-row details { grid-column: 1 / -1; min-width: 0; width: 100%; }
.af-row summary { cursor: pointer; }
.af-row { display: grid; grid-template-columns: 62px minmax(0, 1fr); gap: 6px; padding: 3px 6px; border-bottom: 1px solid var(--color-bg-surface); align-items: start; }
.af-row:last-child { border-bottom: none; }
.af-time { color: var(--color-text-muted); white-space: nowrap; font-size: 11px; padding-top: 2px; }
.af-body { min-width: 0; word-break: break-word; color: var(--color-text-primary); }
.af-kind { display: inline-block; padding: 0 5px; border-radius: 3px; font-size: 10px; letter-spacing: .04em; font-weight: bold; }
.af-card { color: var(--color-text-muted); margin: 0 4px; }
/* 353: truncation markers fold into the group they truncated; the badge reuses
   existing colour tokens so no new contrast pairing is introduced. */
.af-truncated { display: inline-block; margin-left: 6px; padding: 0 5px; border-radius: 3px; font-size: 10px; color: var(--color-text-muted); border: 1px solid var(--color-border); }
.af-stale-note { color: var(--color-accent-yellow); font-size: 11px; padding: 5px 6px; border-bottom: 1px solid var(--color-bg-surface); }
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
.cfg-btn { background: var(--color-bg-elevated); border: 1px solid var(--color-border); color: var(--color-accent-blue); border-radius: 4px; padding: 4px 12px; cursor: pointer; font-size: 12px; font-family: monospace; }
.cfg-btn:hover:not(:disabled) { border-color: var(--color-accent-blue); }
.cfg-btn:disabled { opacity: .45; cursor: not-allowed; }
.cfg-btn-danger { color: var(--color-accent-red); }
.cfg-btn-danger:hover:not(:disabled) { border-color: var(--color-accent-red); }
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
@media (max-width: 899px) {
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
    <a href="/config" class="nav-link" onclick="navigate(event,'/config')">Config</a>
    <a href="/assistant" id="nav-assistant" class="nav-link" style="display:none" onclick="navigate(event,'/assistant')">Assistant</a>
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

<!-- 138: activity feed -->
<div id="activity-feed-card" class="card full">
  <h2>Activity</h2>
  <div class="af-controls">
    <label for="af-filter">Card</label>
    <select id="af-filter" onchange="onActivityFilterChange()" aria-label="Filter activity by card">
      <option value="">All cards</option>
    </select>
    <span id="af-liveness" class="af-liveness" role="status" aria-live="polite">Live</span>
  </div>
  <div id="af-stale-note" class="af-stale-note" style="display:none">
    Stream not live &mdash; entries below may be out of date.
  </div>
  <div id="activity-feed" class="af-log" role="log" aria-live="polite" aria-relevant="additions"
       aria-label="Activity feed, newest first"></div>
  <div id="af-empty" class="empty-state">
    No activity yet &mdash; this feed covers only the current daemon run.
  </div>
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

<div id="config-page" style="display:none">
  <div class="card">
    <h2>Configuration</h2>
    <div id="config-page-section" aria-live="polite"><span class="empty-state">Loading...</span></div>
  </div>
</div>

</div><!-- /#main-content -->

<script src="/static/activity-streams.js"></script>
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

// 343: the step trail. Completed steps from the observed transitions, the
// current one marked and carrying a live elapsed timer seeded from snapshot
// state -- so it survives a browser reload and a daemon restart, neither of
// which a client-side counter would.
function stepTrailHtml(sess, slowAfterSeconds) {
  var cur = sess.workflow_step;
  if (!cur) return '';
  var trail = Array.isArray(sess.workflow_step_trail) ? sess.workflow_step_trail : [];
  var shortName = function(n) { var s = String(n || ''); var i = s.indexOf('.'); return i < 0 ? s : s.slice(i + 1); };
  // Only steps before the current one are done; the current entry is the last.
  var done = trail.slice(0, Math.max(0, trail.length - 1));
  var parts = done.slice(-6).map(function(e) {
    return '<span class="muted">' + esc(shortName(e.step)) + ' &check;</span>';
  });
  var age = fmtAge(sess.workflow_step_entered_at);
  var secs = sess.workflow_step_entered_at
    ? Math.floor((Date.now() - new Date(sess.workflow_step_entered_at).getTime()) / 1000) : 0;
  // 0 means the operator has not said what "too long" is, so say nothing.
  var slow = slowAfterSeconds > 0 && secs > slowAfterSeconds;
  var style = slow ? 'color:var(--color-degraded)' : 'color:var(--color-accent-blue)';
  parts.push('<span style="' + style + ';font-weight:bold" data-step-since="'
    + esc(sess.workflow_step_entered_at || '') + '">&bull; ' + esc(shortName(cur))
    + (age ? ' ' + esc(age) : '') + '</span>');
  var more = done.length > 6 ? '<span class="muted">+' + String(done.length - 6) + ' earlier &middot; </span>' : '';
  return '<div class="step-trail" style="margin-top:4px;font-size:12px">' + more + parts.join('<span class="muted"> &middot; </span>') + '</div>';
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
      var idleFilterText = s.ownership_hint ? esc(s.ownership_hint) : '';
      var idleFilter = idleFilterText ? ' <span style="color:var(--color-text-muted);font-size:12px">(filter: ' + idleFilterText + ')</span>' : '';
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

  // Open questions (355: card-attributed; the server scopes the list to
  // sessions still in the blocked phase, so a question never outlives its
  // card's blocked state). Each entry is {card_id, card_number, card_title,
  // stage, text, asked_at, issue_url}; a bare legacy string renders as the
  // question text alone with the panel-level issue link.
  var qCard = document.getElementById('questions-card');
  var qList = document.getElementById('questions-list');
  if (s.open_questions && s.open_questions.length > 0) {
    qCard.style.display = '';
    qList.innerHTML = s.open_questions.map(function(q) {
      var obj = (typeof q === 'string') ? { text: q } : (q || {});
      var text = (typeof obj.text === 'string') ? obj.text : '';
      var head = '';
      if (obj.card_number != null) {
        var num = '#' + String(obj.card_number);
        head = obj.issue_url
          ? '<a href="' + esc(obj.issue_url) + '" target="_blank" rel="noopener">' + esc(num) + '</a>'
          : esc(num);
        if (obj.card_title) head += ' ' + esc(obj.card_title);
      } else if (obj.card_title) {
        head = esc(obj.card_title);
      }
      if (obj.stage) head += ' <span class="muted">(' + esc(obj.stage) + ')</span>';
      var ageTxt = obj.asked_at ? fmtAge(obj.asked_at) : null;
      var age = ageTxt ? ' <span class="muted">asked ' + esc(ageTxt) + ' ago</span>' : '';
      var body = head ? head + ': ' + esc(text) : esc(text);
      if (!obj.card_number) {
        var issueHref = s.issue_url && /^https?:\\/\\//i.test(s.issue_url) ? s.issue_url : null;
        if (issueHref) {
          body += ' <a href="' + esc(issueHref) + '" target="_blank" rel="noopener" style="font-size:0.85em;white-space:nowrap">View issue &#8599;</a>';
        }
      }
      return '<li>' + body + age + '</li>';
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

  // Clarification history (355: each round labelled with the card and stage
  // it belongs to; rounds stay collapsed as before)
  var clCard = document.getElementById('clarifications-card');
  var clList = document.getElementById('clarifications-list');
  if (s.card_clarifications && s.card_clarifications.length > 0) {
    clCard.style.display = '';
    clList.innerHTML = s.card_clarifications.map(function(round, i) {
      round = round || {};
      var qs = (round.questions || []).map(function(q) {
        return '<li>' + esc(q) + '</li>';
      }).join('');
      var ans = round.answer ? '<div class="qa-round-a">&#x1F4AC; ' + esc(round.answer) + '</div>' : '';
      var attr = '';
      if (round.card_number != null) {
        var num = '#' + String(round.card_number);
        attr = round.issue_url
          ? '<a href="' + esc(round.issue_url) + '" target="_blank" rel="noopener">' + esc(num) + '</a>'
          : esc(num);
        if (round.card_title) attr += ' ' + esc(round.card_title);
      } else if (round.card_title) {
        attr = esc(round.card_title);
      }
      if (round.stage) attr += ' <span class="muted">(' + esc(round.stage) + ')</span>';
      var header = 'Round ' + (i + 1) + (attr ? ' — ' + attr : '');
      return '<div class="qa-round">' +
        '<div style="font-size:11px;color:var(--color-text-muted);margin-bottom:4px">' + header + '</div>' +
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
  // 348/305: prefer the monitored session; top-level is empty in shared-pool
  // mode, which is why live usage displayed a flat zero.
  var tokenSource = activeSess || s;
  var cardTokensTotal = (tokenSource.card_tokens_total || s.card_tokens_total || 0);
  if (!(cardTokensTotal > 0)) {
    var fallbackTokens = derivePerformerTokenTotal(tokenSource);
    if (fallbackTokens == null && tokenSource !== s) fallbackTokens = derivePerformerTokenTotal(s);
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
  var pages = ['dashboard-page','performers-page','personas-page','history-page','symphonies-page','admin-config-page','config-page','assistant-page'];
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
  var titles = {'/':'Dashboard — Coordinare','/performers':'Performers — Coordinare','/personas':'Personas — Coordinare','/history':'History — Coordinare','/symphonies':'Symphonies — Coordinare','/admin/config':'Global Config — Coordinare','/config':'Configuration — Coordinare'};
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
    _symCfgHash = res.headers.get('ETag');
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
      + ' <button class="action-btn" id="sym-wiki-init-btn">Init wiki</button>'
      + ' <span id="sym-wiki-init-msg" class="action-msg"></span>'
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
  var wikiBtn = document.getElementById('sym-wiki-init-btn');
  if (wikiBtn) {
    wikiBtn.addEventListener('click', async function() {
      var msg = document.getElementById('sym-wiki-init-msg');
      wikiBtn.disabled = true;
      msg.textContent = 'Dispatching...';
      msg.style.color = 'var(--color-text-muted)';
      try {
        var r = await fetch('/api/symphonies/' + encodeURIComponent(name) + '/wiki-init', {method: 'POST'});
        var d = await r.json();
        if (r.ok) {
          msg.textContent = 'Accepted — building the wiki, a seed PR will open on completion';
          msg.style.color = 'var(--color-accent-green)';
          setTimeout(function() { loadSymphonyDetail(name, el); }, 1500);
        } else {
          msg.textContent = d.error || ('Error ' + r.status);
          msg.style.color = 'var(--color-accent-red)';
          wikiBtn.disabled = false;
        }
      } catch(e) {
        msg.textContent = 'Network error';
        msg.style.color = 'var(--color-accent-red)';
        wikiBtn.disabled = false;
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
        headers: {'Content-Type':'application/json', 'If-Match': _symCfgHash || ''},
        body: JSON.stringify({overrides: Object.keys(overrides).length ? overrides : null, personas: Object.keys(personas).length ? personas : null, enabled: enabled, env_spec_files: specFilesList}),
      });
      var d = await r.json();
      if (r.ok) {
        _symCfgHash = r.headers.get('ETag') || _symCfgHash;
        msg.textContent = 'Saved';
        msg.style.color = 'var(--color-accent-green)';
      } else {
        msg.textContent = versionErrorText(r.status, d);
        msg.style.color = 'var(--color-accent-red)';
      }
    } catch(e) { msg.textContent = 'Network error'; msg.style.color = 'var(--color-accent-red)'; }
    setTimeout(function(){ if(msg) msg.textContent=''; }, 4000);
  });
  document.getElementById('sym-delete-btn').addEventListener('click', async function() {
    if (!confirm('Delete symphony "' + name + '"? This cannot be undone.')) return;
    var msg = document.getElementById('sym-save-msg');
    try {
      var r = await fetch('/api/symphonies/' + encodeURIComponent(name), {
        method: 'DELETE',
        headers: {'If-Match': _symCfgHash || ''},
      });
      var d = await r.json();
      if (r.ok) {
        navigate(null, '/symphonies');
      } else {
        msg.textContent = versionErrorText(r.status, d);
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
      headers: {'Content-Type':'application/json', 'If-Match': (await currentConfigVersion()) || ''},
      body: JSON.stringify({name: nameVal.trim(), github_project_number: Number(projVal)}),
    });
    var d = await r.json();
    if (r.ok) {
      navigate(null, '/symphonies/' + encodeURIComponent(nameVal.trim()));
    } else {
      msg.textContent = versionErrorText(r.status, d);
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
    // 156: the version this page is about to edit. Sent back on save so a
    // colleague's edit landing in between is refused rather than overwritten.
    _adminCfgHash = res.headers.get('ETag');
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
      if (!_adminCfgHash) {
        // Fail closed, like the assistant's Apply: without a version to write
        // against the server will refuse anyway, and saying so here explains what
        // to do instead of surfacing a bare 428.
        msg.textContent = 'Cannot save: this page did not load a configuration version. '
                        + 'Reload the page and try again.';
        msg.style.color = 'var(--color-accent-red)';
        return;
      }
      payload.expected_hash = _adminCfgHash;
      var r = await fetch('/api/config/global', {
        method: 'PUT',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify(payload),
      });
      var d = await r.json();
      if (r.ok) {
        msg.textContent = 'Saved — reload triggered';
        msg.style.color = 'var(--color-accent-green)';
        // Carry the new version forward, so a second save without reloading is
        // guarded too. Setting this to null would have made the guard hold exactly
        // once per page load while looking as though it always held.
        _adminCfgHash = d.new_hash || null;
      } else if (r.status === 409) {
        msg.textContent = 'Not saved: the configuration changed since this page loaded. '
                        + 'Reload to see the current values, then make your change again.';
        msg.style.color = 'var(--color-accent-red)';
      } else {
        msg.textContent = versionErrorText(r.status, d);
        msg.style.color = 'var(--color-accent-red)';
      }
    } catch(e) { msg.textContent = 'Network error'; msg.style.color = 'var(--color-accent-red)'; }
    setTimeout(function(){ if(msg) msg.textContent=''; }, 5000);
  });
}

// spec 081-config-ui: full editable config surface (US1 read T018-T019, US2 edit T033).
// Renders all sections, masking secrets and preserving ${VAR} placeholders as
// delivered by /api/config/all. Scalar sections (global) and the orchestration
// catalogs (endpoints / model_endpoints / modes) are editable in-place with
// per-field validation, hot-reload-vs-restart feedback, optimistic-concurrency
// (409) reload prompts, and catalog create/edit/delete with delete-protection.
var CFG_CATALOGS = ['endpoints', 'model_endpoints', 'modes'];
// --- Reference dropdowns (spec 081 UI affordance) -------------------------
// Map a reference FIELD LABEL to the catalog whose entry names are valid
// values, plus whether the field is nullable (offers a "— none —" option).
// Labels are unique across reference fields (confirmed: setting label == key),
// so a flat label-keyed map is sufficient.
var _CFG_REFS = {
  endpoint:   {catalog: 'endpoints',       nullable: false},
  tool:       {catalog: 'model_endpoints', nullable: false},
  thinking:   {catalog: 'model_endpoints', nullable: true},
  classifier: {catalog: 'model_endpoints', nullable: true},
  mode:       {catalog: 'modes',           nullable: true}
};
// Option lists for each referenceable catalog, rebuilt on every config load
// from the /api/config/all payload already in hand (no extra fetch).
var _cfgOptions = {endpoints: [], model_endpoints: [], modes: []};
var _cfgTab = null;   // active config section id (tab)
function cfgTabId(secId)   { return 'cfg-tab-' + secId; }
function cfgPanelId(secId) { return 'cfg-panel-' + secId; }
var CFG_HINT = 'font-size:11px;color:var(--color-text-muted);margin-top:2px';
var CFG_INPUT = 'width:100%;box-sizing:border-box;background:var(--color-bg-base);border:1px solid var(--color-border);'
  + 'border-radius:4px;color:var(--color-text-primary);font-family:var(--font-mono,monospace);font-size:12px;padding:5px 7px';
var _cfgHash = null;      // optimistic-concurrency baseline (content_hashes.config_yaml)
var _adminCfgHash = null; // 156: the same, for the older Global Config page (from its ETag)
// 158 (#241): each editing page's config version. Do NOT consolidate these into one
// variable -- see _version_headers in dashboard.py for why that reintroduces the lost
// edit this guards against.
var _symCfgHash = null;
var _personaCfgHash = null;

// A create has no page GET to take a version from -- the symphonies list is drawn
// from the SSE state, not fetched -- so it asks for one.
async function currentConfigVersion() {
  try {
    var r = await fetch('/api/symphonies');
    return r.headers.get('ETag');
  } catch(e) { return null; }
}

// 158: 'you are out of date' is only actionable if it says so. But 409 is not only
// ours -- 'Symphony already exists' is a 409 too, and telling someone to reload over
// a duplicate name would send them chasing a concurrent edit that never happened.
// _version_refusal marks its own refusals with `conflict: true`; everything else
// keeps the server's message.
function versionErrorText(status, body) {
  if (status === 409 && body && body.conflict) {
    return 'Config changed since this page loaded. Reload and re-apply.';
  }
  if (status === 428) return (body && body.error) || 'This save needs a config version.';
  return (body && body.error) || ('Error ' + status);
}
var _cfgVersion = null;
var _cfgRoutingHash = null;     // optimistic-concurrency baseline (routing.yaml, spec-078)
var _cfgRoutingAvail = false;   // whether a routing table is mounted+present on this host
var CFG_WIRE = ['openai', 'anthropic'];
var CFG_STRATEGY = ['normalize', 'reroute'];

function cfgFmtValue(v) {
  if (v === null || v === undefined) return '<span style="color:var(--color-text-muted)">(unset)</span>';
  if (Array.isArray(v)) return v.length ? esc(v.join(', ')) : '<span style="color:var(--color-text-muted)">(empty)</span>';
  if (typeof v === 'boolean') return v ? 'true' : 'false';
  return esc(String(v));
}

function cfgBadge(text, color) {
  return '<span style="display:inline-block;font-size:10px;padding:1px 6px;border-radius:8px;margin-left:6px;'
    + 'background:var(--color-bg-base);border:1px solid ' + color + ';color:' + color + '">' + esc(text) + '</span>';
}

function cfgFlash(el, text, kind) {
  if (!el) return;
  var c = {green: 'var(--color-accent-green)', red: 'var(--color-accent-red)',
    orange: 'var(--color-accent-orange,#d29922)', blue: 'var(--color-accent-blue)',
    muted: 'var(--color-text-muted)'}[kind] || 'var(--color-text-muted)';
  el.innerHTML = '';
  el.textContent = text;
  el.style.color = c;
}

// Build the {endpoints, model_endpoints, modes} option index from the loaded
// sections. Each option is {value, hint}; only referenceable catalogs are kept.
function cfgBuildOptions(sections) {
  var idx = {endpoints: [], model_endpoints: [], modes: []};
  (sections || []).forEach(function(sec) {
    if (idx[sec.id] === undefined) return;
    idx[sec.id] = (sec.items || []).map(function(it) {
      return {value: it.id, hint: cfgOptHint(sec.id, it)};
    });
  });
  return idx;
}

// A short, self-documenting hint for one catalog option, derived only from
// fields known to exist on the schema (no invented field names): modes show
// their `tool`, model_endpoints show their `endpoint`.
function cfgOptHint(catalog, item) {
  // Key off the stable schema key (last dotted component), falling back to the
  // user-facing label only when s.key is absent — labels can be humanized
  // independently of the schema, so a label-keyed map would silently break.
  var byKey = {};
  (item.settings || []).forEach(function(s) {
    var k = s.key ? String(s.key).split('.').pop() : s.label;
    byKey[k] = s.current_value;
  });
  if (catalog === 'modes' && byKey.tool) return 'tool=' + byKey.tool;
  if (catalog === 'model_endpoints' && byKey.endpoint) return 'endpoint=' + byKey.endpoint;
  return '';
}

// Build a <select> for a reference field. Honors nullability with a "— none —"
// option, and PRESERVES + FLAGS a stored value that is not in the catalog so a
// save of unrelated fields never silently discards it.
function cfgRefSelect(s, refSpec, base) {
  var cur = (s.current_value === null || s.current_value === undefined) ? '' : String(s.current_value);
  var opts = _cfgOptions[refSpec.catalog] || [];
  var html = '';
  if (refSpec.nullable) {
    html += '<option value=""' + (cur === '' ? ' selected' : '') + '>— none —</option>';
  }
  var known = false;
  opts.forEach(function(o) {
    if (cur === o.value) known = true;
    var label = o.hint ? (o.value + ' — ' + o.hint) : o.value;
    html += '<option value="' + esc(o.value) + '"' + (cur === o.value ? ' selected' : '') + '>'
      + esc(label) + '</option>';
  });
  if (cur !== '' && !known) {
    html = '<option value="' + esc(cur) + '" selected>' + esc(cur) + '  ⚠ not in catalog</option>' + html;
  }
  return '<select ' + base + ' data-ref="' + esc(refSpec.catalog) + '" style="' + CFG_INPUT + '">'
    + html + '</select>';
}

// Build one editable control for a setting. data-orig carries the JSON of the
// loaded value so change detection can skip unchanged fields (a masked, unchanged
// secret therefore never round-trips — the server treats the mask as a no-op too).
function cfgInput(s) {
  var orig = (s.current_value === undefined) ? null : s.current_value;
  // The payload/error identifier is the bare field name. Source it from the
  // stable dotted `key` (e.g. "global.poll_interval_seconds" → "poll_interval_seconds"),
  // not the display `label`, so a future label change can never break saves.
  var field = s.key ? String(s.key).split('.').pop() : s.label;
  var base = 'data-field="' + esc(field) + '" data-type="' + esc(s.type) + '"'
    + ' data-orig="' + esc(JSON.stringify(orig)) + '"';
  var refSpec = _CFG_REFS[field];
  if (refSpec) {
    return cfgRefSelect(s, refSpec, base);
  }
  if (s.type === 'bool') {
    return '<input type="checkbox" ' + base + (s.current_value ? ' checked' : '') + '>';
  }
  if (s.type === 'enum' && s.enum) {
    var opts = s.enum.map(function(o) {
      return '<option' + (String(s.current_value) === String(o) ? ' selected' : '') + '>' + esc(o) + '</option>';
    }).join('');
    return '<select ' + base + ' style="' + CFG_INPUT + '">' + opts + '</select>';
  }
  var scalar = (s.current_value === null || s.current_value === undefined) ? '' : String(s.current_value);
  if (s.type === 'int' || s.type === 'float') {
    var step = s.type === 'float' ? ' step="any"' : '';
    return '<input type="number"' + step + ' ' + base + ' value="' + esc(scalar) + '" style="' + CFG_INPUT + '">';
  }
  if (s.type === 'list') {
    var lv = Array.isArray(s.current_value) ? s.current_value.join(', ') : '';
    return '<input type="text" ' + base + ' value="' + esc(lv) + '" placeholder="comma, separated" style="' + CFG_INPUT + '">';
  }
  if (s.type === 'text') {
    return '<textarea ' + base + ' rows="2" style="' + CFG_INPUT + '">' + esc(scalar) + '</textarea>';
  }
  var ph = s.secret ? ' placeholder="leave masked to keep current"' : '';
  return '<input type="text" ' + base + ph + ' value="' + esc(scalar) + '" style="' + CFG_INPUT + '">';
}

// Coerce a control's value back to its typed form for the JSON payload.
function cfgCoerce(el) {
  var t = el.getAttribute('data-type');
  if (t === 'bool') return el.checked;
  var v = el.value;
  // A nullable reference left at "— none —" serializes as null (field omitted),
  // matching the prior free-text-empty-means-unset behavior.
  if (el.getAttribute('data-ref') && v === '') return null;
  if (t === 'int') return v.trim() === '' ? null : parseInt(v, 10);
  if (t === 'float') return v.trim() === '' ? null : parseFloat(v);
  if (t === 'list') return v.split(',').map(function(x) { return x.trim(); }).filter(function(x) { return x.length; });
  return v;
}

// Gather only the fields whose value differs from data-orig, scoped to a container.
function cfgCollectChanges(container) {
  var changes = {};
  container.querySelectorAll('[data-field]').forEach(function(el) {
    var coerced = cfgCoerce(el);
    if (JSON.stringify(coerced) !== el.getAttribute('data-orig')) {
      changes[el.getAttribute('data-field')] = coerced;
    }
  });
  return changes;
}

function cfgClearErrors(container) {
  container.querySelectorAll('[data-err]').forEach(function(slot) {
    slot.textContent = '';
    slot.style.display = 'none';
  });
}

function cfgApplyErrors(container, errs) {
  (errs || []).forEach(function(e) {
    if (!e.key) return;
    var f = container.querySelector('[data-field="' + e.key + '"]');
    var holder = f ? f.closest('[data-fieldrow]') : null;
    var slot = holder ? holder.querySelector('[data-err]') : null;
    if (slot) { slot.textContent = e.message; slot.style.display = ''; }
  });
}

// Optimistic-concurrency conflict: the file changed under us. Offer a reload so
// the operator picks up the latest baseline before re-applying their edit.
function cfgConflict(statusEl) {
  if (!statusEl) return;
  statusEl.innerHTML = '<span style="color:var(--color-accent-orange,#d29922)">'
    + 'Config changed on disk since you loaded it. </span>'
    + '<button type="button" class="cfg-btn" onclick="loadConfigPage()">Reload latest</button>';
}

// Map a save response onto field errors + a status badge. Returns true on success.
// `store` selects which optimistic-concurrency baseline the returned new_hash
// refreshes: 'routing_yaml' → _cfgRoutingHash, otherwise the config.yaml baseline.
function cfgHandleSave(status, body, container, statusEl, store) {
  cfgClearErrors(container);
  if (status === 200 && body.ok) {
    if (body.new_hash) {
      if (store === 'routing_yaml') _cfgRoutingHash = body.new_hash;
      else _cfgHash = body.new_hash;
    }
    if (body.applied === 'hot_reloaded') cfgFlash(statusEl, 'Saved · applied live', 'green');
    else if (body.applied === 'staged_restart') cfgFlash(statusEl, body.message || 'Saved · restart required to apply', 'orange');
    else if (body.applied === 'staged_next_job') cfgFlash(statusEl, 'Saved · applies on next job', 'blue');
    else cfgFlash(statusEl, 'Saved', 'green');
    setTimeout(loadConfigPage, 1000);
    return true;
  }
  if (status === 409) { cfgConflict(statusEl); return false; }
  var errs = body.errors || [];
  cfgApplyErrors(container, errs);
  var general = errs.filter(function(e) { return !e.key; }).map(function(e) { return e.message; }).join('; ');
  cfgFlash(statusEl, general || body.message || ('Error ' + status), 'red');
  return false;
}

async function cfgPut(url, payload, container, statusEl, btn) {
  if (btn) btn.disabled = true;
  try {
    var r = await fetch(url, {method: payload._method || 'PUT', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(payload.body)});
    var body = await r.json();
    cfgHandleSave(r.status, body, container, statusEl, payload.store);
  } catch(e) {
    cfgFlash(statusEl, 'Network error', 'red');
  }
  if (btn) btn.disabled = false;
}

function cfgSaveSection(btn, sectionId) {
  var container = btn.closest('[data-section]');
  var statusEl = container.querySelector('[data-status]');
  var changes = cfgCollectChanges(container);
  cfgClearErrors(container);
  if (!Object.keys(changes).length) { cfgFlash(statusEl, 'No changes to save', 'muted'); return; }
  cfgPut('/api/config/section/' + encodeURIComponent(sectionId),
    {body: {store: 'config_yaml', section: sectionId, changes: changes, base_hash: _cfgHash}},
    container, statusEl, btn);
}

function cfgSaveItem(btn, catalog, itemId) {
  var container = btn.closest('[data-item]');
  var statusEl = container.querySelector('[data-status]');
  var changes = cfgCollectChanges(container);
  cfgClearErrors(container);
  if (!Object.keys(changes).length) { cfgFlash(statusEl, 'No changes to save', 'muted'); return; }
  cfgPut('/api/config/catalog/' + encodeURIComponent(catalog) + '/' + encodeURIComponent(itemId),
    {body: {changes: changes, base_hash: _cfgHash}}, container, statusEl, btn);
}

function cfgDeleteItem(btn, catalog, itemId) {
  var container = btn.closest('[data-item]');
  var statusEl = container.querySelector('[data-status]');
  cfgPut('/api/config/catalog/' + encodeURIComponent(catalog) + '/' + encodeURIComponent(itemId),
    {_method: 'DELETE', body: {base_hash: _cfgHash}}, container, statusEl, btn);
}

function cfgToggleAdd(catalog) {
  var form = document.getElementById('cfg-add-' + catalog);
  if (form) form.style.display = (form.style.display === 'none' || !form.style.display) ? '' : 'none';
}

function cfgCreateItem(btn, catalog) {
  var container = btn.closest('[data-item]');
  var statusEl = container.querySelector('[data-status]');
  var item = {};
  container.querySelectorAll('[data-field]').forEach(function(el) {
    var v = cfgCoerce(el);
    if (v !== null && v !== '' && !(Array.isArray(v) && !v.length)) item[el.getAttribute('data-field')] = v;
  });
  cfgClearErrors(container);
  if (!item.name) { cfgFlash(statusEl, 'A name is required', 'red'); return; }
  cfgPut('/api/config/catalog/' + encodeURIComponent(catalog),
    {_method: 'POST', body: {item: item, base_hash: _cfgHash}}, container, statusEl, btn);
}

// spec 081-config-ui US3 (T041): routing-table CRUD wired to /api/config/routing
// (NOT the catalog endpoints). Routing entries carry a nested `target`; edits are
// staged for the next performer job (no live reload) — surfaced as a blue label.
function cfgROrig(value, type) {
  if (type === 'list') return JSON.stringify(Array.isArray(value) ? value : []);
  return JSON.stringify(value == null ? '' : String(value));
}
function cfgRText(rkey, value, type, ph) {
  var v = (value == null) ? '' : (Array.isArray(value) ? value.join(', ') : String(value));
  return '<input type="text" data-rkey="' + esc(rkey) + '" data-rtype="' + esc(type) + '"'
    + ' data-rorig="' + esc(cfgROrig(value, type)) + '" value="' + esc(v) + '"'
    + (ph ? ' placeholder="' + esc(ph) + '"' : '') + ' style="' + CFG_INPUT + '">';
}
function cfgRSelect(rkey, value, opts) {
  var o = opts.map(function(x) {
    return '<option' + (String(value) === String(x) ? ' selected' : '') + '>' + esc(x) + '</option>';
  }).join('');
  return '<select data-rkey="' + esc(rkey) + '" data-rtype="enum" data-rorig="' + esc(cfgROrig(value, 'enum'))
    + '" style="' + CFG_INPUT + '">' + o + '</select>';
}
function cfgRCoerce(el) {
  var v = el.value;
  if (el.getAttribute('data-rtype') === 'list')
    return v.split(',').map(function(x) { return x.trim(); }).filter(function(x) { return x.length; });
  return v;
}
function cfgRRow(label, control) {
  return '<div data-fieldrow style="padding:5px 0">'
    + '<label style="font-size:11px;color:var(--color-text-muted);display:block;margin-bottom:2px">' + esc(label) + '</label>'
    + control + '</div>';
}
function cfgRoutingFields(e) {
  var t = e.target || {};
  return cfgRRow('backend', cfgRText('backend', e.backend, 'string'))
    + cfgRRow('model', cfgRText('model', e.model, 'string'))
    + cfgRRow('target.base_url', cfgRText('target.base_url', t.base_url, 'string'))
    + cfgRRow('target.wire_format', cfgRSelect('target.wire_format', t.wire_format || 'openai', CFG_WIRE))
    + cfgRRow('target.strategy', cfgRSelect('target.strategy', t.strategy || 'normalize', CFG_STRATEGY))
    + cfgRRow('target.normalizers', cfgRText('target.normalizers', t.normalizers || [], 'list', 'harmony_tool_calls, strip_reasoning'))
    + cfgRRow('target.reroute_upstream', cfgRText('target.reroute_upstream', t.reroute_upstream, 'string', 'only for reroute strategy'));
}
function cfgRoutingEntryBlock(entry, index) {
  return '<div data-rentry style="background:var(--color-bg-base);border:1px solid var(--color-border);border-radius:6px;padding:10px;margin-bottom:10px">'
    + '<div style="font-weight:bold;color:var(--color-text-primary);font-size:13px;margin-bottom:4px">'
    + esc(entry.backend || '') + ' / ' + esc(entry.model || '') + '</div>'
    + cfgRoutingFields(entry)
    + '<div style="display:flex;gap:8px;align-items:center;margin-top:8px">'
    + '<button type="button" class="cfg-btn" onclick="cfgSaveRoutingEntry(this,' + index + ')">Save</button>'
    + '<button type="button" class="cfg-btn cfg-btn-danger" onclick="cfgDeleteRoutingEntry(this,' + index + ')">Delete</button>'
    + '<span data-status style="font-size:11px;margin-left:6px"></span></div></div>';
}
function cfgRoutingAddBlock() {
  return '<div id="cfg-add-routing" data-rentry style="display:none;background:var(--color-bg-base);'
    + 'border:1px dashed var(--color-accent-blue);border-radius:6px;padding:10px;margin-bottom:10px">'
    + '<div style="font-weight:bold;color:var(--color-accent-blue);font-size:13px;margin-bottom:4px">New routing entry</div>'
    + cfgRoutingFields({})
    + '<div style="display:flex;gap:8px;align-items:center;margin-top:8px">'
    + '<button type="button" class="cfg-btn" onclick="cfgCreateRoutingEntry(this)">Create</button>'
    + '<span data-status style="font-size:11px;margin-left:6px"></span></div></div>';
}
function cfgCollectRoutingChanges(container, all) {
  var changes = {}, target = {};
  container.querySelectorAll('[data-rkey]').forEach(function(el) {
    var v = cfgRCoerce(el);
    if (!all && JSON.stringify(v) === el.getAttribute('data-rorig')) return;
    if (all && (v === '' || (Array.isArray(v) && !v.length))) return;
    var key = el.getAttribute('data-rkey');
    if (key.indexOf('target.') === 0) target[key.slice(7)] = v;
    else changes[key] = v;
  });
  if (Object.keys(target).length) changes.target = target;
  return changes;
}
function cfgSaveRoutingEntry(btn, index) {
  var container = btn.closest('[data-rentry]');
  var statusEl = container.querySelector('[data-status]');
  var changes = cfgCollectRoutingChanges(container, false);
  if (!Object.keys(changes).length) { cfgFlash(statusEl, 'No changes to save', 'muted'); return; }
  cfgPut('/api/config/routing/entry/' + index,
    {store: 'routing_yaml', body: {changes: changes, base_hash: _cfgRoutingHash}}, container, statusEl, btn);
}
function cfgDeleteRoutingEntry(btn, index) {
  var container = btn.closest('[data-rentry]');
  var statusEl = container.querySelector('[data-status]');
  cfgPut('/api/config/routing/entry/' + index,
    {store: 'routing_yaml', _method: 'DELETE', body: {base_hash: _cfgRoutingHash}}, container, statusEl, btn);
}
function cfgCreateRoutingEntry(btn) {
  var container = btn.closest('[data-rentry]');
  var statusEl = container.querySelector('[data-status]');
  var entry = cfgCollectRoutingChanges(container, true);
  if (!entry.backend || !entry.model) { cfgFlash(statusEl, 'backend and model are required', 'red'); return; }
  cfgPut('/api/config/routing/entry',
    {store: 'routing_yaml', _method: 'POST', body: {entry: entry, base_hash: _cfgRoutingHash}}, container, statusEl, btn);
}
function cfgRoutingSection(sec, banner) {
  var body;
  if (_cfgRoutingAvail) {
    body = '<div style="font-size:11px;color:var(--color-accent-blue);margin-bottom:8px">'
      + 'Routing edits apply to the next performer job (not live).</div>'
      + '<div id="cfg-routing-entries"><span class="empty-state">Loading routing…</span></div>'
      + cfgRoutingAddBlock()
      + '<div style="margin-top:6px"><button type="button" class="cfg-btn" onclick="cfgToggleAdd(&quot;routing&quot;)">+ Add routing entry</button></div>';
  } else {
    body = '<div class="empty-state">Routing is read-only on this host.</div>';
  }
  var desc = sec.description ? '<div style="font-size:12px;color:var(--color-text-muted);margin-bottom:8px">' + esc(sec.description) + '</div>' : '';
  return '<section data-section="routing" aria-label="' + esc(sec.title)
    + '" style="background:var(--color-bg-surface);border:1px solid var(--color-border);border-radius:6px;padding:14px;margin-bottom:14px">'
    + '<h3 style="margin:0 0 8px 0;font-size:14px;color:var(--color-text-primary)">' + esc(sec.title) + '</h3>'
    + desc + banner + body + '</section>';
}
async function loadRoutingEntries() {
  var host = document.getElementById('cfg-routing-entries');
  if (!host) return;
  try {
    var r = await fetch('/api/config/routing');
    var d = await r.json();
    _cfgRoutingHash = d.content_hash || null;
    var entries = d.entries || [];
    host.innerHTML = entries.length
      ? entries.map(function(e, i) { return cfgRoutingEntryBlock(e, i); }).join('')
      : '<div class="empty-state">No routing entries yet.</div>';
  } catch(e) {
    host.innerHTML = '<div class="empty-state">Failed to load routing entries</div>';
  }
}

function cfgFieldRow(s) {
  var meta = [];
  meta.push(cfgBadge(s.type, 'var(--color-border)'));
  if (s.secret) meta.push(cfgBadge('secret', 'var(--color-accent-orange,#d29922)'));
  if (s.is_env_placeholder) meta.push(cfgBadge('env', 'var(--color-accent-blue)'));
  if (s.restart_required) meta.push(cfgBadge('restart required', 'var(--color-accent-orange,#d29922)'));
  if (!s.editable) meta.push(cfgBadge('read-only', 'var(--color-text-muted)'));
  if (s.invalid) meta.push(cfgBadge('invalid on disk', 'var(--color-accent-red)'));
  var control = s.editable ? cfgInput(s)
    : '<div style="font-family:var(--font-mono,monospace);font-size:12px;color:'
      + (s.invalid ? 'var(--color-accent-red)' : 'var(--color-text-primary)') + '">' + cfgFmtValue(s.current_value) + '</div>';
  var constraint = '';
  if (s.enum && s.enum.length) constraint = 'one of: ' + esc(s.enum.join(', '));
  else if (s.range) {
    // The descriptor layer emits either {min,max} (numbers) or
    // {min_length,max_length} (strings/collections). Handle both shapes, plus
    // partial bounds (only-min or only-max), so we never render `undefined`.
    var r = s.range;
    var hasLen = (r.min_length !== null && r.min_length !== undefined)
              || (r.max_length !== null && r.max_length !== undefined);
    var noun = hasLen ? 'length' : 'range';
    var lo = hasLen ? r.min_length : r.min;
    var hi = hasLen ? r.max_length : r.max;
    var hasLo = (lo !== null && lo !== undefined);
    var hasHi = (hi !== null && hi !== undefined);
    // gt/lt are exclusive — render > / < instead of ≥ / ≤ so the displayed range
    // matches server validation. Length bounds are always inclusive.
    var loSym = (!hasLen && r.min_exclusive) ? '> ' : '≥ ';
    var hiSym = (!hasLen && r.max_exclusive) ? '< ' : '≤ ';
    if (hasLo && hasHi) {
      if (!hasLen && (r.min_exclusive || r.max_exclusive))
        constraint = noun + ': ' + loSym + esc(String(lo)) + ' to ' + hiSym + esc(String(hi));
      else
        constraint = noun + ': ' + esc(String(lo)) + ' to ' + esc(String(hi));
    }
    else if (hasLo) constraint = noun + ': ' + loSym + esc(String(lo));
    else if (hasHi) constraint = noun + ': ' + hiSym + esc(String(hi));
  }
  var help = s.help ? '<div style="' + CFG_HINT + '">' + esc(s.help) + '</div>' : '';
  var ch = constraint ? '<div style="' + CFG_HINT + '">' + constraint + '</div>' : '';
  var def = (s.default !== null && s.default !== undefined)
    ? '<div style="' + CFG_HINT + '">default: ' + cfgFmtValue(s.default) + '</div>' : '';
  return '<div data-fieldrow style="padding:8px 0;border-bottom:1px solid var(--color-bg-elevated)">'
    + '<div style="display:flex;justify-content:space-between;align-items:baseline;gap:12px;flex-wrap:wrap">'
    + '<label style="font-size:12px;color:var(--color-text-primary);font-weight:600">' + esc(s.label || s.key) + '</label>'
    + '<div style="text-align:right">' + meta.join('') + '</div></div>'
    + '<div style="margin-top:3px">' + control + '</div>'
    + help + ch + def
    + '<div data-err style="display:none;font-size:11px;color:var(--color-accent-red);margin-top:3px"></div>'
    + '</div>';
}

// Keep the legacy name as an alias so existing callers/tests resolve.
function cfgSettingRow(s) { return cfgFieldRow(s); }

function cfgItemBlock(item, catalog, editable) {
  var ref = item.referenced_by && item.referenced_by.length
    ? cfgBadge('referenced by: ' + item.referenced_by.join(', '), 'var(--color-accent-blue)')
    : '';
  // A view-only section (editable=false, e.g. personas/symphonies) has no Save
  // action, so it must never offer an edit affordance — force every field
  // read-only, even individually-editable ones, before delegating to cfgFieldRow.
  // Use a shallow copy so the source setting object is never mutated.
  var rows = (item.settings || []).map(function(s) {
    return cfgFieldRow(editable ? s : Object.assign({}, s, {editable: false}));
  }).join('');
  var footer = '';
  if (editable) {
    var iid = esc(item.id);
    var cat = esc(catalog);
    var delTitle = item.deletable ? ''
      : ' title="Protected: remove the references first — ' + esc((item.referenced_by || []).join(', ')) + '"';
    var delBtn = '<button type="button" class="cfg-btn cfg-btn-danger"' + (item.deletable ? '' : ' disabled')
      + delTitle + ' onclick="cfgDeleteItem(this, &quot;' + cat + '&quot;, &quot;' + iid + '&quot;)">Delete</button>';
    footer = '<div style="display:flex;gap:8px;align-items:center;margin-top:8px">'
      + '<button type="button" class="cfg-btn" onclick="cfgSaveItem(this, &quot;' + cat + '&quot;, &quot;' + iid + '&quot;)">Save</button>'
      + delBtn
      + '<span data-status style="font-size:11px;margin-left:6px"></span></div>';
  } else if (!item.deletable) {
    footer = cfgBadge('protected', 'var(--color-text-muted)');
  }
  return '<div data-item style="background:var(--color-bg-base);border:1px solid var(--color-border);border-radius:6px;padding:10px;margin-bottom:10px">'
    + '<div style="font-weight:bold;color:var(--color-text-primary);font-size:13px;margin-bottom:4px">' + esc(item.id) + ref + '</div>'
    + (rows || '<div class="empty-state">No editable fields.</div>')
    + footer
    + '</div>';
}

// A blank create form for a catalog, seeded from the field shape of an existing
// item (or an empty name field when the catalog has no entries yet).
function cfgAddBlock(sec) {
  var template = (sec.items && sec.items.length) ? sec.items[0].settings || [] : [];
  var blanks = template.map(function(s) {
    var b = {key: s.key, label: s.label, help: s.help, type: s.type, enum: s.enum, range: s.range,
      secret: s.secret, editable: true, current_value: null};
    return cfgFieldRow(b);
  }).join('');
  if (!blanks) {
    blanks = cfgFieldRow({key: 'name', label: 'name', type: 'string', editable: true, current_value: null});
  }
  return '<div id="cfg-add-' + esc(sec.id) + '" data-item style="display:none;background:var(--color-bg-base);'
    + 'border:1px dashed var(--color-accent-blue);border-radius:6px;padding:10px;margin-bottom:10px">'
    + '<div style="font-weight:bold;color:var(--color-accent-blue);font-size:13px;margin-bottom:4px">New ' + esc(sec.id) + ' entry</div>'
    + blanks
    + '<div style="display:flex;gap:8px;align-items:center;margin-top:8px">'
    + '<button type="button" class="cfg-btn" onclick="cfgCreateItem(this, &quot;' + esc(sec.id) + '&quot;)">Create</button>'
    + '<span data-status style="font-size:11px;margin-left:6px"></span></div>'
    + '</div>';
}

function cfgSectionBlock(sec) {
  var editable = (sec.kind === 'scalar_group') || (sec.kind === 'collection' && CFG_CATALOGS.indexOf(sec.id) !== -1);
  var banner = sec.invalid_banner
    ? '<div role="status" style="background:var(--color-bg-base);border:1px solid var(--color-accent-orange,#d29922);'
      + 'border-radius:4px;padding:8px 10px;margin-bottom:10px;font-size:12px;color:var(--color-accent-orange,#d29922)">'
      + esc(sec.invalid_banner) + '</div>'
    : '';
  if (sec.id === 'routing') return cfgRoutingSection(sec, banner);
  var body, footer = '';
  if (sec.kind === 'scalar_group') {
    body = (sec.settings || []).map(cfgFieldRow).join('') || '<div class="empty-state">No settings.</div>';
    if (editable) {
      footer = '<div style="display:flex;gap:8px;align-items:center;margin-top:10px">'
        + '<button type="button" class="cfg-btn" onclick="cfgSaveSection(this, &quot;' + esc(sec.id) + '&quot;)">Save changes</button>'
        + '<span data-status style="font-size:11px;margin-left:6px"></span></div>';
    }
  } else {
    body = (sec.items && sec.items.length)
      ? sec.items.map(function(it) { return cfgItemBlock(it, sec.id, editable); }).join('')
      : '<div class="empty-state">No entries.</div>';
    if (editable) {
      body += cfgAddBlock(sec);
      footer = '<div style="margin-top:6px"><button type="button" class="cfg-btn" onclick="cfgToggleAdd(&quot;'
        + esc(sec.id) + '&quot;)">+ Add entry</button></div>';
    }
  }
  var desc = sec.description ? '<div style="font-size:12px;color:var(--color-text-muted);margin-bottom:8px">' + esc(sec.description) + '</div>' : '';
  return '<section data-section="' + esc(sec.id) + '" aria-label="' + esc(sec.title)
    + '" style="background:var(--color-bg-surface);border:1px solid var(--color-border);border-radius:6px;padding:14px;margin-bottom:14px">'
    + '<h3 style="margin:0 0 8px 0;font-size:14px;color:var(--color-text-primary)">' + esc(sec.title) + '</h3>'
    + desc + banner + body + footer
    + '</section>';
}

// Render the horizontal tab strip. Reuses .swimlane-tab styling. WAI-ARIA tabs
// pattern: roving tabindex, aria-selected/aria-controls on each tab.
function cfgTabBar(sections, active) {
  var tabs = (sections || []).map(function(sec) {
    var on = sec.id === active;
    return '<button type="button" role="tab" id="' + cfgTabId(sec.id) + '"'
      + ' aria-selected="' + (on ? 'true' : 'false') + '"'
      + ' aria-controls="' + cfgPanelId(sec.id) + '"'
      + ' tabindex="' + (on ? '0' : '-1') + '"'
      + ' class="swimlane-tab' + (on ? ' active' : '') + '"'
      + ' onclick="cfgSelectTab(&quot;' + esc(sec.id) + '&quot;)"'
      + ' onkeydown="cfgTabKey(event, &quot;' + esc(sec.id) + '&quot;)">'
      + esc(sec.title) + '</button>';
  }).join('');
  return '<div class="swimlane-tabs" role="tablist" aria-label="Configuration sections">'
    + tabs + '</div>';
}

// Switch the visible panel. pushState so the browser Back button moves between
// tabs (the existing popstate -> router() -> loadConfigPage() re-derives state).
function cfgSelectTab(secId) {
  _cfgTab = secId;
  document.querySelectorAll('#config-page-section [role="tab"]').forEach(function(t) {
    var on = t.id === cfgTabId(secId);
    t.classList.toggle('active', on);
    t.setAttribute('aria-selected', on ? 'true' : 'false');
    t.setAttribute('tabindex', on ? '0' : '-1');
  });
  document.querySelectorAll('#config-page-section [role="tabpanel"]').forEach(function(p) {
    p.hidden = (p.id !== cfgPanelId(secId));
  });
  if (location.hash !== '#' + secId) {
    history.pushState(null, '', '/config#' + secId);
  }
}

// Arrow/Home/End keyboard navigation across the tab strip (WAI-ARIA tabs).
function cfgTabKey(event, secId) {
  var key = event.key;
  if (key !== 'ArrowRight' && key !== 'ArrowLeft' && key !== 'Home' && key !== 'End') return;
  event.preventDefault();
  var tabs = Array.prototype.slice.call(
    document.querySelectorAll('#config-page-section [role="tab"]'));
  if (!tabs.length) return;
  var i = tabs.findIndex(function(t) { return t.id === cfgTabId(secId); });
  var next;
  if (key === 'Home') next = 0;
  else if (key === 'End') next = tabs.length - 1;
  else if (key === 'ArrowRight') next = (i + 1) % tabs.length;
  else next = (i - 1 + tabs.length) % tabs.length;
  var target = tabs[next];
  if (!target) return;
  var targetSec = target.id.replace('cfg-tab-', '');
  cfgSelectTab(targetSec);
  target.focus();
}

async function loadConfigPage() {
  var el = document.getElementById('config-page-section');
  el.innerHTML = '<span class="empty-state">Loading...</span>';
  var res, data;
  try {
    res = await fetch('/api/config/all');
    data = await res.json();
  } catch(e) {
    el.innerHTML = '<div class="empty-state">Failed to load configuration</div>';
    return;
  }
  if (!res.ok) {
    el.innerHTML = '<div class="empty-state">' + esc(data.error || 'Error loading configuration') + '</div>';
    return;
  }
  _cfgHash = (data.content_hashes || {}).config_yaml || null;
  _cfgVersion = data.config_version;
  _cfgRoutingAvail = !!data.routing_available;
  _cfgOptions = cfgBuildOptions(data.sections);
  var header = '<div style="font-size:11px;color:var(--color-text-muted);margin-bottom:12px">'
    + 'Config version ' + esc(String(data.config_version))
    + (data.routing_available ? ' · routing available' : ' · routing unavailable')
    + '</div>';
  var sections = data.sections || [];
  var hash = (location.hash || '').replace(/^#/, '');
  var active = sections.some(function(s) { return s.id === hash; })
    ? hash
    : (sections[0] ? sections[0].id : null);
  _cfgTab = active;
  var panels = sections.map(function(sec) {
    return '<div role="tabpanel" id="' + cfgPanelId(sec.id) + '"'
      + ' aria-labelledby="' + cfgTabId(sec.id) + '"'
      + (sec.id === active ? '' : ' hidden') + '>'
      + cfgSectionBlock(sec) + '</div>';
  }).join('');
  el.innerHTML = header + cfgTabBar(sections, active) + panels;
  if (_cfgRoutingAvail) loadRoutingEntries();
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
  } else if (path === '/config') {
    showPage('config-page');
    loadConfigPage();
  } else if (path === '/assistant') {
    showPage('assistant-page');
    if (typeof initAssistant === 'function') initAssistant();
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
// 354: queued-for-slot board-row presentation. A card whose role pool is
// saturated sits in phase 'dispatching' with no slot; it renders as
// "Queued for <stage>" with a real wait (from the persisted
// slot_queued_since stamp) instead of a misleading "Dispatching". Returns
// null when the card is not queued, so callers keep their ordinary label.
// Queue position is deliberately not shown: the slot manager keeps no
// ordering, so a per-card position would be invented.
function queuedPhaseLabel(sess) {
  if (!sess || sess.phase !== 'dispatching' || !sess.slot_queued_since) return null;
  return 'Queued for ' + (sess.performer_stage ? formatPhaseLabel(sess.performer_stage) : 'performer');
}

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
      var skip = (s.session_skip_reasons || {})[iid];
      // 354: a card waiting on a saturated pool is queued, not dispatching.
      var queuedLabel = queuedPhaseLabel(sess);
      var phaseLabel = skip && skip.reason === 'pipeline_capacity'
        ? 'Queued — issue limit reached' : (queuedLabel || formatPhaseLabel(sess.phase || ''));
      var stage = sess.performer_stage ? formatPhaseLabel(sess.performer_stage) : '';
      // Queued wait is measured from the queued stamp; an actively
      // dispatching card measures from when its dispatch began.
      var waitSource = queuedLabel ? sess.slot_queued_since : sess.agent_dispatch_at;
      var elapsed = waitSource ? (fmtAge(waitSource) || '') : '';
      var stale = sess.agent_dispatch_at && (Date.now() - new Date(sess.agent_dispatch_at).getTime()) > STALE_THRESHOLD_MS;
      var elapsedHtml = elapsed
        ? (queuedLabel
          ? '<span style="color:var(--color-accent-yellow)">' + esc(elapsed) + '</span>'
          : (stale ? '<span style="color:var(--color-degraded)">⚠ ' + esc(elapsed) + '</span>' : esc(elapsed)))
        : '';
      var costStr = sess.agent_dispatch_at
        ? '$' + (Number(sess.card_cost_estimate) || 0).toFixed(4)
        : '—';
      var parts = [phaseLabel];
      if (!queuedLabel && stage) parts.push('<code>' + stage + '</code>');
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
  // 348 (closes TODO(054)): logs come from the service holding this card's slot.
  var logs = Array.isArray(sess.performer_logs) ? sess.performer_logs.slice(-20) : [];
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
    var filterHint = s.ownership_hint ? esc(s.ownership_hint) : '';
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
        // 138 FR-032: quiet marker — observation only, never a phase change.
        // .af-kind carries no colours of its own, so .ev-quiet is not overridden
        // by .ap-pill (equal specificity, later in the sheet).
        (_afQuietCards[sess.card_id] ? '<span class="af-kind ev-quiet">QUIET</span>' : '') +
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
  // 348: the panels below describe ONE card -- name it.
  var telemetryFor = selectedSessions.length > 1
    ? '<div class="muted" style="margin-top:4px;font-size:12px">Live panels below show <strong>' + esc((selectedSessions[0] || {}).card_title || (selectedSessions[0] || {}).card_id || '—') + '</strong> (' + String(selectedSessions.length) + ' cards on this role).</div>'
    : '';
  // 348: telemetry is per-card, so read the session running this role rather
  // than top-level state (null in shared-pool mode). No fallback: a role with
  // no live session genuinely has no data.
  var sel = selectedSessions[0] || {};
  var sessionStats = sel.session_stats;
  var statsLine = sessionStats
    ? esc((sessionStats.title || '') + (sessionStats.title ? ' · ' : '') + sessionStats.files_changed + ' files · +' + sessionStats.lines_added + '/-' + sessionStats.lines_removed + ' lines')
    : 'No session stats available yet.';
  var metrics = sel.performer_metrics || {};
  var tokenTotal = derivePerformerTokenTotal(sel);
  var metricsLine = (metrics && metrics.pid != null)
    ? (
      'PID ' + String(metrics.pid) +
      ' · CPU ' + esc(metrics.cpu_percent != null ? metrics.cpu_percent.toFixed(1) + '%' : '—') +
      ' · Memory ' + esc(fmtBytes(metrics.memory_bytes)) +
      ' · Tokens ' + esc(tokenTotal != null ? tokenTotal.toLocaleString() : '—')
    )
    : (tokenTotal != null
        ? 'Tokens ' + esc(tokenTotal.toLocaleString()) + ' · no process metrics (containerised performer)'
        : 'No live metrics available yet.');
  var events = Array.isArray(sel.performer_events) ? sel.performer_events.slice(-12) : [];
  var eventsHtml = events.length
    ? '<ul class="detail-list">' + events.map(function(ev) {
      return '<li><strong>' + esc(ev.type || 'event') + ':</strong> ' + esc(ev.text || '') + '</li>';
    }).join('') + '</ul>'
    : '<div class="muted">No live events yet.</div>';
  var logs = Array.isArray(sel.performer_logs) ? sel.performer_logs.slice(-20) : [];
  var logsHtml = logs.length
    ? '<div class="detail-log">' + logs.map(function(line) { return '<div>' + esc(line) + '</div>'; }).join('') + '</div>'
    : '<div class="muted">No stderr logs yet.</div>';

  var backendLine = esc(sel.performer_backend || 'performer');
  if (sel.backend_ui_url) {
    try {
      var parsed = new URL(sel.backend_ui_url);
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
        var label = r.reason === 'pipeline_capacity' ? 'Queued — issue limit reached' : esc(r.reason || 'skipped');
        var blockers = Array.isArray(r.blockers) && r.blockers.length
          ? ' (blocked by #' + r.blockers.map(function(n) { return esc(String(n)); }).join(', #') + ')' : '';
        return '<li><span style="color:var(--color-accent-orange)">' + esc(sr.card) + '</span> — ' + label + blockers + '</li>';
      }).join('') + '</ul>'
    : '';

  detailEl.innerHTML =
    '<div><strong>' + humanPhase(selectedRole) + '</strong>' + statusBadge + '</div>' +
    '<div class="muted" style="margin-top:4px">Active / Idle / Max: ' + String(row.active != null ? row.active : 0) + ' / ' + String(Math.max(0, (row.max != null ? row.max : 0) - (row.active != null ? row.active : 0))) + ' / ' + String(row.max != null ? row.max : 0) + '</div>' +
    '<div class="detail-block"><strong>Card Context</strong>' + roleCardsHtml + telemetryFor + stepTrailHtml(sel, s.stall_timeout_seconds) + (skipHtml ? '<div style="margin-top:8px"><span style="color:var(--color-accent-yellow);font-size:12px">Skipped this cycle:</span>' + skipHtml + '</div>' : '') + '</div>' +
    '<div class="detail-block"><strong>Session</strong><div class="muted" style="margin-top:4px">Session: ' + esc(sel.session_id || s.agent_session_id || '—') + ' &middot; Uptime: ' + esc(fmtAge(sel.agent_dispatch_at || s.agent_dispatch_at) || '—') + '</div><div class="muted" style="margin-top:4px">' + backendLine + '</div><div class="muted" style="margin-top:4px">' + statsLine + '</div></div>' +
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
  fetch('/api/personas').then(function(r){ _personaCfgHash = r.headers.get('ETag'); return r.json(); }).then(function(data) {
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
          var res = await fetch('/api/personas/' + encodeURIComponent(role), { method: 'PUT', headers: {'Content-Type':'application/json', 'If-Match': _personaCfgHash || ''}, body: JSON.stringify({instructions: ta.value}) });
          if (res.ok) _personaCfgHash = res.headers.get('ETag') || _personaCfgHash;
          // The body has to be read: versionErrorText keys on `conflict` to tell a
          // version clash from a duplicate name, and passing null made every 409
          // read 'Error 409'.
          var pd = res.ok ? null : await res.json().catch(function(){ return {}; });
          msg.textContent = res.ok ? 'Saved' : versionErrorText(res.status, pd);
          setTimeout(function(){ msg.textContent=''; }, 3000);
        } catch(e) { msg.textContent = 'Error'; }
      });
      el.querySelector('.persona-reset-btn').addEventListener('click', async function() {
        if (!confirm('Reset ' + role + ' instructions to default?')) return;
        try {
          var res = await fetch('/api/personas/' + encodeURIComponent(role), { method: 'DELETE', headers: {'If-Match': _personaCfgHash || ''} });
          if (res.ok) { loadPersonasPage(true); }
          else if (msg) {
            var rd = await res.json().catch(function(){ return {}; });
            msg.textContent = versionErrorText(res.status, rd);
          }
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
    _personaCfgHash = res.headers.get('ETag');
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
      headers: {'Content-Type': 'application/json', 'If-Match': _personaCfgHash || ''},
      body: JSON.stringify({instructions: ta.value}),
    });
    if (res.ok) _personaCfgHash = res.headers.get('ETag') || _personaCfgHash;
    var data = res.headers.get('content-type') && res.headers.get('content-type').includes('application/json')
      ? await res.json() : {};
    if (!res.ok) {
      msg.textContent = versionErrorText(res.status, data);
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
    var res = await fetch('/api/personas/' + encodeURIComponent(role), {
      method: 'DELETE',
      headers: {'If-Match': _personaCfgHash || ''},
    });
    if (res.status === 204 || res.ok) {
      _personaCfgHash = res.headers.get('ETag') || _personaCfgHash;
      msg.textContent = 'Reset to defaults.';
      msg.style.color = 'var(--color-accent-green)';
      setTimeout(function() { if (msg) { msg.textContent = ''; msg.style.color = 'var(--color-text-muted)'; }}, 3000);
      loadPersonas();
    } else {
      var data = await res.json().catch(function() { return {}; });
      msg.textContent = versionErrorText(res.status, data);
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

// ---------------------------------------------------------------------------
// 138: activity feed
// ---------------------------------------------------------------------------
var AF_MAX_ROWS = 2000;
var AF_SILENCE_MS = 40000;   // ~2.5 keepalive intervals (FR-026)
var AF_LABELS = {
  progress: 'PROGRESS', tool_use: 'TOOL', thinking: 'THINKING', cost: 'COST',
  stage_change: 'STAGE', recovered: 'RECOVERED', quiet: 'QUIET',
  stall: 'STALL', stuck: 'STUCK', error: 'ERROR', blocked: 'BLOCKED', completed: 'COMPLETED',
  // 327 added this activity type but only to AF_SUMMARIES, so the chip fell
  // back to the raw uppercased type while every other type had a short label.
  stream_truncated: 'TRUNCATED',
  // 343: step boundaries are the spine of the trail; give them their own chip.
  workflow_step: 'STEP'
};
var _afGroupingActive = false; // older SSE payloads lack session attribution
var _afEntries = [];        // retained entries, oldest-first
var _afSeen = {};           // seq -> true; reconnect backfill must not double-insert
var _afFilter = '';
var _afLastMsgAt = Date.now();
var _afLive = true;
var _afQuietCards = {};     // card_id -> true while that card is in a quiet episode

function afLabel(t) { return AF_LABELS[t] || String(t || '').toUpperCase() || 'EVENT'; }

function afTime(iso) {
  if (!iso) return '';
  try { return new Date(iso).toLocaleTimeString(); } catch(e) { return ''; }
}


function afMatches(e) { return !_afFilter || e.card_id === _afFilter; }

function afAppend(entries) {
  var feed = document.getElementById('activity-feed');
  if (!feed || !entries || !entries.length) return;
  var html = '';
  var added = 0;
  for (var i = 0; i < entries.length; i++) {
    var e = entries[i];
    if (e == null || _afSeen[e.seq]) continue;
    if (e.session_id) _afGroupingActive = true;
    _afSeen[e.seq] = true;
    var prev = _afEntries[_afEntries.length - 1];
    e._afGroupKey = afCanGroup(prev, e) ? prev._afGroupKey : e.seq;
    _afEntries.push(e);
    // FR-032: any real entry ends the card's quiet episode.
    if (e.card_id && e.activity_type !== 'quiet') delete _afQuietCards[e.card_id];
    if (!afMatches(e)) continue;
    html = afRowHtml(e) + html;   // batch is oldest-first; newest ends up on top
    added++;
  }
  while (_afEntries.length > AF_MAX_ROWS) delete _afSeen[_afEntries.shift().seq];
  if (_afGroupingActive) {
    afSyncGroups(false);
    afUpdateEmpty();
    afUpdateFilterOptions();
    return;
  }
  if (added) {
    // Prepend ONLY the new rows. A full re-render would make
    // aria-relevant="additions" re-announce every existing row.
    feed.insertAdjacentHTML('afterbegin', html);
    while (_afEntries.length > AF_MAX_ROWS) delete _afSeen[_afEntries.shift().seq];
    while (feed.childElementCount > AF_MAX_ROWS) feed.removeChild(feed.lastElementChild);
  }
  afUpdateEmpty();
  afUpdateFilterOptions();
}

function afRender() {
  // Full re-render — filter changes only, never a live update.
  var feed = document.getElementById('activity-feed');
  if (!feed) return;
  if (_afGroupingActive) {
    afSyncGroups(true);
    afUpdateEmpty();
    return;
  }
  var html = '';
  for (var i = 0; i < _afEntries.length; i++) {
    if (afMatches(_afEntries[i])) html = afRowHtml(_afEntries[i]) + html;
  }
  feed.innerHTML = html;
  afUpdateEmpty();
}

function afUpdateEmpty() {
  var feed = document.getElementById('activity-feed');
  var empty = document.getElementById('af-empty');
  if (!feed || !empty) return;
  empty.style.display = feed.childElementCount ? 'none' : '';
}

function afUpdateFilterOptions() {
  var sel = document.getElementById('af-filter');
  if (!sel) return;
  var seen = {}, cards = [];
  for (var i = 0; i < _afEntries.length; i++) {
    var e = _afEntries[i];
    if (!e.card_id || seen[e.card_id]) continue;
    seen[e.card_id] = true;
    cards.push(e);
  }
  if (_afFilter && !seen[_afFilter]) cards.push({card_id:_afFilter, card_title:_afFilter + ' (no retained activity)'});
  var key = cards.map(function(e) { return e.card_id; }).join(',');
  if (key === sel.getAttribute('data-cards')) return;
  sel.setAttribute('data-cards', key);
  var html = '<option value="">All cards</option>';
  cards.forEach(function(e) {
    var label = (e.card_number ? '#' + e.card_number + ' ' : '') + (e.card_title || e.card_id);
    html += '<option value="' + esc(e.card_id) + '">' + esc(label) + '</option>';
  });
  sel.innerHTML = html;
  sel.value = _afFilter;
  if (sel.value !== _afFilter) { _afFilter = ''; sel.value = ''; afRender(); }
}

function onActivityFilterChange() {
  var sel = document.getElementById('af-filter');
  _afFilter = sel ? sel.value : '';
  afRender();
}

function afSetLive(live) {
  if (live === _afLive) return;
  _afLive = live;
  var el = document.getElementById('af-liveness');
  if (el) {
    el.textContent = live ? 'Live' : 'Not live';
    if (live) el.classList.remove('af-not-live'); else el.classList.add('af-not-live');
  }
  // Retained entries stay visible and are marked possibly out of date; the
  // feed is never cleared on disconnect (FR-027).
  var note = document.getElementById('af-stale-note');
  if (note) note.style.display = live ? 'none' : '';
  // Reuse the existing connection indicators rather than adding a second one.
  var dot = document.getElementById('nav-sse-dot');
  if (dot) {
    if (live) { dot.classList.remove('disconnected'); dot.title = 'SSE connected'; }
    else { dot.classList.add('disconnected'); dot.title = 'SSE silent — no messages'; }
  }
  var bnr = document.getElementById('disconnected-banner');
  if (bnr && !live) bnr.style.display = 'block';
}

function afNoteMessage() {
  _afLastMsgAt = Date.now();
  afSetLive(true);
}

function afQuietTick() {
  var s = _lastState;
  if (!s) return;
  var threshold = s.activity_quiet_threshold_seconds;
  if (!threshold || threshold <= 0) return;   // 0 disables
  var sessions = Array.isArray(s.active_sessions) ? s.active_sessions : [];
  var now = Date.now();
  var newest = {};
  for (var i = 0; i < _afEntries.length; i++) {
    var e = _afEntries[i];
    if (!e.card_id || e.activity_type === 'quiet') continue;
    var t = Date.parse(e.timestamp);
    if (!isNaN(t) && (!newest[e.card_id] || t > newest[e.card_id])) newest[e.card_id] = t;
  }
  var marked = false;
  sessions.forEach(function(sess) {
    var cid = sess.card_id;
    if (!cid || _afQuietCards[cid]) return;
    // Anchor on max(newest entry, agent_dispatch_at), skipping whichever is
    // absent. Entry-only anchoring leaves a session restored after a daemon
    // restart unmarked forever — the exact card this exists for (FR-028).
    var lastHeard = newest[cid] || NaN;
    var dispatched = sess.agent_dispatch_at ? Date.parse(sess.agent_dispatch_at) : NaN;
    if (!isNaN(dispatched)) lastHeard = isNaN(lastHeard) ? dispatched : Math.max(lastHeard, dispatched);
    if (isNaN(lastHeard)) return;   // never mark quiet off a missing timestamp
    if ((now - lastHeard) / 1000 <= threshold) return;
    _afQuietCards[cid] = true;
    marked = true;
    // One synthetic row per episode. Synthetic rows never enter the stored log
    // on the daemon and never count as progress (FR-031, FR-033).
    afAppend([{
      seq: 'q:' + cid + ':' + lastHeard,
      timestamp: new Date().toISOString(),
      card_id: cid,
      card_number: sess.issue_number,
      card_title: sess.card_title,
      stage: sess.performer_stage || '',
      activity_type: 'quiet',
      text: 'No activity for ' + Math.round((now - lastHeard) / 1000) + 's',
      truncated: false
    }]);
  });
  if (marked) renderActivePerformers(s);
}

function afTick() {
  afSetLive(Date.now() - _afLastMsgAt < AF_SILENCE_MS);
  afQuietTick();
}

var banner = document.getElementById('disconnected-banner');
var es = new EventSource('/events');
es.addEventListener('activity_event', function(e) {
  afNoteMessage();
  try { afAppend(JSON.parse(e.data).entries); }
  catch(err) { console.error('activity parse error', err); }
});
es.addEventListener('heartbeat', function() { afNoteMessage(); });
es.addEventListener('state_update', function(e) {
  afNoteMessage();
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
  if (typeof assistantAvailable === 'function') assistantAvailable();
  setInterval(afTick, 5000);   // 138: silence timer + quiet detection
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



_ACTIVITY_STREAM_JS = """var AF_SUMMARIES = {
  progress: 'Performer reported progress.', thinking: 'Performer is reasoning.',
  tool_use: 'Performer used a tool.', cost: 'Usage updated.',
  stage_change: 'Workflow stage changed.', recovered: 'Work recovered.',
  quiet: 'No recent activity.', stall: 'Performer stalled.', stuck: 'Work is stuck.',
  error: 'An error was reported.', blocked: 'Work is blocked.', completed: 'Work completed.',
  stream_truncated: 'Output was truncated (stream too long).',
  workflow_step: 'Workflow step started.'
};
function afSummary(e) { return AF_SUMMARIES[e.activity_type] || 'Activity reported.'; }
// 353: collapsed group lines lead with the newest entry's own text instead of
// the generic sentence, so the feed answers "which tool? doing what?" without
// expanding the group. The ingest path already folds the backend's detail
// field into text and redacts it; the preview renders only what the group
// already carries, so there is no new leak path. Flattened to one line and
// capped here because the row is a single summary span.
var AF_PREVIEW_MAX = 120;
function afPreview(entries) {
  for (var i = entries.length - 1; i >= 0; i--) {
    var e = entries[i];
    if (e.activity_type === 'cost' || e.activity_type === 'stream_truncated') continue;
    var text = String(e.text || '').replace(/\\s+/g, ' ').trim();
    if (text) return text.length > AF_PREVIEW_MAX
      ? text.slice(0, AF_PREVIEW_MAX - 1) + '\u2026' : text;
  }
  return '';
}
function afRawHtml(entries) {
  var blocks = [];
  var usage = [];
  entries.forEach(function(e) {
    if (e.activity_type === 'cost') { usage.push(e); return; }
    var previous = blocks[blocks.length - 1];
    var fragment = (e.text || '') + (e.truncated ? '\u2026 [truncated]' : '');
    if (previous && e.is_delta && previous.delta &&
        previous.kind === e.activity_type && previous.stream === (e.stream_id || '')) {
      previous.text += fragment;
    } else {
      blocks.push({text:fragment, kind:e.activity_type,
                   delta:!!e.is_delta, stream:e.stream_id || ''});
    }
  });
  var output = blocks.map(function(b) {
    return b.kind === 'tool_use' ? '<pre class="af-tool"><code>' + esc(b.text) + '</code></pre>' :
      '<p>' + esc(b.text) + '</p>';
  }).join('');
  if (usage.length) {
    // Snapshots may be cumulative or incremental depending on the backend.
    // Preserve the reported values; never infer a total by adding them here.
    output += '<details class="af-usage"><summary>Usage reports (' + usage.length +
      ')</summary>' + usage.map(function(e) {
        return '<p>' + esc((e.text || '') + (e.truncated ? '\u2026 [truncated]' : '')) + '</p>';
      }).join('') + '</details>';
  }
  return output;
}
function afRepresentative(entries) {
  for (var i = entries.length - 1; i >= 0; i--) {
    if (entries[i].activity_type !== 'cost' &&
        entries[i].activity_type !== 'stream_truncated') return entries[i];
  }
  return entries[entries.length - 1];
}
function afCanGroup(a, b) {
  var stream = {progress:true, thinking:true, tool_use:true, cost:true};
  return !!(a && b && a.session_id && a.performer_id &&
    stream[a.activity_type] && stream[b.activity_type] &&
    a.card_id === b.card_id && a.stage === b.stage &&
    a.session_id === b.session_id && a.performer_id === b.performer_id);
}
// 353: the anchor skips folded truncation markers, so a stream that keeps
// flowing after its own truncation notice rejoins the group it belongs to.
function afGroupAnchor(group) {
  for (var i = group.entries.length - 1; i >= 0; i--) {
    if (group.entries[i].activity_type !== 'stream_truncated') {
      return group.entries[i];
    }
  }
  return null;
}
// 353: a truncation marker annotates the stream it truncated — fold it into
// that group as a badge rather than spending a top-level line on bookkeeping.
// An orphan marker (no matching group before it) keeps the legacy standalone
// line, which is the only sensible render when nothing precedes it.
function afCanFold(group, e) {
  var a = group ? afGroupAnchor(group) : null;
  return !!(a && a.session_id && a.card_id === e.card_id &&
    a.stage === e.stage && a.session_id === e.session_id &&
    a.performer_id === e.performer_id);
}
function afGroups() {
  var groups = [];
  _afEntries.forEach(function(e) {
    var g = groups.length ? groups[groups.length - 1] : null;
    if (e.activity_type === 'stream_truncated' && afCanFold(g, e)) {
      g.entries.push(e);
      g.truncatedCount = (g.truncatedCount || 0) + 1;
      return;
    }
    var anchor = g ? afGroupAnchor(g) : null;
    if (g && anchor && afCanGroup(anchor, e)) g.entries.push(e);
    else groups.push({key: e._afGroupKey == null ? e.seq : e._afGroupKey, entries:[e]});
  });
  return groups;
}
function afRowHtml(e, group) {
  var entries = group ? group.entries : [e];
  var latest = entries[entries.length - 1];
  e = afRepresentative(entries);
  var who = e.card_number ? ('#' + e.card_number) : (e.card_title || e.card_id || '');
  var where = who + (e.stage ? ' ' + e.stage : '');
  var count = entries.length;
  var key = group ? group.key : e.seq;
  var preview = afPreview(entries) || afSummary(e);
  var badge = group && group.truncatedCount
    ? '<span class="af-truncated">truncated</span>' : '';
  return '<div class="af-row">' +
    '<details id="af-group-' + esc(String(key)) + '">' +
    '<summary><span class="af-time">' + esc(afTime(latest.timestamp)) + '</span> ' +
    '<span class="af-kind ev-' + esc(e.activity_type) + '">' + esc(afLabel(e.activity_type)) + '</span>' +
    '<span class="af-card">' + esc(where) + '</span> ' +
    '<span class="af-summary">' + esc(preview) + ' (' + count +
    (count === 1 ? ' update)' : ' updates)') + '</span>' + badge + '</summary>' +
    '<div class="af-raw-label">Raw output (may contain other languages; retained text only)</div>' +
    '<div class="af-raw">' + afRawHtml(entries) + '</div>' +
    '</details></div>';
}
function afSyncGroups(reset) {
  var feed = document.getElementById('activity-feed');
  if (!feed) return;
  if (reset) feed.innerHTML = '';
  var wanted = {}, html = '';
  afGroups().forEach(function(g) {
    var latest = g.entries[g.entries.length - 1];
    var e = afRepresentative(g.entries);
    if (!afMatches(e)) return;
    var id = 'af-group-' + g.key;
    wanted[id] = true;
    var node = document.getElementById(id);
    if (!node) { html = afRowHtml(e, g) + html; return; }
    // Preserve the details element and its summary: open state and keyboard
    // focus survive streaming updates. Only text/diagnostic children change.
    node.querySelector('.af-time').textContent = afTime(latest.timestamp);
    var chip = node.querySelector('.af-kind');
    chip.className = 'af-kind ev-' + e.activity_type;
    chip.textContent = afLabel(e.activity_type);
    // 353: the collapsed line carries the newest entry's text; the truncation
    // badge is inserted or removed to match the folded marker count.
    var summary = afPreview(g.entries) || afSummary(e);
    var summaryEl = node.querySelector('.af-summary');
    summaryEl.textContent = summary + ' (' +
      g.entries.length + (g.entries.length === 1 ? ' update)' : ' updates)');
    var badge = node.querySelector('.af-truncated');
    if (g.truncatedCount && !badge) {
      var mark = document.createElement('span');
      mark.className = 'af-truncated';
      mark.textContent = 'truncated';
      summaryEl.parentNode.insertBefore(mark, summaryEl.nextSibling);
    } else if (!g.truncatedCount && badge) {
      badge.remove();
    }
    var raw = node.querySelector('.af-raw');
    var signature = g.entries[0].seq + ':' + latest.seq;
    if (raw.getAttribute('data-events') !== signature) {
      var usage = raw.querySelector('.af-usage');
      var usageOpen = usage && usage.open;
      var usageFocused = usage && document.activeElement === usage.querySelector('summary');
      raw.innerHTML = afRawHtml(g.entries);
      usage = raw.querySelector('.af-usage');
      if (usage) {
        usage.open = !!usageOpen;
        if (usageFocused) usage.querySelector('summary').focus();
      }
      raw.setAttribute('data-events', signature);
    }
  });
  if (html) feed.insertAdjacentHTML('afterbegin', html);
  // Raw retention is authoritative, even when all incoming events are filtered.
  feed.querySelectorAll('details[id^="af-group-"]').forEach(function(node) {
    if (!wanted[node.id]) node.parentElement.remove();
  });
}
"""


#: 155 (#202): the assistant's page and behaviour, kept OUT of ``_DASHBOARD_HTML``
#: and spliced in only when the feature is enabled. Two reasons, and the second is
#: the better one: the shared page has a size budget that a whole extra page would
#: blow, and a deployment with the assistant off should serve the byte-identical
#: page it served before this feature existed -- "off" meaning absent rather than
#: merely inert.
_ASSISTANT_FRAGMENT = """<!-- 155 (#202): the config assistant. Proposes; the operator applies. -->
<div id="assistant-page" style="display:none">
  <div class="card">
    <h2>Config Assistant</h2>
    <p class="empty-state" id="assistant-note">
      Ask for help configuring coordinare. The assistant proposes changes &mdash; you apply them.
    </p>
    <p class="empty-state">
      Your stored secrets are masked before coordinare sends anything. What you type here is not
      &mdash; it goes to the model endpoint as written, so do not paste tokens or keys.
    </p>
    <div id="assistant-log" aria-live="polite" style="max-height:52vh;overflow-y:auto;margin-bottom:12px"></div>
    <div style="display:flex;gap:8px">
      <input id="assistant-input" type="text" style="flex:1" placeholder="e.g. how many cards should run at once?"
             onkeydown="if(event.key==='Enter'){assistantSend();}">
      <button id="assistant-send" onclick="assistantSend()">Send</button>
    </div>
  </div>
</div>

<script>
// 155 (#202): config assistant. The conversation lives here and nowhere else —
// the server keeps nothing between requests, so session-scoped is structural.
var _asstHistory = [];
var _asstBusy = false;
var _asstCurrent = {};   // current editable global values, for the "before" side of a diff
var _asstBaseHash = null; // the config hash the model's context was built from

function assistantAvailable() {
  // Nav link appears only when enabled; disabled shows no trace of the feature.
  fetch('/api/assistant/status').then(function(r) {
    if (!r.ok) return null;
    return r.json();
  }).then(function(d) {
    if (!d || !d.enabled) return;
    var link = document.getElementById('nav-assistant');
    if (link) link.style.display = '';
  }).catch(function() { /* route absent = disabled */ });
}

function initAssistant() {
  fetch('/api/assistant/status').then(function(r) { return r.json(); }).then(function(d) {
    var note = document.getElementById('assistant-note');
    if (note && d && !d.ready) {
      note.textContent = 'The assistant is enabled but not ready: ' + (d.reason || 'unknown');
    } else if (d && d.opening) {
      var log = document.getElementById('assistant-log');
      if (log && !log.childNodes.length) assistantAppend('assistant', d.opening);
    }
  }).catch(function() {});
  assistantRefreshBaseline();
}

function assistantAppend(role, text) {
  var log = document.getElementById('assistant-log');
  if (!log) return null;
  var row = document.createElement('div');
  row.className = 'assistant-row assistant-' + role;
  row.style.margin = '8px 0';
  var who = document.createElement('strong');
  who.textContent = (role === 'operator' ? 'You' : 'Assistant') + ': ';
  row.appendChild(who);
  var body = document.createElement('span');
  body.textContent = text;
  row.appendChild(body);
  log.appendChild(row);
  log.scrollTop = log.scrollHeight;
  return row;
}

function assistantRefreshBaseline() {
  // Returns a promise, and the turn waits on it. Fire-and-forget meant a proposal
  // could be rendered before the hash it belongs to was known, and then applied
  // with whatever happened to be in the variable — or with nothing.
  var a = fetch('/api/config/all').then(function(r) { return r.json(); }).then(function(d) {
    _asstBaseHash = (d && d.content_hashes) ? d.content_hashes.config_yaml : null;
  }).catch(function() { _asstBaseHash = null; });
  var b = fetch('/api/config/global').then(function(r) { return r.json(); }).then(function(d) {
    _asstCurrent = d || {};
  }).catch(function() { _asstCurrent = {}; });
  return Promise.all([a, b]);
}

function assistantRenderProposal(proposal) {
  // A diff, so applying is a decision made by reading rather than by trusting.
  var log = document.getElementById('assistant-log');
  if (!log || !proposal) return;
  var card = document.createElement('div');
  card.className = 'card';
  card.style.margin = '8px 0';

  var head = document.createElement('div');
  head.innerHTML = '<strong>Proposed change to ' + esc(proposal.section) + '</strong>';
  card.appendChild(head);

  if (proposal.reason) {
    var why = document.createElement('div');
    why.className = 'empty-state';
    why.textContent = proposal.reason;
    card.appendChild(why);
  }

  var table = document.createElement('table');
  table.style.margin = '8px 0';
  Object.keys(proposal.values).forEach(function(k) {
    var tr = document.createElement('tr');
    var before = (_asstCurrent && k in _asstCurrent) ? _asstCurrent[k] : '(unset)';
    tr.innerHTML = '<td style="padding-right:12px"><code>' + esc(k) + '</code></td>' +
                   '<td style="padding-right:12px">' + esc(String(before)) + '</td>' +
                   '<td>&rarr; <strong>' + esc(String(proposal.values[k])) + '</strong></td>';
    table.appendChild(tr);
  });
  card.appendChild(table);

  var apply = document.createElement('button');
  apply.textContent = 'Apply';
  // Captured now, not at click time: this is the config the model actually saw.
  var baseHash = _asstBaseHash;
  apply.onclick = function() { assistantApply(proposal, baseHash, card, apply); };
  card.appendChild(apply);

  log.appendChild(card);
  log.scrollTop = log.scrollHeight;
}

function assistantApply(proposal, baseHash, card, button) {
  // Sends the hash this was built against; a concurrent edit refuses the write.
  if (!baseHash) {
    // Fail closed. The baseline fetch is asynchronous and can fail or still be in
    // flight, and without it this would write with no guard at all — silently, and
    // exactly when the dashboard is already having trouble talking to the server.
    // "Applying always goes through the guard" has to be true or it is not a claim.
    var blocked = document.createElement('div');
    blocked.textContent = 'Not applied: could not read the current configuration '
                        + 'version, so this cannot be applied safely. Reload and ask again.';
    card.appendChild(blocked);
    assistantRefreshBaseline();
    return;
  }
  button.disabled = true;
  button.textContent = 'Applying...';
  var body = {};
  Object.keys(proposal.values).forEach(function(k) { body[k] = proposal.values[k]; });
  body.expected_hash = baseHash;

  fetch('/api/config/global', {
    method: 'PUT',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body)
  }).then(function(r) {
    return r.json().then(function(d) { return {ok: r.ok, status: r.status, body: d}; });
  }).then(function(res) {
    var note = document.createElement('div');
    if (res.ok) {
      note.textContent = 'Applied.';
      button.textContent = 'Applied';
      assistantRefreshBaseline();  // everything proposed before this is now stale
    } else if (res.status === 409) {
      note.textContent = 'Not applied: the configuration changed since this was proposed. '
                       + 'Reload the Config page and ask again.';
      button.disabled = false;
      button.textContent = 'Apply';
    } else {
      note.textContent = 'Not applied: ' + ((res.body && res.body.error) || 'unknown error');
      button.disabled = false;
      button.textContent = 'Apply';
    }
    card.appendChild(note);
  }).catch(function(e) {
    button.disabled = false;
    button.textContent = 'Apply';
    var note = document.createElement('div');
    note.textContent = 'Not applied: ' + e;
    card.appendChild(note);
  });
}

function assistantSend() {
  if (_asstBusy) return;
  var input = document.getElementById('assistant-input');
  if (!input) return;
  var message = (input.value || '').trim();
  if (!message) return;
  input.value = '';
  _asstBusy = true;
  document.getElementById('assistant-send').disabled = true;

  assistantAppend('operator', message);
  var pending = assistantAppend('assistant', 'thinking...');

  // The applied hash must be the one the model's context was built from, not the
  // newest at click time — that would defeat the guard by always agreeing. Also
  // means arriving here without opening Config still gets a guarded write.
  assistantRefreshBaseline().then(function() {
  return fetch('/api/assistant/message', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({message: message, history: _asstHistory})
  }).then(function(r) { return r.json(); }).then(function(d) {
    if (pending && pending.parentNode) pending.parentNode.removeChild(pending);
    if (d.reply) assistantAppend('assistant', d.reply);
    if (d.error) assistantAppend('assistant', '(' + d.error + ')');
    if (d.proposal) assistantRenderProposal(d.proposal);
    _asstHistory.push({role: 'operator', text: message});
    if (d.reply) _asstHistory.push({role: 'assistant', text: d.reply});
    if (_asstHistory.length > 20) _asstHistory = _asstHistory.slice(-20);
  }).catch(function(e) {
    if (pending && pending.parentNode) pending.parentNode.removeChild(pending);
    assistantAppend('assistant', '(the assistant is unreachable: ' + e + ')');
  }).then(function() {
    _asstBusy = false;
    document.getElementById('assistant-send').disabled = false;
  });
  });
}

</script>
"""


def _page_html(enabled: bool) -> str:
    """The dashboard page, with the assistant spliced in when it is switched on."""
    if not enabled:
        return _DASHBOARD_HTML
    return _DASHBOARD_HTML.replace("</body>", _ASSISTANT_FRAGMENT + "</body>", 1)


def _entity_tags(supplied: Any) -> list[str]:
    """The versions an ``If-Match`` header names, as bare hashes.

    158 (#241). RFC 7232 3.2:
    ``If-Match = "*" / [ entity-tag *( OWS "," OWS entity-tag ) ]``, and an
    entity-tag may be weak (``W/"..."``). The dashboard's own JS sends exactly one
    strong tag, so the parse was written for that -- and every other legal shape
    then failed as a *version conflict*, which is a lie about what went wrong.

    A seventh review round: splitting on every comma and then removing quotes is the
    obvious order and the wrong one. ``etagc`` is ``%x21 / %x23-7E``, so a comma is
    legal *inside* a tag, and ``"abc,def"`` was being torn into two. The scan below
    only separates at a comma outside quotes. No version this deployment issues
    contains one, and the failure was to refuse rather than to permit, but a parser
    that answers the wrong question about a legal input is a defect regardless of
    who is currently asking.

    The wildcard is returned as ``"*"`` for the caller to refuse; it is the one tag
    that cannot be compared against a hash, and honouring it would mean writing
    without holding a version.
    """
    raw = str(supplied).strip()
    if raw == "*":
        return ["*"]

    members: list[str] = []
    buffer: list[str] = []
    quoted = False
    for char in raw:
        if char == '"':
            quoted = not quoted
        elif char == "," and not quoted:
            members.append("".join(buffer))
            buffer = []
            continue
        buffer.append(char)
    members.append("".join(buffer))

    tags: list[str] = []
    for member in members:
        tag = member.strip()
        # A weak validator names the same version as the bare hash a caller may be
        # holding: these tags are content hashes, so weak and strong comparison
        # coincide. RFC 7232 says weak tags SHOULD NOT be sent on If-Match; refusing
        # one would fail a write over a prefix rather than over the version. The
        # strip afterwards is for `W/ "x"`, which is malformed -- there is no space
        # in the grammar -- and was leaving a stray quote welded to the hash.
        if tag.startswith("W/"):
            tag = tag[2:].strip()
        # A matched pair only, so a caller holding the bare hash (which is what the
        # body vehicle hands out, and what the 081 writes take) is left alone.
        if len(tag) >= 2 and tag.startswith('"') and tag.endswith('"'):
            tag = tag[1:-1]
        tag = tag.strip()
        if tag:
            tags.append(tag)
    return tags


def _version_refusal(
    config_path: Any, supplied: str | None, *, field: str = "If-Match",
) -> JSONResponse | None:
    """Refuse a write that carries no usable version, or ``None`` to proceed.

    158 (#241): one implementation, because five routes needed this and five copies
    of a concurrency check is five chances to get it subtly different -- and the
    difference would show up as a lost edit, which is the failure nobody notices.

    Callers that already have their own body-field version (the global config write,
    the catalog writes) keep it; this is what the rest use.
    """
    from coordinare.services.config_write_service import (
        ConcurrencyConflictError,
        guard_concurrency,
        safe_failure_reason,
    )

    # No file, nothing to overwrite. The writers already no-op in this case, so
    # demanding a version here would refuse a write that was never going to happen
    # -- and there would be no version to give, since the version *is* the file.
    if config_path is None or not config_path.is_file():
        return None

    if supplied is None or not str(supplied).strip():
        _log.warning("config.write_refused_unguarded", field=field)
        return JSONResponse(
            {
                "error": (
                    f"{field} is required. The matching GET returns the current version "
                    "as an ETag; send it back so a concurrent edit is refused rather "
                    "than overwritten."
                ),
                "precondition_required": True,
            },
            status_code=428,
        )

    def _unreadable_refusal(exc: OSError) -> JSONResponse:
        # guard_concurrency deliberately lets OSError through -- its docstring says
        # callers should "degrade to a structured forbidden result rather than
        # proceeding", because compute_content_hash returns the empty-file sentinel
        # for a present-but-unreadable file and a bare comparison would let that
        # sentinel match and clobber data nobody read. Catching only the conflict
        # turned that into a 500 with a traceback. 403 and this message are what
        # _conflict_result already returns for the 081 writes.
        # str(OSError) carries the absolute path and this body goes to the browser;
        # safe_failure_reason keeps the useful half. The full exception goes to the log.
        _log.warning("config.write_refused_unreadable", error=str(exc))
        return JSONResponse(
            {"error": f"Could not read the configuration file: {safe_failure_reason(exc)}"},
            status_code=403,
        )

    # RFC 7232 3.2 lets If-Match carry a comma-separated list of entity-tags, with
    # optional whitespace, each optionally weak. The parse used to be
    # `removeprefix("W/").strip('"')`, which is right for the one shape this
    # dashboard's own JS sends and wrong for every other legal one -- and it failed
    # them as a 409 saying the config had changed, which is untrue and sends an
    # operator looking for a concurrent edit that never happened.
    tags = _entity_tags(supplied)

    # A header that parses to no tag at all (`,,`, `""`) names no version, and an
    # empty loop below would fall through as though the guard had passed. It is the
    # same situation as a missing header, so it gets the same answer.
    if not tags:
        _log.warning("config.write_refused_unguarded", field=field, reason="no_entity_tag")
        return JSONResponse(
            {
                "error": (
                    f"{field} names no version. The matching GET returns the current "
                    "version as an ETag; send it back so a concurrent edit is refused "
                    "rather than overwritten."
                ),
                "precondition_required": True,
            },
            status_code=428,
        )

    # `*` means "any current representation", i.e. write without holding a version.
    # That is precisely what this spec removed, so it is refused rather than honoured
    # -- but refused as a wildcard, so the message says what is actually wrong.
    if "*" in tags:
        _log.warning("config.write_refused_wildcard", field=field)
        return JSONResponse(
            {
                "error": (
                    f"{field}: * is not accepted here. A write needs the version it "
                    "is replacing, so a concurrent edit can be refused; the matching "
                    "GET returns it as an ETag."
                ),
                "precondition_required": True,
            },
            status_code=428,
        )

    # Any-match, per RFC 7232: the caller holds a version that is current. Sorting the
    # conflict to last keeps the error the one a stale caller should see.
    # Flat rather than nested: `test_both_guards_catch_it` reads the try that holds
    # each guard_concurrency call, and an inner try catching only the conflict fails
    # it even when an outer one catches OSError. Over-strict in the safe direction,
    # and this reads better anyway.
    conflict: ConcurrencyConflictError | None = None
    for tag in tags:
        try:
            guard_concurrency(config_path, tag)
        except ConcurrencyConflictError as err:
            conflict = err
            continue
        except OSError as exc:
            return _unreadable_refusal(exc)
        conflict = None
        break
    if conflict is not None:
        return JSONResponse({"error": str(conflict), "conflict": True}, status_code=409)
    return None


def _version_headers(config_path: Any) -> dict[str, str]:
    """The current version of the config file, as an ``ETag`` header.

    158 (#241): the counterpart to :func:`_version_refusal`, and the reason that
    function can insist on a version at all. A guard that demands a version the
    client has no way to obtain is not a guard, it is an outage -- and this is the
    shape 156 already chose for the global config page: the header, so a response
    whose body is a values contract stays one.

    Emitted by the GETs a page loads from *and* by the writes themselves, carrying
    the post-write version, so a second save in the same session does not have to
    re-read the page to find out what it just created.

    The front end keeps one of these per editing page (``_symCfgHash``,
    ``_personaCfgHash``, and 156's ``_adminCfgHash``). One config file and three
    variables looks like redundancy and must not be consolidated: the version has to
    be bound to the *data the page is showing*, not to the file's latest state.
    Consolidated, you load Personas (hash H1, text T1), someone else edits that
    persona (file now H2), you visit Symphonies whose GET refreshes the shared hash to
    H2, you return and save T1 -- the guard sees a matching hash and overwrites their
    change without a word. Separate variables make that a 409, which is the entire
    point of the exercise.

    This docstring is also where that argument lives rather than in a comment beside
    the JS, because everything inside the dashboard HTML ships to every browser and
    counts against the 160 KB budget, which currently has under 1% of headroom.
    """
    if config_path is None or not config_path.is_file():
        return {}
    from coordinare.services.config_write_service import compute_content_hash

    # Quoted, per RFC 7232 §2.3. _version_refusal strips the quotes back off.
    return {"ETag": f'"{compute_content_hash(config_path)}"'}


def _config_assistant_enabled(daemon: Any) -> bool:
    """Is the config assistant switched on for this deployment?

    Reads the live config rather than a captured value so a reload does not leave
    the flag stale. Defaults to off on anything unexpected: a feature that talks to
    a model endpoint and reads configuration should require a deliberate yes, not
    survive an ambiguous one.
    """
    try:
        cfg = daemon.state.get("coordinare_config")
    except Exception:
        return False
    if cfg is None:
        return False
    # CoordinareConfiguration is the multi-symphony root; the global settings live on
    # its `global_config`. Accept either shape so a caller holding a
    # ProjectConfiguration directly is not silently treated as "disabled".
    holder = getattr(cfg, "global_config", cfg)
    return bool(getattr(holder, "config_assistant_enabled", False))


def create_dashboard_app(
    store: DashboardStore,
    daemon: CoordinareDaemon,
    metrics: CoordinareMetrics,
    health: HealthRegistry,
    config_path: Path | None = None,
    *,
    permitted_origins: PermittedOrigins | None = None,
    guard_exempt_paths: frozenset[str] | None = None,
    auth_token: SecretStr | None = None,
    signed_webhook_paths: frozenset[str] = frozenset(),
) -> FastAPI:
    """Create the dashboard FastAPI application.

    Endpoints:
        GET /        — serves the dashboard HTML page
        GET /events  — SSE stream of state_update events
        GET /api/personas          — list all role personas (018)
        PUT /api/personas/{role}   — update persona for a role (018)
        DELETE /api/personas/{role} — reset persona to defaults (018)

    ``permitted_origins`` configures the localhost guard (spec 144). When omitted
    it defaults to loopback on the default dashboard port, which is the safe
    posture; production passes a set derived from the live configuration so the
    operator's bind address, port, and any trusted proxy hostname are honoured.
    """
    app = FastAPI(title="coordinare-dashboard")
    if auth_token is not None:
        from coordinare.dashboard_auth import DashboardAuthentication
        app.add_middleware(
            DashboardAuthentication, token=auth_token,
            signed_webhook_paths=signed_webhook_paths,
        )

    # Spec 144 (#198). Installed BEFORE the request logger deliberately.
    # FastAPI middleware is outermost-last, so the logger added below wraps this
    # guard: every request including a rejected one still appears in the request
    # log, while the guard refuses before any route handler runs.
    install_localhost_guard(
        app,
        permitted_origins
        or build_permitted(dashboard_host="127.0.0.1", dashboard_port=8090),
        exempt_paths=guard_exempt_paths,
    )

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

    @app.get("/static/activity-streams.js")
    async def activity_stream_script() -> Response:
        return Response(_ACTIVITY_STREAM_JS, media_type="application/javascript",
                        headers={"Cache-Control": "no-cache"})

    @app.get("/", response_class=HTMLResponse)
    async def dashboard() -> HTMLResponse:
        return HTMLResponse(_page_html(_assistant_on))

    # 049: Multi-page routes — same HTML shell, JS router handles rendering
    @app.get("/performers", response_class=HTMLResponse)
    async def dashboard_performers() -> HTMLResponse:
        return HTMLResponse(_page_html(_assistant_on))

    @app.get("/personas", response_class=HTMLResponse)
    async def dashboard_personas() -> HTMLResponse:
        return HTMLResponse(_page_html(_assistant_on))

    @app.get("/history", response_class=HTMLResponse)
    async def dashboard_history() -> HTMLResponse:
        return HTMLResponse(_page_html(_assistant_on))

    # 057: Symphony management routes
    @app.get("/symphonies", response_class=HTMLResponse)
    async def dashboard_symphonies() -> HTMLResponse:
        """Display the symphonies list page (Task 8)."""
        return HTMLResponse(_page_html(_assistant_on))

    @app.get("/symphonies/{name}", response_class=HTMLResponse)
    async def dashboard_symphony_detail(name: str) -> HTMLResponse:
        """Display the detail page for a specific symphony (Task 8)."""
        return HTMLResponse(_page_html(_assistant_on))

    @app.get("/admin/config", response_class=HTMLResponse)
    async def dashboard_admin_config() -> HTMLResponse:
        """Display the admin configuration page (Task 11)."""
        return HTMLResponse(_page_html(_assistant_on))

    @app.get("/config", response_class=HTMLResponse)
    async def dashboard_config() -> HTMLResponse:
        """Display the full live-config view (spec 081-config-ui)."""
        return HTMLResponse(_page_html(_assistant_on))

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
            dir=config_path.parent, prefix=".coordinare_config_", suffix=".yaml.tmp",
        )
        try:
            with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
                yaml.safe_dump(
                    loaded, f, default_flow_style=False, allow_unicode=True, sort_keys=False,
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
        }, headers=_version_headers(config_path))

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
        }, headers=_version_headers(config_path))

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

        # 158 (#241): after validation, so "already exists" and a malformed body stay
        # 400/409 rather than becoming a demand for a version to do something that was
        # never going to happen -- but BEFORE the mutation below, because this handler
        # writes daemon.state before it writes the file. A guard placed beside the
        # write refuses having already added the symphony to memory and bumped
        # config_version, which is a worse outcome than the overwrite it prevented.
        # That was the bug in delete_symphony; it was in this handler and in
        # update_symphony too, and the tests missed it because they compared file
        # bytes and only checked memory for delete.
        refusal = _version_refusal(config_path, request.headers.get("If-Match"))
        if refusal is not None:
            return refusal

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
        }, status_code=201, headers=_version_headers(config_path))

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
                    {"error": "Request body must be a JSON object"}, status_code=400,
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

        # 158 (#241): a copy, as create_symphony already took. This handler mutates
        # the dict before it persists, and the live daemon.state one would make any
        # statement that lands above the version guard corrupt shared state the
        # instant it runs, with nothing to roll back from. The guard is above every
        # mutation today and a test asserts that; this makes the ordering a bug
        # rather than a catastrophe if it ever stops holding.
        symphony_configs = dict(daemon.state.get("symphony_configs") or {})

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

        # 158 (#241): before the mutation, for the reason spelt out in create_symphony.
        refusal = _version_refusal(config_path, request.headers.get("If-Match"))
        if refusal is not None:
            return refusal

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
        }, headers=_version_headers(config_path))

    @app.delete("/api/symphonies/{name}")
    async def delete_symphony(name: str, request: Request) -> JSONResponse:
        """Remove a symphony (Task 9). Returns 409 if it would remove the last symphony."""
        if daemon._cycle_active:
            return JSONResponse(
                    {"error": "A cycle is in progress — please try again shortly", "status": "cycle_in_progress"},
                    status_code=409,
                )

        # 158 (#241): a copy, as create_symphony already took. This handler mutates
        # the dict before it persists, and the live daemon.state one would make any
        # statement that lands above the version guard corrupt shared state the
        # instant it runs, with nothing to roll back from. The guard is above every
        # mutation today and a test asserts that; this makes the ordering a bug
        # rather than a catastrophe if it ever stops holding.
        symphony_configs = dict(daemon.state.get("symphony_configs") or {})

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

        # 158 (#241): the last thing before any mutation. All three symphony handlers
        # write daemon.state before they persist, so a guard placed by the write
        # refuses having already made the change it is refusing. This was found here
        # first, and the claim that the other four "sit beside the write" was then
        # left standing for two commits while create_symphony and update_symphony had
        # the same bug -- because the in-memory test was written for this handler
        # only, and the other four were checked by comparing file bytes.
        # TestARefusalTouchesNoStateAtAll now covers all five, and asserts the
        # ordering structurally so it cannot drift back.
        refusal = _version_refusal(config_path, request.headers.get("If-Match"))
        if refusal is not None:
            return refusal

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

        return JSONResponse({"deleted": name}, headers=_version_headers(config_path))

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
                {"error": f"Symphony {name!r} not found"}, status_code=404,
            )

        sym_cfg = symphony_configs[name]
        if getattr(sym_cfg, "env_bootstrap_performer_id", None) is None:
            return JSONResponse(
                {
                    "error": (
                        f"Symphony {name!r} has no env_bootstrap_performer_id "
                        "configured"
                    ),
                },
                status_code=400,
            )

        env_cache_svc = daemon.state.get("env_cache_service")
        if env_cache_svc is None:
            return JSONResponse(
                {"error": "Env cache service not available"}, status_code=503,
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
                    ),
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

    @app.post("/api/symphonies/{name}/wiki-init")
    async def trigger_wiki_init(name: str) -> JSONResponse:
        """Manually seed a symphony's living docs/wiki (spec 124 US2).

        Records a wiki-init request that the daemon's next cycle dispatches: our
        documenter runs in ``init`` mode against the repo, opens a seed PR, and
        WikiInitService auto-merges it on CI-green + trusted-bot approval. Operator
        -initiated (no auto-gate), so it never holds other dispatch. 202 on accept;
        404 unknown symphony; 409 if a wiki-init is already in flight; 503 if the
        env-cache state isn't ready yet.
        """
        symphony_configs = daemon.state.get("symphony_configs") or {}
        if name not in symphony_configs:
            return JSONResponse({"error": f"Symphony {name!r} not found"}, status_code=404)

        cache_state = (daemon.state.get("env_cache") or {}).get(name)
        if cache_state is None:
            return JSONResponse(
                {"error": (
                    f"Symphony {name!r} env-cache state has not been initialised "
                    "yet — wait one cycle and retry"
                )},
                status_code=503,
            )
        if getattr(cache_state, "wiki_in_flight", False):
            return JSONResponse(
                {"error": "A wiki-init is already in flight for this symphony",
                 "status": "wiki_in_flight"},
                status_code=409,
            )

        # Record the manual request; the daemon cycle drains it (mirrors the
        # env-bootstrap flag-then-poke pattern). ``_wiki_init_requests`` is always
        # created in the daemon's __init__.
        daemon._wiki_init_requests.add(name)
        if hasattr(daemon, "_webhook_trigger"):
            daemon._webhook_trigger.set()

        return JSONResponse(
            {"status": "accepted", "symphony": name}, status_code=202,
        )

    def _build_config_snapshot_inputs() -> tuple[Any, dict[str, str | None], dict[str, Any], bool, bool]:
        """Gather the shared inputs for ``build_snapshot`` / ``build_section`` (T016/T017).

        Returns ``(coordinare_cfg, content_hashes, raw_values, routing_avail,
        routing_mounted)``. ``routing_mounted`` is true whenever a performer
        endpoint mounts a routing path (even if its host file is missing), so the
        empty-state banner can distinguish "unmounted" from "mounted-but-broken".
        Display values (``raw_values``) come from the raw on-disk YAML so ``${VAR}``
        literals are preserved verbatim (research D3); the validated config object
        can never hold an unresolved placeholder for the pat ``github_token``.
        """
        from coordinare import routing_config_service
        from coordinare.services.config_write_service import compute_content_hash

        coordinare_cfg = daemon.state.get("coordinare_config")

        content_hashes: dict[str, str | None] = {}
        raw_values: dict[str, Any] = {}
        if config_path is not None and config_path.is_file():
            import yaml as _yaml

            # Tolerant read: an out-of-band edit / mount glitch can leave the file
            # temporarily unreadable or YAML-malformed. The descriptor layer can
            # still render from the in-memory validated config, so degrade only the
            # raw "${VAR}" display values rather than 500 the whole display fetch.
            try:
                content_hashes["config_yaml"] = compute_content_hash(config_path)
                loaded = _yaml.safe_load(config_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, _yaml.YAMLError):
                content_hashes.setdefault("config_yaml", None)
                loaded = None
            if isinstance(loaded, dict):
                for fname, fval in loaded.items():
                    if not isinstance(fval, (dict, list)):
                        raw_values[f"global.{fname}"] = fval

        routing_avail = False
        routing_mounted = False
        if coordinare_cfg is not None:
            endpoints = coordinare_cfg.global_config.performer_endpoints
            routing_avail = routing_config_service.routing_available(endpoints)
            location = routing_config_service.locate_routing_file(endpoints)
            routing_mounted = location is not None
            if location is not None and location.host_path.is_file():
                content_hashes["routing_yaml"] = compute_content_hash(location.host_path)
            else:
                content_hashes["routing_yaml"] = None

        return coordinare_cfg, content_hashes, raw_values, routing_avail, routing_mounted

    @app.get("/api/config/all")
    async def get_config_all() -> JSONResponse:
        """Return the full editable config surface as a ``ConfigSnapshot`` (T016).

        Descriptors + content_hashes + config_version + routing_available. Secrets
        are masked and ``${VAR}`` placeholders preserved by the descriptor layer.
        """
        from coordinare import config_descriptors as cd

        coordinare_cfg, content_hashes, raw_values, routing_avail, routing_mounted = (
            _build_config_snapshot_inputs()
        )
        if coordinare_cfg is None:
            return JSONResponse({"error": "Config not available"}, status_code=500)

        config_version = daemon.state.get("config_version", 0)
        snapshot = cd.build_snapshot(
            coordinare_cfg,
            config_version=config_version,
            routing_available=routing_avail,
            routing_mounted=routing_mounted,
            content_hashes=content_hashes,
            raw_values=raw_values,
        )
        return JSONResponse(snapshot.model_dump(mode="json"))

    @app.get("/api/config/section/{section_id}")
    async def get_config_section(section_id: str) -> JSONResponse:
        """Return a single config section by id; 404 on unknown id (T017)."""
        from fastapi import HTTPException

        from coordinare import config_descriptors as cd

        coordinare_cfg, content_hashes, raw_values, routing_avail, routing_mounted = (
            _build_config_snapshot_inputs()
        )
        if coordinare_cfg is None:
            return JSONResponse({"error": "Config not available"}, status_code=500)

        try:
            section = cd.build_section(
                coordinare_cfg,
                section_id,
                routing_available=routing_avail,
                routing_mounted=routing_mounted,
                content_hashes=content_hashes,
                raw_values=raw_values,
            )
        except KeyError as err:
            raise HTTPException(
                status_code=404, detail=f"Unknown section: {section_id}",
            ) from err
        return JSONResponse(section.model_dump(mode="json"))

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

    from coordinare.config_descriptors import GLOBAL_EDITABLE_FIELDS

    _global_cfg_editable = GLOBAL_EDITABLE_FIELDS

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

        body = {k: _serialize(getattr(cfg, k, None)) for k in _global_cfg_editable}
        headers: dict[str, str] = {}
        # 156 (#237): the page needs the file's content hash to save safely, and had
        # no way to get one -- this endpoint returns values only, and the hash lives
        # in the far heavier /api/config/all. It cannot go in the body either: an
        # existing test pins this response's exact key set, correctly, because that
        # is a values contract. An ETag is what the header is for, and adds nothing
        # to the body.
        if config_path is not None and config_path.is_file():
            from coordinare.services.config_write_service import compute_content_hash

            # Quoted, per RFC 7232 §2.3: an entity-tag is a DQUOTE-enclosed opaque
            # tag. A bare sha256:... happens to work for our own string comparison
            # and is still a malformed header, which is the kind of thing that works
            # until something between us and the browser starts caring.
            headers["ETag"] = f'"{compute_content_hash(config_path)}"'
        return JSONResponse(body, headers=headers)

    @app.put("/api/config/global")
    async def update_global_config(request: Request) -> JSONResponse:
        """Persist editable global config fields to config.yaml and trigger reload."""
        import os
        import stat
        import tempfile

        import yaml

        from coordinare.services.config_write_service import compute_content_hash

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

        # 155 (#202): optional optimistic-concurrency guard. The catalog and routing
        # endpoints have carried one since spec 081; this one never did, so two
        # dashboards open on the same config could silently overwrite each other
        # (#237). Optional rather than required so no existing caller changes
        # behaviour -- the config assistant's Apply always sends one, so every
        # assistant-driven write is guarded even while the older UI is not.
        expected_hash = body.pop("expected_hash", None)
        if expected_hash is None:
            # 158: If-Match is the mechanism the other writes use, so accept it here
            # too and document one thing. The body field wins when both are sent,
            # because callers written against 157 already rely on it.
            expected_hash = request.headers.get("If-Match")
        if expected_hash is None:
            # 157: required, not merely accepted. Spec 156 logged these instead of
            # refusing them, because refusing is a contract change and nobody could
            # weigh it without knowing how often it happened. Every first-party
            # caller now sends one, so what remains is automation writing config with
            # no protection against overwriting a concurrent edit -- and silently
            # losing someone's change is worse than a loud failure a script can be
            # taught to handle.
            #
            # 428 rather than 400: the request is well-formed, and what is missing is
            # a precondition. RFC 6585 §3 exists for exactly this, and it tells a
            # caller *what* to do rather than only that they were wrong.
            _log.warning(
                "config.global_write_refused_unguarded",
                hint="no expected_hash sent; refusing rather than risking a lost edit",
            )
            return JSONResponse(
                {
                    "error": (
                        "expected_hash is required. GET /api/config/global returns the "
                        "current version as an ETag; send it back as expected_hash so a "
                        "concurrent edit is refused rather than overwritten."
                    ),
                    "precondition_required": True,
                },
                status_code=428,
            )
        if expected_hash is not None:
            from coordinare.services.config_write_service import (
                ConcurrencyConflictError,
                guard_concurrency,
            )

            if not isinstance(expected_hash, str):
                return JSONResponse(
                    {"error": "expected_hash must be a string"}, status_code=400,
                )
            # A client that read the hash from the ETag sends it back with its
            # quotes, and a client holding it from `new_hash` sends it bare. Both
            # are the same version, so both are accepted -- otherwise quoting the
            # header correctly would have made every save from the page 409.
            expected_hash = expected_hash.removeprefix("W/").strip('"')
            try:
                guard_concurrency(config_path, expected_hash)
            except ConcurrencyConflictError as err:
                return JSONResponse({"error": str(err), "conflict": True}, status_code=409)
            except OSError as exc:
                # Same contract as _version_refusal; see the comment there.
                # Path kept out of the body; see the comment in _version_refusal.
                from coordinare.services.config_write_service import safe_failure_reason

                _log.warning("config.global_write_refused_unreadable", error=str(exc))
                return JSONResponse(
                    {
                        "error": "Could not read the configuration file: "
                        f"{safe_failure_reason(exc)}",
                    },
                    status_code=403,
                )

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

        # 156: hand back the new version, the way the catalog endpoints already do
        # (`new_hash`, consumed at the 081 page's save). Without it a page that saved
        # successfully would have no current hash, and its NEXT save would go
        # unguarded -- the guard would hold exactly once per page load, which is
        # worse than useless because it looks like it holds always.
        saved_hash = (
            compute_content_hash(config_path)
            if config_path is not None and config_path.is_file()
            else None
        )
        return JSONResponse({
            "status": "saved",
            "reload_triggered": hasattr(daemon, "_config_reload_trigger"),
            "new_hash": saved_hash,
        })

    # -----------------------------------------------------------------------
    # 081 — Live config editing: section save + spec-080 catalog CRUD
    # -----------------------------------------------------------------------

    def _save_status_code(result: Any) -> int:
        """Map a ``SaveResult`` to an HTTP status (T032).

        ok → 200; otherwise the first error's code decides:
        conflict/referenced → 409, validation → 422, forbidden → 403.
        Messages are already secret-free and stack-trace-free at the service layer.
        """
        if result.ok:
            return 200
        code = result.errors[0].code if result.errors else "validation"
        return {
            "conflict": 409,
            "referenced": 409,
            "validation": 422,
            "forbidden": 403,
        }.get(code, 422)

    def _trigger_reload() -> None:
        """Fire the daemon's hot-reload trigger (and wake the loop). May raise."""
        daemon._config_reload_trigger.set()
        if hasattr(daemon, "_webhook_trigger"):
            daemon._webhook_trigger.set()

    def _apply_reload_or_stage(result: Any, what: str) -> None:
        """Fire the hot-reload trigger for a successful write, enforcing FR-015.

        On a successful ``hot_reloaded`` write the daemon reload trigger is fired.
        If the trigger raises after the atomic write, the change stays on disk but
        the live config is NOT swapped — downgrade the result to ``staged_restart``
        with an operator-readable, secret-free advisory (mirroring
        ``put_config_section``) rather than falsely reporting the change as live.
        """
        if not (result.ok and result.applied == "hot_reloaded"):
            return
        if not hasattr(daemon, "_config_reload_trigger"):
            return
        try:
            _trigger_reload()
        except Exception:
            _log.warning("config_reload_failed", what=what)
            result.applied = "staged_restart"
            result.message = (
                "Saved to disk, but the live reload failed; restart the "
                "coordinare to apply this change."
            )

    @app.put("/api/config/section/{section_id}")
    async def put_config_section(section_id: str, request: Request) -> JSONResponse:
        """Validate + persist a scalar-group section edit (T027).

        On a ``hot_reloaded`` result the daemon reload trigger is fired. FR-015
        (T047): if the trigger raises after the successful atomic write, the
        change stays on disk but the live config is NOT swapped — we downgrade
        the result to ``staged_restart`` with an operator-readable, secret-free
        advisory rather than reporting a failure.
        """
        from pydantic import ValidationError

        from coordinare.services.config_write_service import SaveRequest, save_section

        if config_path is None:
            return JSONResponse(
                {"ok": False, "errors": [{"key": None, "code": "forbidden",
                 "message": "Config file not available; changes cannot be persisted."}]},
                status_code=403,
            )
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "errors": [{"key": None,
                "code": "validation", "message": "Invalid JSON body."}]}, status_code=422)
        if not isinstance(body, dict):
            return JSONResponse({"ok": False, "errors": [{"key": None,
                "code": "validation", "message": "Request body must be a JSON object."}]},
                status_code=422)

        # The URL is authoritative: this route only ever writes the named scalar
        # section in config.yaml. Any `store`/`section` in the body is ignored so a
        # mismatched payload can't redirect the write to a different target.
        try:
            save_req = SaveRequest(
                store="config_yaml",
                section=section_id,
                changes=body.get("changes", {}),
                base_hash=body.get("base_hash", ""),
            )
        except ValidationError:
            return JSONResponse({"ok": False, "errors": [{"key": None,
                "code": "validation", "message": "Invalid request payload."}]},
                status_code=422)
        result = save_section(save_req, config_path)

        if result.ok and result.applied == "hot_reloaded" and hasattr(daemon, "_config_reload_trigger"):
            try:
                _trigger_reload()
            except Exception:
                _log.warning("config_section_reload_failed", section=section_id)
                result.applied = "staged_restart"
                result.message = (
                    "Saved to disk, but the live reload failed; restart the "
                    "coordinare to apply this change."
                )

        return JSONResponse(result.model_dump(mode="json"), status_code=_save_status_code(result))

    @app.get("/api/config/catalog/{catalog}")
    async def get_config_catalog(catalog: str) -> JSONResponse:
        """Return a single spec-080 catalog with referential-integrity flags (T028)."""
        from fastapi import HTTPException

        from coordinare import config_descriptors as cd
        from coordinare.services.config_write_service import (
            CATALOG_KEYS,
            compute_content_hash,
        )

        if catalog not in CATALOG_KEYS:
            raise HTTPException(status_code=404, detail=f"Unknown catalog: {catalog}")

        coordinare_cfg = daemon.state.get("coordinare_config")
        if coordinare_cfg is None:
            return JSONResponse({"error": "Config not available"}, status_code=500)

        section = next(
            (s for s in cd._build_catalog_sections(coordinare_cfg) if s.id == catalog),
            None,
        )
        if section is None:
            raise HTTPException(status_code=404, detail=f"Unknown catalog: {catalog}")

        content_hash = (
            compute_content_hash(config_path)
            if config_path is not None and config_path.is_file()
            else None
        )
        return JSONResponse({
            "id": catalog,
            "content_hash": content_hash,
            "items": [i.model_dump(mode="json") for i in section.items],
        })

    @app.post("/api/config/catalog/{catalog}")
    async def post_config_catalog(catalog: str, request: Request) -> JSONResponse:
        """Create a new item in a spec-080 catalog (T029)."""
        from coordinare.services.config_write_service import (
            CATALOG_KEYS,
            create_catalog_item,
        )

        if catalog not in CATALOG_KEYS:
            return JSONResponse({"ok": False, "errors": [{"key": None,
                "code": "validation", "message": f"Unknown catalog: {catalog}"}]},
                status_code=422)
        if config_path is None:
            return JSONResponse({"ok": False, "errors": [{"key": None,
                "code": "forbidden", "message": "Config file not available."}]},
                status_code=403)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "errors": [{"key": None,
                "code": "validation", "message": "Invalid JSON body."}]}, status_code=422)
        if not isinstance(body, dict):
            return JSONResponse({"ok": False, "errors": [{"key": None,
                "code": "validation", "message": "Request body must be a JSON object."}]},
                status_code=422)

        result = create_catalog_item(
            catalog, body.get("item", {}), body.get("base_hash", ""), config_path,
        )
        _apply_reload_or_stage(result, f"catalog POST {catalog}")
        return JSONResponse(result.model_dump(mode="json"), status_code=_save_status_code(result))

    @app.put("/api/config/catalog/{catalog}/{item_id}")
    async def put_config_catalog_item(catalog: str, item_id: str, request: Request) -> JSONResponse:
        """Update an existing item in a spec-080 catalog (T030)."""
        from coordinare.services.config_write_service import (
            CATALOG_KEYS,
            update_catalog_item,
        )

        if catalog not in CATALOG_KEYS:
            return JSONResponse({"ok": False, "errors": [{"key": None,
                "code": "validation", "message": f"Unknown catalog: {catalog}"}]},
                status_code=422)
        if config_path is None:
            return JSONResponse({"ok": False, "errors": [{"key": None,
                "code": "forbidden", "message": "Config file not available."}]},
                status_code=403)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "errors": [{"key": None,
                "code": "validation", "message": "Invalid JSON body."}]}, status_code=422)
        if not isinstance(body, dict):
            return JSONResponse({"ok": False, "errors": [{"key": None,
                "code": "validation", "message": "Request body must be a JSON object."}]},
                status_code=422)

        result = update_catalog_item(
            catalog, item_id, body.get("changes", {}), body.get("base_hash", ""), config_path,
        )
        _apply_reload_or_stage(result, f"catalog PUT {catalog}/{item_id}")
        return JSONResponse(result.model_dump(mode="json"), status_code=_save_status_code(result))

    @app.delete("/api/config/catalog/{catalog}/{item_id}")
    async def delete_config_catalog_item(catalog: str, item_id: str, request: Request) -> JSONResponse:
        """Delete a catalog item unless referenced (T031, delete-protection)."""
        from coordinare.services.config_write_service import (
            CATALOG_KEYS,
            delete_catalog_item,
        )

        if catalog not in CATALOG_KEYS:
            return JSONResponse({"ok": False, "errors": [{"key": None,
                "code": "validation", "message": f"Unknown catalog: {catalog}"}]},
                status_code=422)
        if config_path is None:
            return JSONResponse({"ok": False, "errors": [{"key": None,
                "code": "forbidden", "message": "Config file not available."}]},
                status_code=403)
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}

        result = delete_catalog_item(
            catalog, item_id, body.get("base_hash", ""), config_path,
        )
        _apply_reload_or_stage(result, f"catalog DELETE {catalog}/{item_id}")
        return JSONResponse(result.model_dump(mode="json"), status_code=_save_status_code(result))

    # -----------------------------------------------------------------------
    # 081 — Routing-table CRUD (spec-078 self-hosted backend)
    # -----------------------------------------------------------------------

    def _routing_location() -> Any:
        """Resolve the host-side routing-table location from the live config, or None.

        Returns the :class:`RoutingLocation` whenever a performer endpoint mounts a
        routing path — *even when the host-side file is missing or not a regular
        file*. The writability decision (and its accurate operator-facing message)
        belongs to ``routing_config_service._readonly_guard()``: collapsing a
        mounted-but-missing location to ``None`` here would lose the actionable
        "endpoint X mounts a path that isn't a file" diagnostic and emit the
        generic "no endpoint mounts a routing table" message instead. ``None`` is
        returned only when no endpoint mounts a routing table at all.
        """
        from coordinare import routing_config_service

        coordinare_cfg = daemon.state.get("coordinare_config")
        if coordinare_cfg is None:
            return None
        endpoints = coordinare_cfg.global_config.performer_endpoints
        return routing_config_service.locate_routing_file(endpoints)

    @app.get("/api/config/routing")
    async def get_config_routing() -> JSONResponse:
        """Return the routing table (entries + content_hash) or read-only empty state (T037)."""
        from coordinare import routing_config_service

        location = _routing_location()
        view = routing_config_service.read_routing(location)
        return JSONResponse(view.model_dump(mode="json"))

    @app.post("/api/config/routing/entry")
    async def post_config_routing_entry(request: Request) -> JSONResponse:
        """Create a routing entry → ``applied: staged_next_job`` (T038)."""
        from coordinare import routing_config_service

        location = _routing_location()
        guard = routing_config_service._readonly_guard(location)
        if guard is not None:
            return JSONResponse(guard.model_dump(mode="json"),
                                status_code=_save_status_code(guard))
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "errors": [{"key": None,
                "code": "validation", "message": "Invalid JSON body."}]}, status_code=422)
        if not isinstance(body, dict):
            return JSONResponse({"ok": False, "errors": [{"key": None,
                "code": "validation", "message": "Request body must be a JSON object."}]},
                status_code=422)

        result = routing_config_service.create_routing_entry(
            location, body.get("entry", {}), body.get("base_hash", ""),
        )
        return JSONResponse(result.model_dump(mode="json"), status_code=_save_status_code(result))

    @app.put("/api/config/routing/entry/{index}")
    async def put_config_routing_entry(index: int, request: Request) -> JSONResponse:
        """Update the routing entry at ``index`` → ``staged_next_job`` (T039)."""
        from coordinare import routing_config_service

        location = _routing_location()
        guard = routing_config_service._readonly_guard(location)
        if guard is not None:
            return JSONResponse(guard.model_dump(mode="json"),
                                status_code=_save_status_code(guard))
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"ok": False, "errors": [{"key": None,
                "code": "validation", "message": "Invalid JSON body."}]}, status_code=422)
        if not isinstance(body, dict):
            return JSONResponse({"ok": False, "errors": [{"key": None,
                "code": "validation", "message": "Request body must be a JSON object."}]},
                status_code=422)

        result = routing_config_service.update_routing_entry(
            location, index, body.get("changes", {}), body.get("base_hash", ""),
        )
        return JSONResponse(result.model_dump(mode="json"), status_code=_save_status_code(result))

    @app.delete("/api/config/routing/entry/{index}")
    async def delete_config_routing_entry(index: int, request: Request) -> JSONResponse:
        """Delete the routing entry at ``index`` → ``staged_next_job`` (T040)."""
        from coordinare import routing_config_service

        location = _routing_location()
        guard = routing_config_service._readonly_guard(location)
        if guard is not None:
            return JSONResponse(guard.model_dump(mode="json"),
                                status_code=_save_status_code(guard))
        try:
            body = await request.json()
        except Exception:
            body = {}
        if not isinstance(body, dict):
            body = {}

        result = routing_config_service.delete_routing_entry(
            location, index, body.get("base_hash", ""),
        )
        return JSONResponse(result.model_dump(mode="json"), status_code=_save_status_code(result))

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
        return JSONResponse(result, headers=_version_headers(config_path))

    @app.put("/api/personas/{role}")
    async def update_persona(role: str, request: Request) -> JSONResponse:
        """Update persona instructions for a role. Returns 404 for unknown roles, 400 for oversized instructions."""
        import yaml

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


        refusal = _version_refusal(config_path, request.headers.get("If-Match"))
        if refusal is not None:
            return refusal
        try:
            save_persona(role, instructions, config_path)
        except (yaml.YAMLError, UnicodeDecodeError) as exc:
            # 158 (#241), sixth review round: save_persona begins with
            # `yaml.safe_load(config_path.read_text())`, so a config file that is not
            # valid YAML or not valid UTF-8 raises out of it. Neither ValueError nor
            # OSError below is a superclass of YAMLError, so that left the handler as
            # a 500 with a traceback -- while the symphony write, which wraps its
            # persist in `except Exception`, answered the same broken file with a
            # structured refusal. UnicodeDecodeError *is* a ValueError, and so was
            # being reported as a 400: a file corrupt on disk is not a bad request.
            from coordinare.services.config_write_service import safe_failure_reason

            _log.warning("persona_write_failed_unreadable", role=role, error=str(exc))
            return JSONResponse(
                {"error": f"Failed to read config: {safe_failure_reason(exc)}"},
                status_code=500,
            )
        except ValueError as exc:
            # ValueError here means malformed config shape (role/length already
            # validated above); treat as validation error per API contract. Logged
            # because the body is deliberately terse and an operator debugging a 400
            # otherwise has nothing.
            _log.warning("persona_write_rejected", role=role, error=str(exc))
            return JSONResponse({"error": str(exc)}, status_code=400)
        except OSError as exc:
            # Not the exception itself: str(OSError) carries the absolute path.
            from coordinare.services.config_write_service import safe_failure_reason

            _log.warning("persona_write_failed", error=str(exc))
            return JSONResponse(
                {"error": f"Failed to write config: {safe_failure_reason(exc)}"},
                status_code=500,
            )

        # Re-read from config to return what's actually stored/effective.
        from coordinare.services.persona_service import get_effective_instructions, load_personas_hot

        personas = load_personas_hot(config_path, daemon.state.get("config"))
        effective = get_effective_instructions(role, personas)
        is_default = not getattr(personas, role).instructions.strip()
        return JSONResponse(
            {"role": role, "instructions": effective, "is_default": is_default},
            headers=_version_headers(config_path),
        )

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
    async def reset_persona_endpoint(role: str, request: Request) -> Response:
        """Reset a role's persona to built-in defaults (clears custom instructions)."""
        import yaml

        from coordinare.services.persona_service import VALID_ROLES, reset_persona

        if role not in VALID_ROLES:
            return JSONResponse({"error": f"Unknown role: {role!r}"}, status_code=404)

        if config_path is None or not config_path.is_file():
            return JSONResponse({"error": "Config file not found"}, status_code=500)


        refusal = _version_refusal(config_path, request.headers.get("If-Match"))
        if refusal is not None:
            return refusal
        try:
            reset_persona(role, config_path)
        except (yaml.YAMLError, UnicodeDecodeError) as exc:
            # The same hole as the save above, for the same reason: reset_persona
            # reads and re-dumps the same file.
            from coordinare.services.config_write_service import safe_failure_reason

            _log.warning("persona_reset_failed_unreadable", role=role, error=str(exc))
            return JSONResponse(
                {"error": f"Failed to read config: {safe_failure_reason(exc)}"},
                status_code=500,
            )
        except ValueError as exc:
            _log.warning("persona_reset_rejected", role=role, error=str(exc))
            return JSONResponse({"error": str(exc)}, status_code=400)
        except OSError as exc:
            # Not the exception itself: str(OSError) carries the absolute path.
            from coordinare.services.config_write_service import safe_failure_reason

            _log.warning("persona_write_failed", error=str(exc))
            return JSONResponse(
                {"error": f"Failed to write config: {safe_failure_reason(exc)}"},
                status_code=500,
            )

        return Response(status_code=204, headers=_version_headers(config_path))

    # ---- 155 (#202): the config assistant --------------------------------------
    # Registered only when enabled, so a disabled deployment does not merely refuse
    # these routes -- it does not have them (FR-013/FR-014). "Off" should mean the
    # dashboard is the dashboard it was before the feature existed.
    # Captured once, and used for BOTH the routes and the page. Routes cannot be
    # added to a running app, so a per-request page check would drift from them: a
    # config reload flipping the flag would serve a panel whose endpoints do not
    # exist, or hide a panel whose endpoints do. One value means the two can never
    # disagree — at the cost of a restart to change it, which is the honest trade.
    _assistant_on = _config_assistant_enabled(daemon)

    if _assistant_on:

        @app.get("/api/assistant/status")
        async def assistant_status() -> JSONResponse:
            """Whether the panel can be used, and if not, why."""
            from coordinare.services.config_assistant import opening_guidance

            backend = daemon.state.get("conducting_backend")
            cfg = daemon.state.get("coordinare_config")
            return JSONResponse(
                {
                    "enabled": True,
                    "ready": backend is not None,
                    "reason": None if backend is not None else "no conducting backend configured",
                    # 155: the panel opens on whatever is actually missing rather than
                    # on a blank prompt, which is the same problem as the YAML.
                    "opening": opening_guidance(cfg) if cfg is not None else "",
                },
            )

        @app.post("/api/assistant/message")
        async def assistant_message(request: Request) -> JSONResponse:
            """One turn. The conversation lives in the client, so nothing is stored.

            Session-scoped memory (FR-015) is structural here rather than a policy
            about a cache: the server keeps nothing between requests, so there is
            nothing to persist, expire, or leak into a later conversation.
            """
            from coordinare.services.config_assistant import run_turn

            try:
                body = await request.json()
            except Exception:
                return JSONResponse({"error": "Invalid JSON body"}, status_code=400)
            if not isinstance(body, dict):
                return JSONResponse({"error": "Request body must be a JSON object"}, status_code=400)

            message = body.get("message")
            if not isinstance(message, str) or not message.strip():
                return JSONResponse({"error": "message is required"}, status_code=400)

            history = body.get("history")
            if not isinstance(history, list):
                history = []

            coordinare_cfg = daemon.state.get("coordinare_config")
            if coordinare_cfg is None:
                return JSONResponse({"error": "configuration is not loaded"}, status_code=503)

            turn = await run_turn(
                message=message,
                history=[h for h in history if isinstance(h, dict)][-20:],
                config=coordinare_cfg,
                backend=daemon.state.get("conducting_backend"),
            )

            proposal = None
            if turn.proposal is not None:
                proposal = {
                    "section": turn.proposal.section,
                    "values": turn.proposal.values,
                    "reason": turn.proposal.reason,
                }

            return JSONResponse(
                {"reply": turn.reply, "proposal": proposal, "error": turn.error},
            )

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
        secret.encode(), body, hashlib.sha256,
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
