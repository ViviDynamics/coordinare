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
