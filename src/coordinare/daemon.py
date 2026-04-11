from __future__ import annotations

import asyncio
import signal
from datetime import UTC, datetime
from time import monotonic, perf_counter
from typing import TYPE_CHECKING, Any
from uuid import uuid4

import structlog

from coordinare.graph.state import CoordinareState, initial_state
from coordinare.lib.runtime_events import build_runtime_event
from coordinare.metrics import METRICS
from coordinare.observability import HEALTH, HealthStatus, bind_cycle_id, clear_cycle_id
from coordinare.resilience import CircuitOpenError
from coordinare.session import session_to_state, state_to_session
from coordinare.state_store import StateLoadError, WorkflowPhase, WorkflowSnapshot

if TYPE_CHECKING:
    from coordinare.dashboard import DashboardStore
    from coordinare.state_store import StateStore

logger = structlog.get_logger(__name__)

# Maps (previous_phase, current_phase) tuples to canonical metric transition labels.
# Transitions not listed here are not recorded (e.g. recovery→idle, relay_feedback→*).
_PHASE_TRANSITION_METRIC: dict[tuple[str, str], str] = {
    ("idle", "dispatching"): "idle_to_dispatch",
    ("dispatching", "monitoring_agent"): "dispatch_to_monitor",
    ("dispatching", "monitoring_pr"): "dispatch_to_monitor",
    ("monitoring_agent", "merging"): "monitor_to_merge",
    ("monitoring_pr", "merging"): "monitor_to_merge",
    ("monitoring_agent", "blocked"): "monitor_to_blocked",
    ("monitoring_pr", "blocked"): "monitor_to_blocked",
    ("blocked", "idle"): "blocked_to_idle",
}

# Maps circuit-breaker service names to the HEALTH subsystem they represent.
# Services not listed here are not registered as health probes and are skipped.
# When the circuit is open and poll=0 (webhook-only mode), the daemon cannot
# rely on _wait_for_next_cycle() for recovery — no webhook will arrive if
# GitHub is down. Use a fixed backoff so the circuit can probe-recover.
_CIRCUIT_OPEN_BACKOFF_SECONDS: int = 60

_CIRCUIT_TO_HEALTH_SUBSYSTEM: dict[str, str] = {
    "github": "github",
    "agent": "agent",
    "agent_ssh": "agent",
}


class RuntimeExecutionError(RuntimeError):
    def __init__(self, *, phase: str, step: str, cause: Exception) -> None:
        super().__init__(f"{phase} failure in {step}: {cause}")
        self.phase = phase
        self.step = step
        self.cause = cause


