from __future__ import annotations

import asyncio
import signal
from time import monotonic
from typing import Any

import structlog

from coordinare.graph.state import CoordinareState, initial_state
from coordinare.lib.runtime_events import build_runtime_event

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

    @property
    def running(self) -> bool:
        return self._running

    @property
    def state(self) -> CoordinareState:
        return self._state

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
