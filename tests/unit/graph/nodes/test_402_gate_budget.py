"""#402 (coordinare side): a budget finding must not regenerate the env cache.

Every local-test-gate env_blocked forces an env-cache regen via
``mark_runtime_health_failed``. That is right for a service that never came
up and wrong for a suite that simply outlived its budget with healthy
services; on 2026-09-13 it cost two bootstraps (~50 min, see #400) for an
environment that was fine. The performer now says which it was in the
response's structured ``report``; this branch reads it.

MUTATION THAT MUST FAIL A TEST HERE:
  M4  call mark_runtime_health_failed regardless of the report
      -> test_a_budget_finding_holds_the_card_without_a_regen
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from coordinare.graph.nodes.monitor_performer import monitor_performer
from tests.unit.graph.nodes.test_monitor_performer import _make_state, _Performer


def _state_for(response: dict) -> tuple[dict, MagicMock]:
    svc = _Performer(response=response)
    state = _make_state(service=svc, stage="implementing", sequence=["implementing", "qa"])
    env_svc = MagicMock()
    state["env_cache_service"] = env_svc
    state["current_symphony"] = "website"
    slot_mgr = MagicMock()
    slot_mgr.acquire.return_value = svc
    state["slot_manager"] = slot_mgr
    return state, env_svc


@pytest.mark.asyncio
async def test_a_budget_finding_holds_the_card_without_a_regen() -> None:
    """M4: held (an operator must raise the budget) but the cache is fine."""
    state, env_svc = _state_for({
        "status": "env_blocked",
        "reason": "the test command did not finish within 600s ... services were healthy; the suite exceeded the gate's budget",
        "report": {"local_test_gate": {"budget_exceeded": True, "timeout_seconds": 600, "duration_seconds": 600.5}},
    })
    result = await monitor_performer(state)

    assert result["phase"] == "blocked"
    env_svc.mark_runtime_health_failed.assert_not_called()
    assert "budget" in str(result.get("env_health_hold_reason", "")).lower()
    # 399 x 402: no recovery marker. The cache is healthy, so a marker would
    # lift the card next cycle, re-run the same over-budget suite and block it
    # again, oscillating at a full run per cycle. Raising the budget is a human
    # action; the hold has to wait for it.
    assert result.get("env_blocked") is None, "a budget hold must not be auto-recoverable"


@pytest.mark.asyncio
async def test_a_genuine_environment_block_still_regenerates() -> None:
    """Regression pin: no report, or a report without the flag, keeps today's
    behaviour so a real services failure still rebuilds the cache."""
    state, env_svc = _state_for({
        "status": "env_blocked",
        "reason": "env-cache services-start failed: postgres did not become ready within 180s",
    })
    result = await monitor_performer(state)

    assert result["phase"] == "blocked"
    env_svc.mark_runtime_health_failed.assert_called_once()
    assert isinstance(result.get("env_blocked"), dict), "a genuine env block keeps its recovery marker (399)"
