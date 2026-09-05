"""Spec 164: the QA repair brief travels on BOTH terminal QA verdicts.

``qa_findings`` exists so a failing QA can tell the next implementer what
failed and how to reproduce it. Lifting it only on ``qa_passed`` (where it is
nearly always empty) defeats the feature: the one verdict that carries a
useful brief is the one that dropped it.
"""

from __future__ import annotations

import pytest

from coordinare.graph.nodes.monitor_performer import monitor_performer
from coordinare.graph.state import initial_state


class _Performer:
    def __init__(self, response: dict) -> None:
        self._response = response

    async def check_status(self, session_id: str, **kwargs: object) -> dict:
        _ = session_id
        return self._response


def _qa_state(response: dict) -> dict:
    state = initial_state()
    state["performer_services"] = {"qa": _Performer(response)}
    state["performer_stage"] = "qa"
    state["lifecycle_sequence"] = ["implementing", "qa", "closing_review"]
    state["current_card"] = {"id": "ITEM_1", "status": "IN_PROGRESS"}
    state["agent_dispatch"] = {"session_id": "s1"}
    state["current_symphony"] = "website"
    return state


_FINDING = {
    "category": "criterion_unmet",
    "criterion": "Workspace field is present",
    "expected": "a Workspace select on /settings",
    "observed": "no Workspace control in the DOM",
    "reproduce": "GET /settings and inspect the form",
}


@pytest.mark.asyncio
async def test_qa_failed_lifts_findings_for_the_implementer():
    state = _qa_state({
        "status": "qa_failed",
        "failures": [{"criterion": "Workspace field is present", "detail": "missing"}],
        "report": {
            "criteria_checked": 1,
            "criteria_passed": 0,
            "qa_findings": [_FINDING, "not-a-dict"],
        },
    })
    result = await monitor_performer(state)

    assert result["performer_stage"] == "implementing"  # bounced
    assert result["qa_findings"] == [_FINDING]


@pytest.mark.asyncio
async def test_qa_passed_still_lifts_findings():
    state = _qa_state({
        "status": "qa_passed",
        "report": {
            "criteria_checked": 1,
            "criteria_passed": 1,
            "qa_findings": [_FINDING],
        },
    })
    result = await monitor_performer(state)

    assert result["qa_findings"] == [_FINDING]


@pytest.mark.asyncio
async def test_a_failed_qa_without_a_workflow_does_not_grow_state():
    state = _qa_state({
        "status": "qa_failed",
        "failures": [{"criterion": "x", "detail": "y"}],
        "report": {"criteria_checked": 1, "criteria_passed": 0},
    })
    result = await monitor_performer(state)

    assert "qa_findings" not in result


@pytest.mark.asyncio
async def test_a_round_without_a_brief_clears_the_previous_rounds_brief():
    state = _qa_state({
        "status": "qa_failed",
        "failures": [{"criterion": "x", "detail": "y"}],
        "report": {"criteria_checked": 1, "criteria_passed": 0},
    })
    state["qa_findings"] = [_FINDING]  # left over from an earlier QA round
    result = await monitor_performer(state)

    assert "qa_findings" not in result


@pytest.mark.asyncio
async def test_an_empty_brief_replaces_the_previous_rounds_brief():
    state = _qa_state({
        "status": "qa_passed",
        "report": {"criteria_checked": 1, "criteria_passed": 1, "qa_findings": []},
    })
    state["qa_findings"] = [_FINDING]
    result = await monitor_performer(state)

    assert result["qa_findings"] == []
