"""Command counter in WorkflowMetrics (spec 166 FR-003).

run_command increments metrics.commands_run; a toolkit without command_runner
raises and does not increment.
"""
from __future__ import annotations

import pytest
from performer.workflows.base import WorkflowMetrics
from performer.workflows.toolkit import Toolkit


@pytest.mark.asyncio
async def test_run_command_increments_commands_run():
    """Every run_command call increments metrics.commands_run before delegating."""
    called: list[tuple[str, int]] = []

    async def runner(cmd, cwd, timeout_s):
        called.append((cmd, 0))
        return 0, "done"

    metrics = WorkflowMetrics()
    assert metrics.commands_run == 0
    tk = Toolkit(metrics=metrics, command_runner=runner)

    await tk.run_command("ls /")
    assert metrics.commands_run == 1
    assert len(called) == 1

    await tk.run_command("cat file.txt")
    assert metrics.commands_run == 2
    assert len(called) == 2


@pytest.mark.asyncio
async def test_toolkit_without_command_runner_raises_without_incrementing():
    """A toolkit with no command_runner raises RuntimeError and does not touch
    the counter."""
    metrics = WorkflowMetrics()
    tk = Toolkit(metrics=metrics, command_runner=None)

    with pytest.raises(RuntimeError, match="command runner"):
        await tk.run_command("echo hi")

    assert metrics.commands_run == 0
