"""165 FR-009: the monitor lifts the architect workflow's blueprint into the
session, replaces a prior one, resets the side run, and leaves the prose path
alone."""
from __future__ import annotations

import pytest

from coordinare.graph.nodes.monitor_performer import monitor_performer
from coordinare.graph.state import initial_state


class _Performer:
    def __init__(self, response: dict) -> None:
        self._response = response

    async def check_status(self, session_id: str, **kwargs: object) -> dict:
        return self._response


def _state(response: dict) -> dict:
    state = initial_state()
    state["performer_services"] = {"architecting": _Performer(response)}
    state["performer_stage"] = "architecting"
    state["lifecycle_sequence"] = ["architecting", "implementing", "qa"]
    state["current_card"] = {"id": "ITEM_1", "status": "IN_PROGRESS"}
    state["agent_dispatch"] = {"session_id": "s1"}
    state["current_symphony"] = "website"
    return state


def _bp(tag: str) -> dict:
    return {
        "summary": tag,
        "milestones": [{"goal": tag, "scope": ["a"], "done_when": "d"}],
        "modules": [], "data_model": {"changes": []}, "interfaces": [], "risks": [],
        "criteria": [{"surface": "/", "action": "open", "expected": "ok", "kind": "functional"}],
        "docs": [], "size": "small", "blueprint_hash": tag, "created_at": "t",
    }


@pytest.mark.asyncio
async def test_plan_committed_with_a_report_lifts_the_blueprint_and_advances():
    state = _state({"status": "plan_committed", "report": {"blueprint": _bp("h1"), "size": "small"}})
    result = await monitor_performer(state)
    assert result["blueprint"]["blueprint_hash"] == "h1"
    assert result["documenting_side"] is None
    assert result["performer_stage"] == "implementing"


@pytest.mark.asyncio
async def test_a_new_blueprint_replaces_the_old_and_resets_the_side_run():
    state = _state({"status": "plan_committed", "report": {"blueprint": _bp("h2"), "size": "large"}})
    state["blueprint"] = _bp("h1")
    state["documenting_side"] = {"status": "done", "blueprint_hash": "h1"}
    result = await monitor_performer(state)
    assert result["blueprint"]["blueprint_hash"] == "h2"
    assert result["documenting_side"] is None


@pytest.mark.asyncio
async def test_the_prose_architect_leaves_state_untouched():
    state = _state({"status": "plan_committed", "plan_path": "docs/cards/1/plan.md"})
    result = await monitor_performer(state)
    assert "blueprint" not in result or result.get("blueprint") is None
    assert result["performer_stage"] == "implementing"


@pytest.mark.asyncio
async def test_a_hollow_blueprint_in_the_report_is_not_lifted():
    state = _state({"status": "plan_committed", "report": {"blueprint": {**_bp("h3"), "milestones": []}}})
    result = await monitor_performer(state)
    assert not result.get("blueprint")
