from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from coordinare.graph.builder import CoordinareGraphBuilder
from coordinare.graph.nodes.assess_card import assess_card
from coordinare.graph.nodes.dispatch_performer import _feedback_for_dispatch
from coordinare.graph.routing import route_from_board_check
from coordinare.graph.state import initial_state


def retry_state():
    state = initial_state()
    state.update(current_card={"id": "card-A", "issue_id": "issue-A", "pr_url": "https://github.com/example/sample/pull/7", "pr_node_id": "pr-A", "pushed_branch": "conductor/card-A/existing", "head_after": "head-A"},
                 phase="dispatching", performer_stage="implementing",
                 lifecycle_sequence=["implementing"], github_service=AsyncMock(), conducting_backend=AsyncMock())
    state["github_service"].find_pr_for_issue.return_value = {"pr_url": state["current_card"]["pr_url"], "pr_node_id": "pr-A"}
    return state


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["dispatched_feedback", "relay_feedback"])
async def test_todo_retry_graph_dispatches_unfulfilled_feedback_on_same_pr(source: str) -> None:
    state = retry_state()
    item = {"kind": "review", "body": "Add the missing mixed-token regression", "target_stage": "implementing"}
    state[source] = {"stage": "implementing", "items": [item]} if source == "dispatched_feedback" else [item]
    state["processed_review_ids"] = {"already-dispatched-review"}
    dispatched = []

    async def passthrough(current):
        return current

    async def dispatch(current):
        dispatched.append({"stage": current["performer_stage"], "card": dict(current["current_card"]),
                           "feedback": _feedback_for_dispatch(current, current["performer_stage"])})
        current["phase"] = "monitoring_performer"
        current["agent_dispatch"] = {"session_id": "replacement-A"}
        return current

    graph = CoordinareGraphBuilder(node_overrides={
        "route_issue_comments": passthrough, "check_board": passthrough,
        "classify_scope": passthrough, "dispatch_card": dispatch,
        "monitor_pr": passthrough, "notify": passthrough,
    }).build()
    result = await graph.ainvoke(state)
    assert result["phase"] == "monitoring_performer"
    assert dispatched == [{"stage": "implementing", "card": state["current_card"], "feedback": [item]}]
    assert result["agent_dispatch"] == {"session_id": "replacement-A"}
    assert result["processed_review_ids"] == {"already-dispatched-review"}
    result["conducting_backend"].assess.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("batch", [{}, {"stage": "implementing", "items": []}, {"stage": "reviewing", "items": [{"body": "Review this"}]}])
async def test_open_pr_without_current_stage_work_still_skips_to_monitoring(batch: dict) -> None:
    state = retry_state()
    state["dispatched_feedback"] = batch
    assert route_from_board_check(state) == "assess"
    result = await assess_card(state)
    assert result["phase"] == "monitoring_pr"
    result["conducting_backend"].assess.assert_not_awaited()
