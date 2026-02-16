from __future__ import annotations

import asyncio
import signal
from typing import Any

import structlog

from coordinare.graph.state import CoordinareState, initial_state

logger = structlog.get_logger(__name__)


class CoordinareDaemon:
    def __init__(
        self,
        graph: Any,
        *,
        poll_interval_seconds: int = 30,
        max_backoff_seconds: int = 300,
        sleep_func: Any = asyncio.sleep,
    ) -> None:
        self._graph = graph
        self._poll_interval_seconds = poll_interval_seconds
        self._max_backoff_seconds = max_backoff_seconds
        self._sleep = sleep_func
        self._running = False
        self._stop_event = asyncio.Event()
        self._state: CoordinareState = initial_state()

    @property
    def running(self) -> bool:
        return self._running

    @property
    def state(self) -> CoordinareState:
        return self._state

    def stop(self) -> None:
        self._running = False
        self._stop_event.set()

    def _install_signal_handlers(self) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self.stop)

    async def start(self) -> None:
        self._running = True
        self._install_signal_handlers()
        error_count = 0

        logger.info("daemon_started", poll_interval_seconds=self._poll_interval_seconds)

        while self._running:
            try:
                self._state = await self._graph.ainvoke(self._state)
                error_count = 0
                self._state["error_count"] = 0
                await self._sleep(self._poll_interval_seconds)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                error_count += 1
                self._state["error_count"] = error_count
                backoff = min(2**error_count, self._max_backoff_seconds)
                logger.exception(
                    "daemon_cycle_failed",
                    error_count=error_count,
                    backoff_seconds=backoff,
                    error=str(exc),
                )
                await self._sleep(backoff)

        logger.info("daemon_stopped")
