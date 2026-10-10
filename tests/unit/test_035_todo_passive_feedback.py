from __future__ import annotations

from copy import deepcopy

import pytest

from coordinare.graph.nodes.check_board import _reset_and_rehydrate
from coordinare.graph.routing import route_from_board_check
from coordinare.graph.state import initial_state


def passive_session() -> dict:
    return {
        "phase": "monitoring_pr", "performer_stage": "implementing",
        "current_card": {"id": "card-A", "status": "TODO", "previous_status": "BACKLOG",
                         "pr_url": "https://github.com/example/sample/pull/7",
                         "pushed_branch": "conductor/card-A/existing", "head_after": "head-A"},
        "dispatched_feedback": {"stage": "implementing", "items": [{"id": "review-A", "body": "Add missing regression coverage"}]},
        "processed_review_ids": {"review-A"}, "total_feedback_cycles": 4,
    }


async def admission(session: dict, eligible: bool = True) -> dict:
    state = initial_state()
    state.update(phase=session["phase"], performer_stage="implementing", active_card_id="card-A", active_sessions={"card-A": session})
    await _reset_and_rehydrate(state, {"TODO": ["card-A"]} if eligible else {"BACKLOG": ["card-A"]},
                               ["card-A"] if eligible else [], 1, state["active_sessions"])
    return state


@pytest.mark.asyncio
async def test_todo_admission_resumes_unfinished_feedback_on_passive_existing_pr() -> None:
    session = passive_session()
    retained = deepcopy(session)
    state = await admission(session)
    assert session["phase"] == "dispatching"
    assert state["phase"] == "dispatching"
    assert route_from_board_check(state) == "dispatch"
    for key in ("id", "pr_url", "pushed_branch", "head_after"):
        assert session["current_card"][key] == retained["current_card"][key]
    assert session["current_card"]["status"] == "TODO"
    assert session["dispatched_feedback"] == retained["dispatched_feedback"]
    assert session["processed_review_ids"] == {"review-A"}
    assert session["total_feedback_cycles"] == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("changes", [
    {"dispatched_feedback": {}},
    {"dispatched_feedback": {"stage": "implementing", "items": []}},
    {"dispatched_feedback": {"stage": "reviewing", "items": [{"id": "review-A"}]}},
    {"board_paused": True},
    {"agent_dispatch": {"session_id": "live-worker"}},
    {"phase": "monitoring_performer", "agent_dispatch": {"session_id": "live-worker"}},
])
async def test_passive_retry_does_not_redispatch_completed_unowned_held_or_live_work(changes: dict) -> None:
    session = passive_session()
    session.update(changes)
    before = deepcopy(session)
    await admission(session)
    assert session == before


@pytest.mark.asyncio
async def test_backlog_does_not_admit_retained_passive_feedback() -> None:
    session = passive_session()
    before = deepcopy(session)
    await admission(session, eligible=False)
    assert session == before
