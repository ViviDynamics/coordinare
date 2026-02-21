from __future__ import annotations

from pathlib import Path

import pytest

from coordinare.daemon import CoordinareDaemon, RuntimeExecutionError
from coordinare.lib.runtime_events import build_runtime_event


def test_runtime_event_state_context_is_observable() -> None:
    event = build_runtime_event(
        category="state_change",
        message="state transition detected",
        previous_phase="idle",
        current_phase="running",
    )

    assert event["category"] == "state_change"
    assert event["previous_phase"] == "idle"
    assert event["current_phase"] == "running"


def test_contracts_describe_same_state_visibility_categories() -> None:
    shell_contract = Path("specs/002-docker-cli-output/contracts/shell-runtime-contract.md").read_text()
    compose_contract = Path("specs/002-docker-cli-output/contracts/compose-runtime-contract.md").read_text()

    for keyword in ("startup", "activity", "heartbeat", "failure", "shutdown"):
        assert keyword in shell_contract
        assert keyword in compose_contract


@pytest.mark.asyncio
async def test_recovery_state_is_observable_in_output_before_exit() -> None:
    """US3 scenario 4: reviewer can see recovery state messaging before process exits non-zero."""

    class _FailingGraph:
        async def ainvoke(self, state):
            raise RuntimeError("simulated failure")

    async def _no_sleep(_: int) -> None:
        return None

    emitted: list[dict] = []

    daemon = CoordinareDaemon(
        _FailingGraph(),
        poll_interval_seconds=1,
        heartbeat_interval_seconds=1,
        max_cycles=1,
        sleep_func=_no_sleep,
    )
    original_emit = daemon._emit

    def capturing_emit(**event):
        emitted.append(event)
        original_emit(**event)

    daemon._emit = capturing_emit  # type: ignore[method-assign]

    with pytest.raises(RuntimeExecutionError):
        await daemon.start()

    categories = [e["category"] for e in emitted]
    # recovery state_change must appear in the event stream
    recovery_events = [e for e in emitted if e.get("category") == "state_change" and e.get("current_phase") == "recovery"]
    assert recovery_events, "Recovery state transition must be visible in output before exit"
    # and it must precede shutdown
    assert categories.index("shutdown") > emitted.index(recovery_events[0])
