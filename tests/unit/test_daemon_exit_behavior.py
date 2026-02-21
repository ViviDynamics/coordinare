from __future__ import annotations

import pytest

from coordinare.daemon import CoordinareDaemon, RuntimeExecutionError


class _SuccessfulGraph:
    async def ainvoke(self, state):
        updated = dict(state)
        updated["phase"] = "running"
        return updated


class _FailingGraph:
    async def ainvoke(self, state):
        raise RuntimeError("boom")


class _StopDuringCycleGraph:
    """Graph that calls stop() on the daemon while ainvoke is in progress."""

    def __init__(self, daemon_ref: list) -> None:
        self._daemon_ref = daemon_ref

    async def ainvoke(self, state):
        # Signal stop mid-cycle to simulate SIGTERM during active processing
        self._daemon_ref[0].stop()
        updated = dict(state)
        updated["phase"] = "active"
        return updated


async def _no_sleep(_: int) -> None:
    return None


@pytest.mark.asyncio
async def test_daemon_stops_after_max_cycles() -> None:
    daemon = CoordinareDaemon(
        _SuccessfulGraph(),
        poll_interval_seconds=1,
        heartbeat_interval_seconds=1,
        max_cycles=1,
        sleep_func=_no_sleep,
    )

    await daemon.start()

    assert daemon.running is False


@pytest.mark.asyncio
async def test_daemon_raises_runtime_error_on_cycle_failure() -> None:
    daemon = CoordinareDaemon(
        _FailingGraph(),
        poll_interval_seconds=1,
        heartbeat_interval_seconds=1,
        max_cycles=1,
        sleep_func=_no_sleep,
    )

    with pytest.raises(RuntimeExecutionError) as exc_info:
        await daemon.start()

    assert exc_info.value.phase == "runtime"
    assert exc_info.value.step == "cycle_execution"


@pytest.mark.asyncio
async def test_daemon_emits_recovery_phase_before_exit_on_failure() -> None:
    """FR-006: recovery state must be observable in output before non-zero exit."""
    emitted_events: list[dict] = []

    daemon = CoordinareDaemon(
        _FailingGraph(),
        poll_interval_seconds=1,
        heartbeat_interval_seconds=1,
        max_cycles=1,
        sleep_func=_no_sleep,
    )
    original_emit = daemon._emit

    def capturing_emit(**event):
        emitted_events.append(event)
        original_emit(**event)

    daemon._emit = capturing_emit  # type: ignore[method-assign]

    with pytest.raises(RuntimeExecutionError):
        await daemon.start()

    state_changes = [e for e in emitted_events if e.get("category") == "state_change"]
    recovery_transitions = [e for e in state_changes if e.get("current_phase") == "recovery"]
    assert len(recovery_transitions) == 1, "Expected exactly one transition to recovery state before exit"

    # Recovery must appear before shutdown in the event stream
    recovery_idx = emitted_events.index(recovery_transitions[0])
    shutdown_events = [e for e in emitted_events if e.get("category") == "shutdown"]
    assert shutdown_events, "Expected a shutdown event"
    shutdown_idx = emitted_events.index(shutdown_events[0])
    assert recovery_idx < shutdown_idx, "Recovery state must be emitted before shutdown"


@pytest.mark.asyncio
async def test_daemon_shutdown_reports_cycle_interrupted_when_stop_called_mid_cycle() -> None:
    """FR-007: shutdown message must indicate cycle_interrupted=True when stop() fires during ainvoke."""
    emitted_events: list[dict] = []
    daemon_ref: list = [None]

    daemon = CoordinareDaemon(
        _StopDuringCycleGraph(daemon_ref),
        poll_interval_seconds=1,
        heartbeat_interval_seconds=1,
        sleep_func=_no_sleep,
    )
    daemon_ref[0] = daemon

    original_emit = daemon._emit

    def capturing_emit(**event):
        emitted_events.append(event)
        original_emit(**event)

    daemon._emit = capturing_emit  # type: ignore[method-assign]

    await daemon.start()

    shutdown_events = [e for e in emitted_events if e.get("category") == "shutdown"]
    assert shutdown_events, "Expected a shutdown event"
    assert shutdown_events[0].get("cycle_interrupted") is True


@pytest.mark.asyncio
async def test_daemon_shutdown_reports_cycle_not_interrupted_on_clean_stop() -> None:
    """FR-007: shutdown message must have cycle_interrupted=False when stop is called between cycles."""
    emitted_events: list[dict] = []

    daemon = CoordinareDaemon(
        _SuccessfulGraph(),
        poll_interval_seconds=1,
        heartbeat_interval_seconds=1,
        max_cycles=1,
        sleep_func=_no_sleep,
    )

    original_emit = daemon._emit

    def capturing_emit(**event):
        emitted_events.append(event)
        original_emit(**event)

    daemon._emit = capturing_emit  # type: ignore[method-assign]

    await daemon.start()

    shutdown_events = [e for e in emitted_events if e.get("category") == "shutdown"]
    assert shutdown_events, "Expected a shutdown event"
    assert shutdown_events[0].get("cycle_interrupted") is False
