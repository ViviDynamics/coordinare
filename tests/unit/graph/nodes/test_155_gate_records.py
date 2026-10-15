"""#155 review: the CI gate records what it decided, not what it was about to decide.

Each path of the gate leaves one ci_result on the card's lifecycle: the classified outcome
(FORWARD, BOUNCE, HOLD or ENV_BLOCKED), or UNKNOWN when the gate could not judge (disabled, no
PR, or an error). The gate's behaviour is unchanged, only what is written down. The fixtures
come from test_monitor_performer_ci_gate.py.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from coordinare.graph.nodes.monitor_performer import (
    _reset_ci_gate_api_error_cooldown,
    monitor_performer,
)
from coordinare.services.card_lifecycle import CardLifecycleLog
from tests.unit.graph.nodes.test_monitor_performer_ci_gate import (
    _ci_gate_state,
    _env_gate_state,
    _GitHubWithRollup,
    _rollup_payload,
)

HEAD = "a" * 40


@pytest.fixture(autouse=True)
def _clear_cooldown() -> Iterator[None]:
    _reset_ci_gate_api_error_cooldown()
    yield
    _reset_ci_gate_api_error_cooldown()


def _with_log(state: dict) -> tuple[dict, CardLifecycleLog]:
    log = CardLifecycleLog()
    state["card_lifecycle"] = log
    return state, log


def _gate_results(log: CardLifecycleLog) -> list[dict]:
    return [e for e in log.events("default", "ITEM_1") if e["kind"] == "ci_result"]


@pytest.mark.asyncio
async def test_a_bounce_records_bounce_with_the_gated_head() -> None:
    payload = _rollup_payload(contexts=[
        {"__typename": "CheckRun", "name": "unit-tests", "status": "COMPLETED", "conclusion": "FAILURE"},
    ])
    state, log = _with_log(_ci_gate_state(_GitHubWithRollup(payload)))
    await monitor_performer(state)
    [record] = _gate_results(log)
    assert record["detail"] == {"result": "BOUNCE", "head": HEAD[:12]}


@pytest.mark.asyncio
async def test_a_pass_records_forward() -> None:
    state, log = _with_log(_ci_gate_state(_GitHubWithRollup(_rollup_payload())))
    await monitor_performer(state)
    [record] = _gate_results(log)
    assert record["detail"]["result"] == "FORWARD"


@pytest.mark.asyncio
async def test_an_env_blocked_hold_records_env_blocked_not_bounce() -> None:
    payload = _rollup_payload(contexts=[{
        "__typename": "CheckRun", "name": "Build Pull Request", "status": "COMPLETED",
        "conclusion": "FAILURE", "title": "Failed to CreateArtifact: artifact storage quota has been hit",
    }])
    state, log = _with_log(_env_gate_state(_GitHubWithRollup(payload)))
    result = await monitor_performer(state)
    assert result["latest_ci_gate_decision"]["verdict"] == "hold"
    [record] = _gate_results(log)
    assert record["detail"]["result"] == "ENV_BLOCKED"


@pytest.mark.asyncio
async def test_an_error_records_unknown_so_an_old_result_does_not_persist() -> None:
    state, log = _with_log(_ci_gate_state(_GitHubWithRollup(raise_on_execute=True)))
    await monitor_performer(state)
    [record] = _gate_results(log)
    assert record["detail"]["result"] == "UNKNOWN"


@pytest.mark.asyncio
async def test_a_disabled_gate_records_unknown() -> None:
    state, log = _with_log(_ci_gate_state(_GitHubWithRollup(), enabled=False))
    await monitor_performer(state)
    [record] = _gate_results(log)
    assert record["detail"]["result"] == "UNKNOWN"
