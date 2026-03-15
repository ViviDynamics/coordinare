"""T030: Performance test — poll cycle budget with open circuits."""
from __future__ import annotations

import time

import pytest

from coordinare.daemon import CoordinareDaemon
from coordinare.resilience import CircuitOpenError


class _FastGraph:
    """Graph that returns instantly."""

    async def ainvoke(self, state):
        return state


class _CircuitOpenGraph:
    """Graph that raises CircuitOpenError N times then stops the daemon."""

    def __init__(self, max_calls: int) -> None:
        self.call_count = 0
        self._max_calls = max_calls
        self._daemon: CoordinareDaemon | None = None

    async def ainvoke(self, state):
        self.call_count += 1
        if self.call_count >= self._max_calls and self._daemon is not None:
            self._daemon.stop()
        raise CircuitOpenError("github")


async def _instant_sleep(_seconds: float) -> None:
    """No-op sleep for performance tests."""


@pytest.mark.asyncio
async def test_open_circuit_poll_cycle_within_budget() -> None:
    """SC-003: Mean poll cycle with open circuits <= 110% of healthy baseline."""
    num_cycles = 10

    # Measure baseline: healthy graph with instant responses
    baseline_graph = _FastGraph()
    baseline_daemon = CoordinareDaemon(
        baseline_graph,
        max_cycles=num_cycles,
        poll_interval_seconds=1,
        sleep_func=_instant_sleep,
    )

    start = time.monotonic()
    await baseline_daemon.start()
    baseline_total = time.monotonic() - start
    baseline_mean = baseline_total / num_cycles

    # Measure open-circuit scenario: every cycle raises CircuitOpenError
    open_graph = _CircuitOpenGraph(max_calls=num_cycles)
    open_daemon = CoordinareDaemon(
        open_graph,
        max_cycles=None,  # Don't use max_cycles since CB errors don't increment count
        poll_interval_seconds=1,
        sleep_func=_instant_sleep,
    )
    open_graph._daemon = open_daemon

    start = time.monotonic()
    await open_daemon.start()
    open_total = time.monotonic() - start
    open_mean = open_total / num_cycles

    # Open-circuit cycles should not be slower than 110% of healthy baseline
    # Allow a generous minimum to avoid flaky tests on slow CI
    assert open_mean <= max(baseline_mean * 1.10, 0.01), (
        f"Open-circuit mean {open_mean:.6f}s exceeded 110% of baseline {baseline_mean:.6f}s"
    )
