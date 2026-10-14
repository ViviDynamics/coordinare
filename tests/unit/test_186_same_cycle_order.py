"""#186 B: a returning card goes before a card that has not started, in the same cycle.

Before the fix, the cycle's selection admitted a not-yet-started To Do card
first. A parked card only learns that it needs a performer in its own tick, so
its claim came too late and was refused.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from coordinare import daemon as daemon_module
from coordinare.daemon import CoordinareDaemon
from coordinare.services.pipeline_budget import dispatch_has_pipeline_slot, select_pipelines


class _DispatchingGraph:
    """Stands in for one session's graph tick: a card that needs a performer tries for a slot."""

    def __init__(self) -> None:
        self.invoked: list[str] = []

    async def ainvoke(self, state: Any) -> Any:
        card_id = str(state.get("active_card_id"))
        self.invoked.append(card_id)
        if state.get("phase") == "monitoring_pr":
            # monitor_pr found review feedback on the parked card.
            state["phase"] = "relay_feedback"
        if state.get("phase") in {"relay_feedback", "dispatching"}:
            if dispatch_has_pipeline_slot(state, card_id):
                state["phase"] = "monitoring_performer"
                state["agent_dispatch"] = {"session_id": f"job-{card_id}"}
            else:
                state["phase"] = "dispatching"
        return state


def _config(limit: int) -> SimpleNamespace:
    return SimpleNamespace(max_concurrent_cards=limit, max_cards_in_review=None, poll_interval_seconds=30)


def _sessions() -> dict[str, Any]:
    return {
        # Not started, left over from the cycle before: it is the one the old selection admitted first.
        "next": {
            "current_card": {"id": "next", "content_id": "next", "status": "TODO"},
            "phase": "dispatching",
            "performer_stage": "assessing",
            "pipeline_admitted": False,
        },
        "reviewed": {
            "current_card": {"id": "reviewed", "content_id": "reviewed", "status": "IN_REVIEW", "pr_number": 81},
            "phase": "monitoring_pr",
            "performer_stage": "closing_review",
            "review_wait": "human",
            "pipeline_admitted": False,
        },
    }


@pytest.fixture(autouse=True)
def _forget_logged_waits() -> None:
    daemon_module._review_wait_logged.clear()


@pytest.mark.asyncio
async def test_a_returning_card_is_dispatched_before_a_card_that_has_not_started() -> None:
    daemon = CoordinareDaemon(_DispatchingGraph(), max_cycles=1, sleep_func=lambda _s: None)
    daemon._state["config"] = _config(1)
    daemon._state["active_sessions"] = _sessions()
    daemon._state["board_snapshot"] = {"TODO": ["next"], "IN_REVIEW": ["reviewed"]}
    graph: _DispatchingGraph = daemon._graph

    await daemon._invoke_multi_session()

    sessions = daemon._state["active_sessions"]
    assert sessions["reviewed"]["agent_dispatch"] == {"session_id": "job-reviewed"}
    # The card the selection did not admit is not ticked at all this cycle, so it
    # cannot dispatch; it stays where it was and is admitted once a slot is free.
    assert graph.invoked == ["reviewed"]
    assert sessions["next"]["phase"] == "dispatching"
    assert sessions["next"]["pipeline_admitted"] is False


def test_the_phase_one_selection_holds_back_cards_that_have_not_started() -> None:
    """Before the parked card's tick, only started work is selected, so its claim finds the slot free."""
    sessions = _sessions()
    selected = select_pipelines(sessions, 1, admit_unstarted=False)
    assert selected == set()
    state = {"active_sessions": sessions, "config": _config(1), "_pipeline_selected": selected}
    assert dispatch_has_pipeline_slot(state, "reviewed")
    assert not dispatch_has_pipeline_slot(state, "next")


def test_the_full_selection_after_a_claim_never_exceeds_the_limit() -> None:
    sessions = _sessions()
    sessions["reviewed"]["phase"] = "monitoring_performer"
    sessions["reviewed"]["agent_dispatch"] = {"session_id": "job-reviewed"}
    assert select_pipelines(sessions, 1) == {"reviewed"}
    assert sessions["next"]["pipeline_admitted"] is False