class CoordinareDaemon:
    def __init__(
        self,
        graph: Any,
        *,
        run_mode: str = "shell",
        poll_interval_seconds: int = 30,
        heartbeat_interval_seconds: int = 30,
        max_cycles: int | None = None,
        sleep_func: Any = asyncio.sleep,
        state_store: StateStore | None = None,
        idle_threshold_seconds: int = 1800,
        dashboard_store: DashboardStore | None = None,
        webhook_trigger: asyncio.Event | None = None,
    ) -> None:
        self._graph = graph
        self._run_mode = run_mode
        self._poll_interval_seconds = poll_interval_seconds
        self._heartbeat_interval_seconds = heartbeat_interval_seconds
        self._max_cycles = max_cycles
        self._sleep = sleep_func
        self._running = False
        self._stop_event = asyncio.Event()
        self._webhook_trigger: asyncio.Event = webhook_trigger or asyncio.Event()
        self._state: CoordinareState = initial_state()
        self._cycle_active = False
        self._stop_during_cycle = False
        self._state_store = state_store
        self._idle_threshold_seconds = idle_threshold_seconds
        self._dashboard_store = dashboard_store
        self._main_task: asyncio.Task[None] | None = None

    @property
    def running(self) -> bool:
        return self._running

    @property
    def state(self) -> CoordinareState:
        return self._state

    @property
    def state_store(self) -> StateStore | None:
        return self._state_store

    def _build_snapshot(self) -> WorkflowSnapshot:
        card = self._state.get("current_card")
        card_dict = card if isinstance(card, dict) else {}
        dispatch = self._state.get("agent_dispatch")
        dispatch_dict = dispatch if isinstance(dispatch, dict) else {}
        raw_questions = self._state.get("open_questions")
        questions = [str(q) for q in raw_questions] if isinstance(raw_questions, list) else []
        raw_clarifications = self._state.get("card_clarifications")
        clarifications = list(raw_clarifications) if isinstance(raw_clarifications, list) else []
        last_notified = self._state.get("last_blocked_notified_at")
        return WorkflowSnapshot(
            snapshot_at=datetime.now(UTC),
            phase=self._state.get("phase", "idle"),
            active_card_id=str(card_dict.get("id", "")) or None if card_dict else None,
            active_card_title=str(card_dict.get("title", "")) or None if card_dict else None,
            active_card_column=str(card_dict.get("status", "")) or None if card_dict else None,
            active_card_issue_id=str(card_dict.get("issue_id", "")) or None if card_dict else None,
            pr_url=str(card_dict.get("pr_url", "")) or None if card_dict else None,
            pr_node_id=str(card_dict.get("pr_node_id", "")) or None if card_dict else None,
            agent_session_id=str(dispatch_dict.get("session_id", "")) or None if dispatch_dict else None,
            open_questions=questions,
            card_clarifications=clarifications,
            last_blocked_notified_at=last_notified if isinstance(last_notified, datetime) else None,
            lifecycle_completed_at=self._state.get("lifecycle_completed_at") if isinstance(self._state.get("lifecycle_completed_at"), datetime) else None,
        )

    def _restore_from_snapshot(self, snapshot: WorkflowSnapshot) -> None:
        self._state["phase"] = snapshot.phase
        self._state["open_questions"] = list(snapshot.open_questions)
        self._state["card_clarifications"] = list(snapshot.card_clarifications)
        self._state["last_blocked_notified_at"] = snapshot.last_blocked_notified_at
        self._state["lifecycle_completed_at"] = snapshot.lifecycle_completed_at
        if snapshot.active_card_id:
            self._state["current_card"] = {
                "id": snapshot.active_card_id,
                "issue_id": snapshot.active_card_issue_id or "",
                "title": snapshot.active_card_title or "",
                "status": snapshot.active_card_column or "",
                "pr_url": snapshot.pr_url,
                "pr_node_id": snapshot.pr_node_id,
            }
        if snapshot.agent_session_id:
            self._state["agent_dispatch"] = {"session_id": snapshot.agent_session_id}

    @staticmethod
    def _infer_phase_from_board_column(column: str) -> WorkflowPhase:
        normalized = column.strip().lower()
        if normalized in {"in progress", "in_progress"}:
            return "monitoring_agent"
        if normalized in {"in review", "in_review"}:
            return "monitoring_pr"
        if normalized == "blocked":
            return "blocked"
        return "idle"

    async def _reconcile_with_board(self, snapshot: WorkflowSnapshot) -> None:
        """Query the live board and reconcile restored state against it."""
        github = self._state.get("github_service")
        if github is None:
            return
        try:
            board = await github.poll_board()
            board_snapshot = board.get("snapshot", {})
            found_column: str | None = None
            for column, card_ids in board_snapshot.items():
                if isinstance(card_ids, list) and snapshot.active_card_id in card_ids:
                    found_column = column
                    break

            if found_column is None or found_column.upper() == "DONE":
                logger.warning(
                    "board_contradicts_snapshot",
                    active_card_id=snapshot.active_card_id,
                    found_column=found_column,
                )
                self._state["phase"] = "idle"
                self._state["current_card"] = None
            else:
                inferred = self._infer_phase_from_board_column(found_column)
                if inferred != snapshot.phase:
                    logger.info(
                        "board_reconciliation_advanced",
                        active_card_id=snapshot.active_card_id,
                        snapshot_phase=snapshot.phase,
                        board_column=found_column,
                        inferred_phase=inferred,
                    )
                    self._state["phase"] = inferred
                else:
                    logger.info(
                        "board_reconciliation_confirmed",
                        active_card_id=snapshot.active_card_id,
                        phase=snapshot.phase,
                    )
        except Exception as exc:
            logger.warning(
                "board_reconciliation_skipped",
                error=str(exc),
            )

    async def _wait_for_next_cycle(self) -> None:
        """Wait for the next polling cycle, honouring webhook triggers and poll=0 mode."""
        poll = self._poll_interval_seconds
        if poll > 0:
            # Race the poll sleep against a webhook trigger so either can wake the loop.
            # Using self._sleep makes this injectable/mockable in tests.
            sleep_task = asyncio.ensure_future(self._sleep(poll))
            trigger_task = asyncio.ensure_future(self._webhook_trigger.wait())
            done, pending = await asyncio.wait(
                {sleep_task, trigger_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            # Only consume the trigger if it actually fired; a webhook arriving
            # just as the sleep expires should not be silently discarded.
            if trigger_task in done:
                self._webhook_trigger.clear()
        else:
            # Polling disabled — block until a webhook trigger or stop event fires
            trigger_task = asyncio.ensure_future(self._webhook_trigger.wait())
            stop_task = asyncio.ensure_future(self._stop_event.wait())
            done, pending = await asyncio.wait(
                {trigger_task, stop_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            if trigger_task in done:
                self._webhook_trigger.clear()

    def stop(self) -> None:
        if self._cycle_active:
            self._stop_during_cycle = True
        self._running = False
        self._stop_event.set()
        # Cancel the in-progress cycle only when stop() is called from outside
        # start() — i.e. signal handlers or external code.  When called from
        # within start() itself (max_cycles, mid-cycle graph callbacks) the
        # existing break/stop-event logic handles the exit and we must not
        # self-cancel, which would propagate CancelledError to the caller.
        if (
            self._main_task is not None
            and not self._main_task.done()
            and asyncio.current_task() != self._main_task
        ):
            self._main_task.cancel()

    def _max_concurrent_cards(self) -> int:
        """Return the configured concurrency limit (defaults to 1)."""
        config = self._state.get("config")
        if config is not None and hasattr(config, "max_concurrent_cards"):
            return max(1, int(config.max_concurrent_cards))
        return 1

    async def _invoke_multi_session(self) -> None:
        """Process each active session through the graph independently.

        Called only when max_concurrent_cards > 1 and there are active
        sessions.  Each session's fields are copied into the flat state,
        the graph is invoked, and the resulting state is copied back into
        the session.  Completed sessions (phase=idle, current_card=None)
        are removed to free capacity.

        On error, session-scoped fields are restored from a pre-invocation
        snapshot.  Non-session fields (board_snapshot, phase, caches) are
        re-derived each cycle so transient leaks are self-correcting.
        """
        active_sessions: dict = self._state.get("active_sessions") or {}

        # Always clear board cache at cycle start so check_board re-polls
        self._state["_board_cache"] = None

        if not active_sessions:
            # No sessions yet — run one graph cycle to let check_board populate them
            self._state = await self._graph.ainvoke(self._state)
            return

        # Cache was already cleared above; the first session's check_board
        # will re-poll GitHub exactly once; subsequent sessions within
        # this cycle reuse the cached result and skip the API call.

        completed_ids: list[str] = []
        for card_id, session in list(active_sessions.items()):
            # Save the pre-invocation session fields so we can restore
            # on error without deep-copying service objects.
            pre_session = dict(session)

            # Copy session → flat state
            session_to_state(session, self._state)
            try:
                self._state = await self._graph.ainvoke(self._state)
            except Exception:
                logger.error(
                    "session_graph_error",
                    card_id=card_id,
                    exc_info=True,
                )
                # Restore the original session fields so the partial
                # mutation doesn't leak; the session can be retried next cycle.
                session_to_state(pre_session, self._state)
                active_sessions[card_id] = pre_session
                continue

            # Copy flat state → session
            updated_session = state_to_session(self._state)
            active_sessions[card_id] = updated_session

            # Check if session completed
            session_phase = updated_session.get("phase", "idle")
            session_card = updated_session.get("current_card")
            if session_phase == "idle" and session_card is None:
                completed_ids.append(card_id)

        # Remove completed sessions
        for card_id in completed_ids:
            del active_sessions[card_id]
            logger.info("session_completed", card_id=card_id)

        self._state["active_sessions"] = active_sessions

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                loop.add_signal_handler(sig, self.stop)
            except NotImplementedError:
                return

    def _emit(self, **event: Any) -> None:
        category = event.get("category", "activity")
        if category == "failure":
            logger.error("runtime_event", **event)
        elif category in {"heartbeat", "activity"}:
            logger.debug("runtime_event", **event)
        else:
            logger.info("runtime_event", **event)

    async def start(self) -> None:
        self._main_task = asyncio.current_task()
        self._running = True
        self._install_signal_handlers()
        last_heartbeat = monotonic()
        cycle_count = 0

        # T018: Startup recovery — load persisted state before poll loop
        if self._state_store is not None:
            try:
                snapshot = await self._state_store.load()
                if snapshot is not None:
                    self._restore_from_snapshot(snapshot)
                    self._emit(
                        **build_runtime_event(
                            category="startup",
                            message="prior state loaded",
                            phase=snapshot.phase,
                            active_card_id=snapshot.active_card_id,
                        )
                    )
                    # T020: Board reconciliation after restore
                    if snapshot.active_card_id:
                        await self._reconcile_with_board(snapshot)
                else:
                    self._emit(
                        **build_runtime_event(
                            category="startup",
                            message="no prior state found",
                        )
                    )
            except StateLoadError as exc:
                self._emit(
                    **build_runtime_event(
                        category="warning",
                        message="state load failed",
                        reason=exc.reason,
                        detail=exc.detail,
                    )
                )
                # Fresh start — self._state already initialised by initial_state()

        previous_phase = self._state.get("phase")
        self._emit(
            **build_runtime_event(
                category="startup",
                message="daemon startup complete",
                run_mode=self._run_mode,
                poll_interval_seconds=self._poll_interval_seconds,
            )
        )

        # T018: Dispatch daemon_restart notification
        notification_service = self._state.get("notification_service")
        if notification_service is not None:
            from coordinare.models.notification import (
                EventType,
                NotificationEvent,
                NotificationSeverity,
            )

            try:
                await notification_service.dispatch(
                    NotificationEvent(
                        event_type=EventType.daemon_restart,
                        severity=NotificationSeverity.info,
                        source="daemon",
                        payload={
                            "event_type": "daemon_restart",
                            "severity": "info",
                            "source": "daemon",
                            "run_mode": self._run_mode,
                            "summary": f"🔄 Coordinare restarted (mode: {self._run_mode})",
                        },
                    )
                )
            except Exception as exc:
                logger.warning("daemon_restart_notification_failed", error=str(exc))

        if self._poll_interval_seconds == 0:
            logger.info("polling_disabled")

        # T019: Track prolonged idle
        last_activity_at = monotonic()

        failure: RuntimeExecutionError | None = None
        while self._running and not self._stop_event.is_set():
            try:
                self._cycle_active = True
                # US2: bind a unique cycle_id for log correlation
                cycle_id = str(uuid4())
                bind_cycle_id(cycle_id)
                _cycle_t0 = perf_counter()

                # 035: Multi-card parallelism — when concurrency > 1,
                # iterate over active sessions independently.
                if self._max_concurrent_cards() > 1:
                    await self._invoke_multi_session()
                else:
                    self._state = await self._graph.ainvoke(self._state)

                # US1: record cycle metrics
                _cycle_elapsed = perf_counter() - _cycle_t0
                METRICS.cycles_completed_total.inc()
                METRICS.cycle_duration_seconds.observe(_cycle_elapsed)
                # 035: Update active session gauge
                _active = self._state.get("active_sessions") or {}
                METRICS.active_sessions.set(len(_active))
                # US3: mark external service subsystems healthy after a successful poll cycle
                HEALTH.update("github", HealthStatus.healthy)
                HEALTH.update("agent", HealthStatus.healthy)
                # config and notifications don't change mid-run; refresh timestamps
                # so the stale-detection window doesn't expire between cycles.
                HEALTH.update("config", HealthStatus.healthy)
                if self._state.get("notification_service") is not None:
                    HEALTH.update("notifications", HealthStatus.healthy)

                self._cycle_active = False
                cycle_count += 1
                self._state["error_count"] = 0

                # Dashboard: record cycle and broadcast updated snapshot to all open tabs
                if self._dashboard_store is not None:
                    _current_phase = str(self._state.get("phase", "idle"))
                    self._dashboard_store.record_cycle(
                        duration_seconds=_cycle_elapsed,
                        phase=_current_phase,
                        outcome="success",
                    )
                    _snapshot = self._dashboard_store.build_snapshot(self, METRICS, HEALTH)
                    self._dashboard_store.broadcaster.broadcast(_snapshot)
                self._emit(
                    **build_runtime_event(
                        category="activity",
                        message="processing cycle completed",
                        cycle=cycle_count,
                        phase=self._state.get("phase", "unknown"),
                    )
                )
                current_phase = self._state.get("phase")
                if current_phase != previous_phase:
                    self._emit(
                        **build_runtime_event(
                            category="state_change",
                            message="state transition detected",
                            previous_phase=previous_phase,
                            current_phase=current_phase,
                        )
                    )
                    # US1: record phase transition metric before updating previous_phase;
                    # only canonical transitions defined in _PHASE_TRANSITION_METRIC are recorded.
                    _transition_label = _PHASE_TRANSITION_METRIC.get(
                        (str(previous_phase), str(current_phase))
                    )
                    if _transition_label is not None:
                        METRICS.card_state_transitions_total.labels(
                            transition_type=_transition_label,
                        ).inc()
                    previous_phase = current_phase
                    # 028: Track when the phase was entered
                    self._state["phase_entered_at"] = datetime.now(UTC)
                    # T021: Persist snapshot on every phase transition
                    if self._state_store is not None:
                        await self._state_store.save(self._build_snapshot())

                # T019: Prolonged idle detection
                current_phase = self._state.get("phase")
                if current_phase != "idle":
                    last_activity_at = monotonic()
                elif notification_service is not None:
                    idle_seconds = monotonic() - last_activity_at
                    if idle_seconds >= self._idle_threshold_seconds:
                        from coordinare.models.notification import (
                            EventType,
                            NotificationEvent,
                            NotificationSeverity,
                        )

                        try:
                            await notification_service.dispatch(
                                NotificationEvent(
                                    event_type=EventType.prolonged_idle,
                                    severity=NotificationSeverity.warning,
                                    source="daemon",
                                    payload={
                                        "event_type": "prolonged_idle",
                                        "severity": "warning",
                                        "source": "daemon",
                                        "idle_seconds": str(int(idle_seconds)),
                                        "summary": f"💤 Coordinare has been idle for {int(idle_seconds // 60)} minutes — no cards to process",
                                    },
                                    dedup_key="prolonged_idle",
                                )
                            )
                        except Exception as exc:
                            logger.warning("prolonged_idle_notification_failed", error=str(exc))

                # 028: Stuck card detection
                _stuck_phase = self._state.get("phase")
                _stuck_excluded = {"idle", "system_error"}
                if _stuck_phase and _stuck_phase not in _stuck_excluded and notification_service is not None:
                    _config = self._state.get("config")
                    _phase_entered = self._state.get("phase_entered_at")
                    if _config is not None and _phase_entered is not None and hasattr(_config, "stuck_alerts"):
                        _stuck_cfg = _config.stuck_alerts
                        _threshold = _stuck_cfg.per_phase_thresholds.get(_stuck_phase, _stuck_cfg.threshold_seconds)
                        if _threshold > 0:
                            _elapsed = (datetime.now(UTC) - _phase_entered).total_seconds()
                            if _elapsed > _threshold:
                                from coordinare.models.notification import (
                                    EventType,
                                    NotificationEvent,
                                    NotificationSeverity,
                                )
                                _card = self._state.get("current_card") or {}
                                _card_title = str(_card.get("title", ""))[:50]
                                _card_num = _card.get("issue_number", "")
                                _card_ref = f"#{_card_num} " if _card_num else ""
                                _summary = f"⏰ {_card_ref}{_card_title} — stuck in {_stuck_phase} for {round(_elapsed // 60)} min"
                                try:
                                    await notification_service.dispatch(
                                        NotificationEvent(
                                            event_type=EventType.card_stuck,
                                            severity=NotificationSeverity.warning,
                                            payload={
                                                "phase": _stuck_phase,
                                                "elapsed_seconds": str(round(_elapsed)),
                                                "threshold_seconds": str(_threshold),
                                                "card_title": str(_card.get("title", "")),
                                                "card_id": str(_card.get("id", "")),
                                                "summary": _summary,
                                            },
                                            source="daemon",
                                            dedup_key=f"stuck:{_card.get('id', '')}:{_stuck_phase}",
                                        )
                                    )
                                except Exception as _exc:
                                    logger.warning("stuck_card_notification_failed", error=str(_exc))

                now = monotonic()
                if now - last_heartbeat >= self._heartbeat_interval_seconds:
                    self._emit(
                        **build_runtime_event(
                            category="heartbeat",
                            message="daemon heartbeat",
                            cycle=cycle_count,
                            phase=self._state.get("phase", "unknown"),
                        )
                    )
                    last_heartbeat = now

                if self._max_cycles is not None and cycle_count >= self._max_cycles:
                    self.stop()
                    clear_cycle_id()
                    break
                await self._wait_for_next_cycle()
            except asyncio.CancelledError:
                self._cycle_active = False
                clear_cycle_id()
                break  # exit loop cleanly so shutdown log can emit
            except CircuitOpenError as exc:
                self._cycle_active = False
                METRICS.service_calls_total.labels(
                    service=exc.service_name, action="call_blocked", outcome="circuit_open",
                ).inc()
                logger.warning(
                    "circuit_open.call_skipped",
                    service=exc.service_name,
                )
                # Mark the isolated service degraded so /ready reflects the circuit state.
                # Use the mapping to translate circuit service names to health subsystem names.
                _health_subsystem = _CIRCUIT_TO_HEALTH_SUBSYSTEM.get(exc.service_name)
                if _health_subsystem is not None:
                    HEALTH.update(
                        _health_subsystem,
                        HealthStatus.degraded,
                        details=f"circuit open: {exc.service_name}",
                    )
                # Do NOT set self._running = False — continue the poll loop
                clear_cycle_id()
                # Use a dedicated backoff rather than _wait_for_next_cycle():
                # in poll=0 (webhook-only) mode, _wait_for_next_cycle blocks
                # until a webhook fires — but if GitHub is down the circuit is
                # open AND no webhooks arrive, causing an indefinite hang.
                # This backoff always makes forward progress and respects stop().
                _backoff = (
                    self._poll_interval_seconds
                    if self._poll_interval_seconds > 0
                    else _CIRCUIT_OPEN_BACKOFF_SECONDS
                )
                _sleep_t = asyncio.ensure_future(self._sleep(_backoff))
                _stop_t = asyncio.ensure_future(self._stop_event.wait())
                try:
                    _cb_done, _cb_pending = await asyncio.wait(
                        {_sleep_t, _stop_t}, return_when=asyncio.FIRST_COMPLETED
                    )
                    for _t in _cb_pending:
                        _t.cancel()
                except asyncio.CancelledError:
                    _sleep_t.cancel()
                    _stop_t.cancel()
                    break  # treat external cancellation as stop signal
            except Exception as exc:
                self._cycle_active = False
                self._state["error_count"] = self._state.get("error_count", 0) + 1
                self._emit(
                    **build_runtime_event(
                        category="failure",
                        message="runtime processing cycle failed",
                        error=str(exc),
                        failing_step="cycle_execution",
                        error_count=self._state["error_count"],
                    )
                )
                previous_phase = self._state.get("phase", "unknown")
                self._state["phase"] = "recovery"
                self._emit(
                    **build_runtime_event(
                        category="state_change",
                        message="state transition detected",
                        previous_phase=previous_phase,
                        current_phase="recovery",
                    )
                )
                failure = RuntimeExecutionError(phase="runtime", step="cycle_execution", cause=exc)
                # Dashboard: record error cycle and broadcast
                if self._dashboard_store is not None:
                    _err_phase = str(self._state.get("phase", "recovery"))
                    self._dashboard_store.record_cycle(
                        duration_seconds=perf_counter() - _cycle_t0,
                        phase=_err_phase,
                        outcome="error",
                    )
                    _err_snapshot = self._dashboard_store.build_snapshot(self, METRICS, HEALTH)
                    self._dashboard_store.broadcaster.broadcast(_err_snapshot)
                self._running = False
                clear_cycle_id()
            else:
                # Happy-path cycle end — clear cycle_id before inter-cycle sleep
                clear_cycle_id()

        self._emit(
            **build_runtime_event(
                category="shutdown",
                message="daemon stopped",
                graceful=failure is None,
                cycle_interrupted=self._stop_during_cycle,
                run_mode=self._run_mode,
            )
        )
        if failure is not None:
            raise failure
