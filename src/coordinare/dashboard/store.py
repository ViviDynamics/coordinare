"""Dashboard state store: history, watcher and snapshot building (436)."""
from __future__ import annotations

import asyncio
import contextlib
import copy
import json
from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from coordinare.dashboard.helpers import (
    _json_default,
    compute_overall_health,
    format_phase_label,
    ownership_hint,
)
from coordinare.dashboard.sse import SSEBroadcaster
from coordinare.services.activity_log import ActivityLog
from coordinare.services.observer import OBSERVER_RECENT_VERDICTS

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

    from coordinare.daemon import CoordinareDaemon
    from coordinare.metrics import CoordinareMetrics
    from coordinare.observability import HealthRegistry

SESSION_EVENT_LIMIT = 40


@dataclass
class _PerfTelemetry:
    """Live performer telemetry threaded through the snapshot builders."""

    events: list[Any]
    metrics: Any
    backend: str | None
    logs: list[str]


@dataclass
class _SnapCtx:
    """Shared top-state context threaded through the snapshot builders."""

    daemon: CoordinareDaemon
    snapshot: Any
    phase: str
    card_dict: dict[str, Any]


class DashboardStore:
    """In-process state for the dashboard: cycle history + SSE broadcaster.

    One instance is created at daemon startup and passed to both
    create_dashboard_app() and CoordinareDaemon (as dashboard_store).
    """

    def __init__(self) -> None:
        self.broadcaster = SSEBroadcaster()
        # Rolling in-memory log (FR-009); deque drops oldest automatically
        self.history: deque[dict[str, Any]] = deque(maxlen=20)
        # Most recent cycle duration — stored here rather than reading prometheus internals
        self.last_cycle_duration: float | None = None
        # 065 US1: mid-cycle active_sessions watcher. The daemon only broadcasts
        # at cycle end (daemon.py:1655), so mutations made by in-cycle code
        # (kickbacks, stage advances, new dispatches) would otherwise be
        # invisible to SSE subscribers for up to a full cycle. A single shared
        # poll-and-fingerprint task started on first subscribe makes any
        # mutation observable within ~100 ms without requiring every call site
        # to notify explicitly.
        self._watcher_task: asyncio.Task[None] | None = None
        self._watcher_fingerprint: tuple[Any, ...] | None = None
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
    def _active_sessions_fingerprint(daemon: CoordinareDaemon) -> tuple[Any, ...]:
        """Identity-changing fields for each active session.

        Compared between watcher ticks to decide whether a broadcast is needed.
        Includes the fields the Active Performers panel renders (title, stage,
        phase, urls) plus dispatch identity (container_id) so a re-dispatch
        of the same card_id still trips a broadcast.
        """
        active = daemon.state.get("active_sessions") or {}
        parts: list[tuple[Any, ...]] = []
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
        previous: tuple[Any, ...] | None,
        current: tuple[Any, ...],
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
    ) -> dict[str, Any]:
        """Assemble the full DashboardState dict for an SSE state_update event."""
        # --- Phase / card / agent from persisted snapshot or live state ---
        snapshot = (
            daemon.state_store.last_snapshot
            if daemon.state_store is not None
            else None
        )
        phase = str(snapshot.phase if snapshot else daemon.state.get("phase", "idle"))
        subsystems = self._subsystem_rows(health)
        cycles_completed, started_at = self._metric_rows(metrics)
        error_count = int(daemon.state.get("error_count", 0))
        card = daemon.state.get("current_card")
        card_dict = card if isinstance(card, dict) else {}
        performer_telemetry = self._performer_state(daemon)
        board_summary, last_poll_at = self._board_and_poll(daemon)
        snapshot_ctx = _SnapCtx(
            daemon=daemon, snapshot=snapshot, phase=phase, card_dict=card_dict,
        )
        active_session_summaries = self._session_summaries(
            snapshot_ctx, performer_telemetry,
        )
        core = self._snapshot_core(
            snapshot_ctx, performer_telemetry,
            len(active_session_summaries), active_session_summaries, subsystems,
        )
        extras = self._snapshot_extras(
            daemon, board_summary, last_poll_at, (cycles_completed, started_at), error_count,
        )
        return {**core, **extras}

    @staticmethod
    def _subsystem_rows(health: HealthRegistry) -> list[dict[str, Any]]:
        """Flatten the HealthRegistry report into dashboard-ready rows."""
        health_report = health.snapshot()
        return [
            {
                "name": p.subsystem_name,
                "status": p.status.value,
                "required": p.is_required,
                "checked_at": p.checked_at.isoformat(),
                "details": p.details,
            }
            for p in health_report.probes
        ]

    @staticmethod
    def _metric_rows(metrics: CoordinareMetrics) -> tuple[int, str | None]:
        """Cycle counter and daemon start time from the metrics registry."""
        # Read prometheus counter value via the internal _value; this is stable
        # across prometheus-client 0.x for single-process use.
        try:
            cycles_completed = int(metrics.cycles_completed_total._value.get())
        except Exception:
            cycles_completed = 0

        # daemon start time stored in build_info label at startup
        try:
            build_info = metrics.build_info.labels()._value.get()  # type: ignore[call-overload]
            started_at: str | None = (
                build_info.get("started_at") if isinstance(build_info, dict) else None
            )
        except Exception:
            started_at = None
        return cycles_completed, started_at

    @staticmethod
    def _performer_state(daemon: CoordinareDaemon) -> _PerfTelemetry:
        """Live performer events, metrics, backend and drained stderr logs."""
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
        return _PerfTelemetry(
            events=performer_events,
            metrics=performer_metrics,
            backend=performer_backend,
            logs=performer_logs,
        )

    def _board_and_poll(
        self, daemon: CoordinareDaemon,
    ) -> tuple[dict[str, int], str | None]:
        """Board summary and last-poll timestamp for the idle-state panel."""
        # Aggregate board snapshots: top-level first, then symphony states as fallback.
        # When coordinare runs in symphony mode it has no top-level board of its own,
        # so we must fold symphony board_snapshots together to get accurate counts.
        _top_board = daemon.state.get("board_snapshot")
        if not _top_board:
            _agg_board: dict[str, list[Any]] = {}
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
        return board_summary, last_poll_at

    def _session_summaries(
        self,
        ctx: _SnapCtx,
        perf: _PerfTelemetry,
    ) -> list[dict[str, Any]]:
        """Summaries for every live session; synthesized from top state when solo."""
        # 035: Multi-card parallelism — active session summaries
        active_sessions_raw = ctx.daemon.state.get("active_sessions") or {}
        summaries = [
            self._session_summary(ctx.daemon, sid, sess)
            for sid, sess in active_sessions_raw.items()
        ]
        # Single-session mode compatibility: when multi-card ``active_sessions``
        # is empty but we're actively monitoring a performer, synthesize one
        # summary row from top-level state so the dashboard doesn't show
        # "No active performers" during in-flight work.
        if not summaries and ctx.phase in {
            "monitoring_agent",
            "monitoring_performer",
            "monitoring_pr",   # performer pushed a PR and is now watching CI/review
            "relay_feedback",
        }:
            summaries.append(
                self._top_state_summary(ctx, perf),
            )
        return summaries

    @staticmethod
    def _session_summary(daemon: CoordinareDaemon, sid: str, sess: dict[str, Any]) -> dict[str, Any]:
        """One summary row for a live session in multi-card mode."""
        sess_card = sess.get("current_card") or {}
        _sess_raw_stage = sess.get("performer_stage")
        _sess_stage = _sess_raw_stage if isinstance(_sess_raw_stage, str) else ""
        _sess_dispatch = sess.get("agent_dispatch_at")
        _sess_agent_dispatch = sess.get("agent_dispatch") or {}
        return {
            # 348: per-session performer telemetry. These fields round-trip
            # through _SESSION_FIELDS, so in shared-pool/multi-symphony mode
            # the session entry is the only place they exist -- the daemon
            # aggregates active_sessions and phase and nothing else, which
            # left every live panel on /performers permanently empty.
            "performer_events": list(sess.get("performer_events") or [])[
                -SESSION_EVENT_LIMIT:
            ],
            # 429: the recent observer verdicts, newest last, for the
            # session view's "why did the coordinare do that" panel.
            "observer_verdicts": list(sess.get("observer_verdicts") or [])[
                -OBSERVER_RECENT_VERDICTS:
            ],
            "performer_metrics": sess.get("performer_metrics"),
            "session_stats": DashboardStore._serialise_session_stats(sess.get("session_stats")),
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
            "performer_logs": DashboardStore._session_performer_logs(daemon, _sess_stage, sid),
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
        }

    @staticmethod
    def _top_state_summary(
        ctx: _SnapCtx,
        perf: _PerfTelemetry,
    ) -> dict[str, Any]:
        """One synthesized summary row from top-level state (single-session mode)."""
        daemon = ctx.daemon
        snapshot = ctx.snapshot
        phase = ctx.phase
        card_dict = ctx.card_dict
        performer_events = perf.events
        performer_metrics = perf.metrics
        performer_backend = perf.backend
        performer_logs = perf.logs
        _top_stage_raw = daemon.state.get("performer_stage")
        _top_stage = _top_stage_raw if isinstance(_top_stage_raw, str) else ""
        _top_dispatch = daemon.state.get("agent_dispatch_at")
        _dispatch = daemon.state.get("agent_dispatch") or {}
        _top_card_id = str(card_dict.get("id") or _dispatch.get("session_id") or "active")
        _top_card_title = str(card_dict.get("title") or (snapshot.active_card_title if snapshot else "") or "")
        return {
            # 348: mirror the per-session telemetry keys from top-level state
            # so the UI can read the session row unconditionally, without a
            # separate single-symphony code path.
            "performer_events": performer_events[-SESSION_EVENT_LIMIT:],
            "performer_metrics": performer_metrics,
            "session_stats": DashboardStore._serialise_session_stats(
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
            # 429: the solo path synthesizes from flat state, where the
            # monitor's observer phase wrote the recent verdicts.
            "observer_verdicts": list(
                daemon.state.get("observer_verdicts") or [],
            )[-OBSERVER_RECENT_VERDICTS:],
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
        }

    def _snapshot_core(
        self,
        ctx: _SnapCtx,
        perf: _PerfTelemetry,
        active_session_count: int,
        active_session_summaries: list[dict[str, Any]],
        subsystems: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """The first half of the snapshot dict: state and performer telemetry."""
        daemon = ctx.daemon
        snapshot = ctx.snapshot
        phase = ctx.phase
        card_dict = ctx.card_dict
        performer_events = perf.events
        performer_metrics = perf.metrics
        performer_backend = perf.backend
        performer_logs = perf.logs
        active_card_issue_url = str(card_dict.get("issue_url", "")) or None
        card_clarifications = self._annotate_clarifications(
            list(snapshot.card_clarifications) if snapshot else [],
            card_dict,
            snapshot,
        )
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
                daemon.state.get("agent_dispatch_at").isoformat()  # type: ignore[union-attr]
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
        }

    def _snapshot_extras(
        self,
        daemon: CoordinareDaemon,
        board_summary: dict[str, int],
        last_poll_at: str | None,
        metric_rows: tuple[int, str | None],
        error_count: int,
    ) -> dict[str, Any]:
        """The second half of the snapshot dict: cycles, board and ownership."""
        cycles_completed, started_at = metric_rows
        return {
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
    def _serialise_session_stats(stats: Any) -> dict[str, Any] | None:
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
    def _build_role_utilization(daemon: Any) -> list[dict[str, Any]]:
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
        return slot_mgr.utilization(queued_by_stage=queued)  # type: ignore[no-any-return]

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
    def _build_symphonies_data(daemon: Any) -> list[dict[str, Any]]:
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
