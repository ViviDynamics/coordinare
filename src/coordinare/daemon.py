from __future__ import annotations

import asyncio
import signal
from datetime import UTC, datetime
from time import monotonic
from typing import TYPE_CHECKING, Any

import structlog

from coordinare.graph.state import CoordinareState, initial_state
from coordinare.lib.runtime_events import build_runtime_event
from coordinare.resilience import CircuitOpenError
from coordinare.state_store import StateLoadError, WorkflowPhase, WorkflowSnapshot

if TYPE_CHECKING:
    from coordinare.state_store import StateStore

logger = structlog.get_logger(__name__)


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
    ) -> None:
        self._graph = graph
        self._run_mode = run_mode
        self._poll_interval_seconds = poll_interval_seconds
        self._heartbeat_interval_seconds = heartbeat_interval_seconds
        self._max_cycles = max_cycles
        self._sleep = sleep_func
        self._running = False
        self._stop_event = asyncio.Event()
        self._state: CoordinareState = initial_state()
        self._cycle_active = False
        self._stop_during_cycle = False
        self._state_store = state_store

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
        return WorkflowSnapshot(
            snapshot_at=datetime.now(UTC),
            phase=self._state.get("phase", "idle"),
            active_card_id=str(card_dict.get("id", "")) or None if card_dict else None,
            active_card_title=str(card_dict.get("title", "")) or None if card_dict else None,
            active_card_column=str(card_dict.get("status", "")) or None if card_dict else None,
            pr_url=str(card_dict.get("pr_url", "")) or None if card_dict else None,
            pr_node_id=str(card_dict.get("pr_node_id", "")) or None if card_dict else None,
            agent_session_id=str(dispatch_dict.get("session_id", "")) or None if dispatch_dict else None,
            open_questions=questions,
        )

    def _restore_from_snapshot(self, snapshot: WorkflowSnapshot) -> None:
        self._state["phase"] = snapshot.phase
        self._state["open_questions"] = list(snapshot.open_questions)
        if snapshot.active_card_id:
            self._state["current_card"] = {
                "id": snapshot.active_card_id,
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

    def stop(self) -> None:
        if self._cycle_active:
            self._stop_during_cycle = True
        self._running = False
        self._stop_event.set()

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

        failure: RuntimeExecutionError | None = None
        while self._running and not self._stop_event.is_set():
            try:
                self._cycle_active = True
                self._state = await self._graph.ainvoke(self._state)
                self._cycle_active = False
                cycle_count += 1
                self._state["error_count"] = 0
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
                    previous_phase = current_phase
                    # T021: Persist snapshot on every phase transition
                    if self._state_store is not None:
                        await self._state_store.save(self._build_snapshot())

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
                    break
                await self._sleep(self._poll_interval_seconds)
            except asyncio.CancelledError:
                raise
            except CircuitOpenError as exc:
                self._cycle_active = False
                from coordinare.metrics import METRICS

                METRICS.service_calls_total.labels(
                    service=exc.service_name, action="call_blocked", outcome="circuit_open",
                ).inc()
                logger.warning(
                    "circuit_open.call_skipped",
                    service=exc.service_name,
                )
                # Do NOT set self._running = False — continue the poll loop
                await self._sleep(self._poll_interval_seconds)
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
                self._running = False

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
