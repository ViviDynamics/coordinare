from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from coordinare.graph.nodes.check_board import _reset_and_rehydrate
from coordinare.graph.nodes.monitor_pr import monitor_pr
from coordinare.graph.state import initial_state
from coordinare.services.closed_pr import CLOSED_PR_REASON_PREFIX
from coordinare.session import create_session_from_card, session_to_state


def monitor_state():
    card = {"id": "card", "issue_id": "issue", "status": "IN_REVIEW",
            "pr_node_id": "PR1", "pr_url": "https://github.com/acme/repo/pull/1"}
    session = create_session_from_card(card)
    session.update(phase="monitoring_pr", pipeline_admitted=True)
    state = initial_state()
    state.update(active_sessions={"card": session}, active_card_id="card")
    session_to_state(session, state)
    github = SimpleNamespace(
        get_pr_review_context=AsyncMock(return_value={"state": "CLOSED", "reviews": [], "review_decision": "APPROVED"}),
        move_card=AsyncMock(side_effect=RuntimeError("board unavailable")), merge_pr=AsyncMock(),
    )
    state["github_service"] = github
    return state, github


@pytest.mark.asyncio
@pytest.mark.parametrize("confirmed", [True, False])
@pytest.mark.parametrize("gate_phase", ["dispatching", "monitoring_pr"])
async def test_reopened_same_pr_stale_board_cannot_bypass_durable_resume_guard(monkeypatch, confirmed, gate_phase):
    state, github = monitor_state()
    side = SimpleNamespace(stop_session_confirmed=AsyncMock(return_value=confirmed))
    main = SimpleNamespace(stop_session_confirmed=AsyncMock(return_value=confirmed))
    state["agent_dispatch"] = {"session_id": "main-owned", "performer_id": "worker"}
    state["performer_services_by_id"] = {"worker": main}
    state["documenting_side"] = {"status": "running", "writer_active": True,
                               "session_id": "side-owned", "blueprint_hash": "bp"}
    state["performer_services"] = {"documenting": side}
    gate = AsyncMock(return_value=({"phase": gate_phase}, True))
    monkeypatch.setattr("coordinare.graph.nodes.monitor_performer._evaluate_pr_checks_gate", gate)
    await monitor_pr(state)
    reason = state["system_error_reason"]
    github.get_pr_review_context.return_value["state"] = "OPEN"
    # Board-write failure or an older board cache routes the parked card here.
    state["current_card"]["status"] = "IN_REVIEW"
    state["phase"] = "monitoring_pr"
    await monitor_pr(state)
    gate.assert_not_awaited()
    github.merge_pr.assert_not_awaited()
    assert state["phase"] == ("blocked" if confirmed else "monitoring_performer")
    assert state["current_card"]["status"] == "BLOCKED"
    assert state["system_error_reason"] == reason
    assert state["active_sessions"]["card"]["system_error_reason"] == reason
    if not confirmed:
        assert state["agent_dispatch"]["session_id"] == "main-owned"
        assert state["documenting_side"]["session_id"] == "side-owned"
        assert state["board_paused"] and state["pipeline_admitted"]
    else:
        assert state["agent_dispatch"] == {}
        assert not state["board_paused"] and not state["pipeline_admitted"]


@pytest.mark.asyncio
async def test_explicit_todo_reopen_clears_guard_before_monitor_can_gate(monkeypatch):
    state, github = monitor_state()
    gate = AsyncMock(return_value=({"phase": "monitoring_pr"}, True))
    monkeypatch.setattr("coordinare.graph.nodes.monitor_performer._evaluate_pr_checks_gate", gate)
    await monitor_pr(state)
    github.move_card.side_effect = None
    github.get_pr_review_context.return_value["state"] = "OPEN"
    await _reset_and_rehydrate(state, {}, ["card"], 1, state["active_sessions"])
    assert not state["active_sessions"]["card"]["system_error_reason"]
    await monitor_pr(state)
    gate.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("live_state", ["MERGED", "OPEN"])
async def test_monitor_guard_does_not_intercept_merged_or_unrelated_pr(monkeypatch, live_state):
    state, github = monitor_state()
    identity = state["current_card"]["pr_url"] if live_state == "MERGED" else "https://github.com/acme/repo/pull/2"
    reason = f"{CLOSED_PR_REASON_PREFIX} {identity}. Reopen this PR and move the card to Todo to resume review."
    state["system_error_reason"] = reason
    state["active_sessions"]["card"]["system_error_reason"] = reason
    github.get_pr_review_context.return_value["state"] = live_state
    gate = AsyncMock(return_value=({"phase": "monitoring_pr"}, True))
    monkeypatch.setattr("coordinare.graph.nodes.monitor_performer._evaluate_pr_checks_gate", gate)
    await monitor_pr(state)
    gate.assert_awaited_once()
