from __future__ import annotations

import pytest
from pydantic import ValidationError

from coordinare.config import ProjectConfiguration
from coordinare.daemon import CoordinareDaemon, RuntimeExecutionError


def test_startup_failure_classification_invalid_config(tmp_path) -> None:
    config_path = tmp_path / "bad.yaml"
    config_path.write_text("project_name: only-name\n")

    with pytest.raises(ValidationError):
        ProjectConfiguration.from_yaml(config_path)


class _FailingGraph:
    async def ainvoke(self, state):
        raise RuntimeError("cycle failure")


async def _no_sleep(_: int) -> None:
    return None


@pytest.mark.asyncio
async def test_runtime_failure_classification_cycle_execution() -> None:
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
