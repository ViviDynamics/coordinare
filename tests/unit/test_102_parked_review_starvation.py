"""#102: a card parked in monitoring_pr must get its merge evaluation every cycle.

Before the fix, the fanout admission (select_pipelines) let a running performer
take the only reservation, and any parked review card that was not selected was
downgraded to pipeline_capacity and never ticked, so its closer never ran.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
import structlog

from coordinare import daemon as daemon_module
from coordinare.daemon import (
    _compute_session_eligibilities,
    _FanoutContext,
    _invoke_session_tick,
)
from coordinare.graph.nodes.check_board import _shares_cycle_reads
from coordinare.graph.state import initial_state


@pytest.fixture(autouse=True)
def _forget_logged_waits() -> None:
    daemon_module._review_wait_logged.clear()


def _parked_and_running() -> dict[str, dict[str, Any]]:
    return {
        "parked": {
            "current_card": {
                "id": "parked",
                "content_id": "parked",
                "status": "IN_REVIEW",
                "pr_number": 81,
            },
            "phase": "monitoring_pr",
            "performer_stage": "closing_review",
            "review_wait": "human",
            "pipeline_admitted": True,
        },
        "running": {
            "current_card": {"id": "running", "content_id": "running", "status": "IN_PROGRESS"},
            "phase": "monitoring_performer",
            "performer_stage": "implementing",
            "agent_dispatch": {"session_id": "worker"},
        },
    }


def _state_for(sessions: dict[str, dict[str, Any]]) -> Any:
    state = initial_state()
    state["active_sessions"] = sessions
    state["board_snapshot"] = {
        "IN_REVIEW": ["parked"],
        "IN_PROGRESS": ["running"],
        "TODO": [],
        "BACKLOG": [],
        "BLOCKED": [],
    }
    return state


def test_parked_review_card_stays_eligible_while_running_work_holds_the_budget() -> None:
    sessions = _parked_and_running()
    state = _state_for(sessions)

    eligibility = _compute_session_eligibilities(state, sessions, 1)

    # The performer keeps the only pipeline reservation (cap unchanged) ...
    assert state["_pipeline_selected"] == {"running"}
    # ... but the parked card is still evaluated this cycle, not skipped.
    assert eligibility["parked"].eligible
    assert eligibility["parked"].reason == "eligible"


class _RecordingGraph:
    def __init__(self) -> None:
        self.invoked: list[str] = []

    async def ainvoke(self, state: Any) -> Any:
        self.invoked.append(str(state.get("active_card_id")))
        return state


@pytest.mark.asyncio
async def test_parked_review_card_graph_runs_each_cycle_while_another_card_runs() -> None:
    sessions = _parked_and_running()
    state = _state_for(sessions)
    eligibility = _compute_session_eligibilities(state, sessions, 1)
    graph = _RecordingGraph()
    ctx = _FanoutContext(
        state=state,
        active_sessions=sessions,
        eligibilities=eligibility,
        blocked_to_poll=set(),
        graph=graph,
        semaphore=asyncio.Semaphore(1),
    )

    result = await _invoke_session_tick(ctx, "parked", sessions["parked"])

    assert not result.skipped
    assert graph.invoked == ["parked"]


def test_a_review_wait_is_logged_as_healthy_whatever_else_is_running() -> None:
    """Since #179 a parked card holds no slot by design, so running work beside
    it is not starvation. It is reported as waiting on review, with the count."""
    sessions = _parked_and_running()
    with structlog.testing.capture_logs() as captured:
        _compute_session_eligibilities(_state_for(sessions), sessions, 1)
    waits = [e for e in captured if e["event"] == "session.awaiting_review"]
    assert [(e["card_id"], e["parked_in_review"]) for e in waits] == [("parked", 1)]
    assert not [e for e in captured if e["event"] == "session.review_starved_by_capacity"]


def test_a_wait_is_logged_when_it_starts_and_not_again_every_cycle() -> None:
    sessions = _parked_and_running()
    state = _state_for(sessions)
    with structlog.testing.capture_logs() as captured:
        for _ in range(3):
            _compute_session_eligibilities(state, sessions, 1)
    assert len([e for e in captured if e["event"] == "session.awaiting_review"]) == 1

    # The card leaves review and comes back: that is a new wait.
    sessions["parked"]["phase"] = "dispatching"
    _compute_session_eligibilities(state, sessions, 1)
    sessions["parked"]["phase"] = "monitoring_pr"
    with structlog.testing.capture_logs() as after:
        _compute_session_eligibilities(state, sessions, 1)
    assert [e["card_id"] for e in after if e["event"] == "session.awaiting_review"] == ["parked"]


def test_ticks_share_one_board_read_when_cards_wait_beside_the_running_one() -> None:
    """With a cap of one, each parked card's tick must not poll the board again."""
    assert _shares_cycle_reads(_state_for(_parked_and_running()), 1)
    alone = _state_for({"running": _parked_and_running()["running"]})
    assert not _shares_cycle_reads(alone, 1)
    assert _shares_cycle_reads(alone, 2)
