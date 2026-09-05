"""T001b: a finished workflow leaves its timings in the performer log.

The step durations live on ``WorkflowMetrics`` in memory. Nothing durable saw
them until the adapter logged them, and the live measurement T001b asks for
reads the container's stderr, so the completion line is the measurement.
"""

from __future__ import annotations

import pytest
from performer.workflows.adapter import WorkflowAdapter
from structlog.testing import capture_logs


class _Score:
    role = "qa"
    model = "m"


@pytest.mark.asyncio
async def test_completion_logs_metrics_for_measurement():
    adapter = WorkflowAdapter("noop", toolkit_factory=lambda metrics, sink: object())
    adapter._metrics.step_durations_ms["plan"] = 12
    adapter._metrics.model_calls = 3

    with capture_logs() as logs:
        await adapter.start(object(), _Score())
        await adapter._task

    done = [e for e in logs if e["event"] == "workflow.completed"]
    assert len(done) == 1
    entry = done[0]
    assert entry["workflow"] == "noop"
    assert entry["step_durations_ms"] == {"plan": 12}
    assert entry["model_calls"] == 3
    assert isinstance(entry["total_ms"], int) and entry["total_ms"] >= 0
    assert adapter.get_status().state != "error"


class _Boom:
    name = "boom"

    async def run(self, stand, score, toolkit):
        raise RuntimeError("no")


@pytest.mark.asyncio
async def test_failure_logs_timings_but_never_completed():
    adapter = WorkflowAdapter("noop", toolkit_factory=lambda metrics, sink: object())
    adapter._workflow = _Boom()

    with capture_logs() as logs:
        await adapter.start(object(), _Score())
        await adapter._task

    events = [e["event"] for e in logs]
    assert "workflow.failed" in events
    assert "workflow.completed" not in events
    failed = next(e for e in logs if e["event"] == "workflow.failed")
    assert "total_ms" in failed
    assert failed["step_durations_ms"] == {}
    assert adapter.get_status().state == "error"
