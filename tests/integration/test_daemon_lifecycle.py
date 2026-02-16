from __future__ import annotations

import pytest

from coordinare.daemon import CoordinareDaemon


class _Graph:
    async def ainvoke(self, state):
        state["phase"] = "running"
        return state


async def _no_sleep(_: int) -> None:
    return None


@pytest.mark.asyncio
async def test_daemon_startup_and_shutdown_cycle() -> None:
    daemon = CoordinareDaemon(_Graph(), max_cycles=1, sleep_func=_no_sleep)

    await daemon.start()

    assert daemon.running is False


def test_config_only_onboarding_validation() -> None:
    # SC-007 smoke coverage: daemon can be constructed from config-derived values.
    daemon = CoordinareDaemon(_Graph(), poll_interval_seconds=30, heartbeat_interval_seconds=30, max_cycles=1)
    assert daemon.running is False
