from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from langgraph.graph import END, START, StateGraph

from coordinare.daemon import CoordinareDaemon, _persist_one_session, _restored_session_dict
from coordinare.graph.nodes.check_board import check_board
from coordinare.graph.nodes.monitor_performer import monitor_performer
from coordinare.graph.nodes.monitor_pr import monitor_pr
from coordinare.graph.state import CoordinareState, initial_state
from coordinare.session import create_session_from_card, session_to_state, state_to_session
from coordinare.state_store import PersistedSession, WorkflowSnapshot

CARD = {"id": "card", "issue_id": "issue", "issue_number": 23, "status": "IN_PROGRESS"}
PR = {"pr_url": "https://github.com/acme/repo/pull/25", "pr_node_id": "PR25",
      "pr_number": 25, "head_after": "head", "pushed_branch": "coordinare/card/test"}


class CompletedWorker:
    def __init__(self):
        self.live = True
        self.polls = 0

    def has_live_session(self, _session_id):
        return self.live

    async def check_status(self, _session_id, **_kwargs):
        self.polls += 1
        self.live = False
        return {"status": "pr_opened", **PR}


def board():
    return {"snapshot": {"IN_PROGRESS": ["card"]}, "titles": {"card": "Normalization"},
            "issue_numbers": {"card": 23}, "content_node_ids": {"card": "issue"}}


@pytest.mark.asyncio
@pytest.mark.parametrize("restart", [False, True])
async def test_actual_daemon_pending_final_checks_do_not_redispatch_completed_worker(monkeypatch, restart):
    worker = CompletedWorker()
    github = SimpleNamespace(poll_board=AsyncMock(return_value=board()), move_card=AsyncMock())
    gate = AsyncMock(side_effect=[({"phase": "monitoring_performer"}, True), ({}, False)])
    monkeypatch.setattr("coordinare.graph.nodes.monitor.body._evaluate_pr_checks_gate", gate)
    dispatched = []

    async def route(state):
        if state.get("phase") == "dispatching":
            dispatched.append(state["current_card"]["id"])
            return state
        if state.get("phase") == "monitoring_performer":
            return await monitor_performer(state)
        return state

    builder = StateGraph(CoordinareState)
    builder.add_node("board", check_board)
    builder.add_node("route", route)
    builder.add_edge(START, "board")
    builder.add_edge("board", "route")
    builder.add_edge("route", END)
    daemon = CoordinareDaemon(builder.compile())
    session = create_session_from_card(dict(CARD))
    session.update(phase="monitoring_performer", agent_dispatch={"session_id": "finished"},
                   pipeline_admitted=True)
    daemon._state.update(active_sessions={"card": session}, current_card=session["current_card"],
                         active_card_id="card", github_service=github,
                         performer_services={"implementing": worker}, lifecycle_sequence=["implementing"])
    await daemon._invoke_multi_session()
    held = daemon._state["active_sessions"]["card"]
    assert held["phase"] == "monitoring_performer"
    assert held["current_card"]["pr_node_id"] == "PR25"
    assert held["current_card"]["status"] == "IN_PROGRESS"
    github.move_card.assert_not_awaited()
    if restart:
        saved = WorkflowSnapshot.model_validate_json(daemon._build_snapshot().model_dump_json())
        daemon._restore_from_snapshot(saved)
    await daemon._invoke_multi_session()
    assert dispatched == []
    assert worker.polls == 1
    assert gate.await_count == 2
    assert daemon._state["active_sessions"]["card"]["phase"] == "monitoring_pr"
    github.move_card.assert_awaited_with("card", "IN_REVIEW")


@pytest.mark.parametrize("focused", [True, False])
def test_pr_identity_survives_json_even_when_card_is_unfocused(focused):
    card = {**CARD, **PR, "description": "private issue details"}
    session = create_session_from_card(card)
    session.update(phase="monitoring_pr", pr_artefacts_recorded_at=datetime.now(UTC))
    saved = _persist_one_session("card", session)
    restored = _restored_session_dict(
        "card", PersistedSession.model_validate_json(saved.model_dump_json()),
        WorkflowSnapshot(snapshot_at=datetime.now(UTC), phase="monitoring_pr",
                         active_card_id="card" if focused else "sibling"),
        {"id": "card"} if focused else None,
    )
    assert {k: restored["current_card"][k] for k in PR} == PR
    assert restored["pr_artefacts_recorded_at"] == session["pr_artefacts_recorded_at"]
    assert "private issue details" not in saved.model_dump_json()


@pytest.mark.asyncio
async def test_unfocused_restored_closed_pr_parks_before_checks(monkeypatch):
    session = create_session_from_card({**CARD, **PR})
    session.update(phase="monitoring_pr", pr_comment_tracking={"pr_node_id": "PR25"})
    saved = PersistedSession.model_validate_json(_persist_one_session("card", session).model_dump_json())
    restored = _restored_session_dict("card", saved,
        WorkflowSnapshot(snapshot_at=datetime.now(UTC), phase="idle"), None)
    state = initial_state()
    session_to_state(restored, state)
    github = SimpleNamespace(
        get_pr_review_context=AsyncMock(return_value={"state": "CLOSED", "reviews": []}),
        find_open_pr_for_issue=AsyncMock(return_value=None), move_card=AsyncMock(), merge_pr=AsyncMock())
    state.update(active_sessions={"card": restored}, active_card_id="card", github_service=github)
    gate = AsyncMock(return_value=({}, False))
    monkeypatch.setattr("coordinare.graph.nodes.monitor_performer._evaluate_pr_checks_gate", gate)
    await monitor_pr(state)
    assert state["phase"] == "blocked"
    assert "closed without merging" in state["system_error_reason"]
    gate.assert_not_awaited()
    github.merge_pr.assert_not_awaited()
    assert state_to_session(state)["current_card"]["pr_node_id"] == "PR25"


def test_pending_gate_pause_resumes_gate_only_without_worker():
    from coordinare.daemon import _capture_board_pause_resume_phase, _resume_board_paused_session

    session = {"phase": "monitoring_performer", "agent_dispatch": {},
               "pending_pr_handoff": {"stage": "implementing"}}
    _capture_board_pause_resume_phase(session)
    session.update(board_paused=True, phase="blocked")
    _resume_board_paused_session(session)
    assert session["phase"] == "monitoring_performer"
    assert not session["reconciled_dispatch_pending"]


def test_pr_identity_and_handoff_changes_trigger_snapshot_save():
    from coordinare.daemon import _recovery_signature

    before = {"phase": "monitoring_pr", "current_card": dict(CARD)}
    after = {**before, "current_card": {**CARD, **PR}}
    assert _recovery_signature(before) != _recovery_signature(after)
    handoff = {**after, "pending_pr_handoff": {"stage": "implementing"}}
    assert _recovery_signature(after) != _recovery_signature(handoff)


@pytest.mark.asyncio
async def test_pending_handoff_closed_pr_parks_before_ci_or_worker(monkeypatch):
    state = initial_state()
    state.update(current_card={**CARD, **PR}, phase="monitoring_performer",
                 performer_stage="implementing", lifecycle_sequence=["implementing"],
                 pending_pr_handoff={"stage": "implementing"}, agent_dispatch={})
    state["active_sessions"] = {"card": state_to_session(state)}
    state["active_card_id"] = "card"
    state["github_service"] = SimpleNamespace(
        get_pr_review_context=AsyncMock(return_value={"state": "CLOSED", "reviews": []}),
        move_card=AsyncMock())
    gate = AsyncMock(return_value=({}, False))
    monkeypatch.setattr("coordinare.graph.nodes.monitor.body._evaluate_pr_checks_gate", gate)
    await monitor_performer(state)
    assert state["phase"] == "blocked"
    gate.assert_not_awaited()
    assert state["pending_pr_handoff"] is None
    assert state["active_sessions"]["card"]["pending_pr_handoff"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("pending_handoff", [False, True])
async def test_actual_board_cycle_restores_closed_pr_and_reopens_same_pr_without_dispatch(monkeypatch, pending_handoff):
    session = create_session_from_card({**CARD, **PR})
    session.update(phase="monitoring_pr", pipeline_admitted=True,
                   pending_pr_handoff={"stage": "implementing"} if pending_handoff else None)
    snapshot = WorkflowSnapshot(snapshot_at=datetime.now(UTC), phase="idle",
                                active_sessions={"card": _persist_one_session("card", session)})
    snapshot = WorkflowSnapshot.model_validate_json(snapshot.model_dump_json())
    restored = _restored_session_dict("card", snapshot.active_sessions["card"], snapshot, None)
    live_board = board()
    live_board["snapshot"] = {"IN_REVIEW": ["card"]}
    context = {"state": "CLOSED", "reviews": []}
    github = SimpleNamespace(poll_board=AsyncMock(return_value=live_board),
                             get_pr_review_context=AsyncMock(return_value=context), move_card=AsyncMock())
    gate = AsyncMock(return_value=({"phase": "monitoring_pr"}, True))
    monkeypatch.setattr("coordinare.graph.nodes.monitor_performer._evaluate_pr_checks_gate", gate)
    dispatched = []

    async def route(state):
        if state.get("phase") == "dispatching":
            dispatched.append(state["current_card"]["id"])
        elif state.get("phase") == "monitoring_pr":
            return await monitor_pr(state)
        return state

    builder = StateGraph(CoordinareState)
    builder.add_node("board", check_board)
    builder.add_node("route", route)
    builder.add_edge(START, "board")
    builder.add_edge("board", "route")
    builder.add_edge("route", END)
    daemon = CoordinareDaemon(builder.compile())
    daemon._state.update(active_sessions={"card": restored}, github_service=github,
                         current_card=restored["current_card"], active_card_id="card")
    await daemon._invoke_multi_session()
    blocked = daemon._state["active_sessions"]["card"]
    assert blocked["phase"] == "blocked"
    assert "closed without merging" in blocked["system_error_reason"]
    gate.assert_not_awaited()
    context["state"] = "OPEN"
    live_board["snapshot"] = {"TODO": ["card"]}
    daemon._state["board_snapshot"] = live_board["snapshot"]
    await daemon._reconcile_board_pauses()
    await daemon._invoke_multi_session()
    assert dispatched == []
    resumed = daemon._state["active_sessions"]["card"]
    assert resumed["phase"] == "monitoring_pr"
    assert resumed["current_card"]["pr_node_id"] == "PR25"
    assert not resumed["system_error_reason"]


def test_handoff_json_restore_keeps_gate_wait_without_runtime_or_private_card_text():
    session = create_session_from_card({**CARD, **PR, "description": "private issue text"})
    session.update(phase="monitoring_performer", agent_dispatch={},
                   pending_pr_handoff={"stage": "implementing", "completed_at": datetime.now(UTC).isoformat()},
                   dispatched_feedback={"stage": "implementing", "items": [{"body": "Original request"}]})
    saved = _persist_one_session("card", session)
    restored = _restored_session_dict("card", PersistedSession.model_validate_json(saved.model_dump_json()),
        WorkflowSnapshot(snapshot_at=datetime.now(UTC), phase="idle"), None)
    assert restored["phase"] == "monitoring_performer"
    assert not restored["reconciled_dispatch_pending"]
    assert restored["pending_pr_handoff"] == session["pending_pr_handoff"]
    assert restored["dispatched_feedback"] == session["dispatched_feedback"]
    assert "private issue text" not in saved.model_dump_json()


def test_schema31_contract_and_older_snapshot_defaults():
    import json
    from pathlib import Path

    from coordinare.state_store import CURRENT_SCHEMA_VERSION

    contract = json.loads(Path("specs/003-state-persistence/contracts/workflow-snapshot.schema.json").read_text())
    properties = contract["properties"]["active_sessions"]["additionalProperties"]["properties"]
    assert {"pending_pr_handoff", "pr_artefacts", "pr_artefacts_recorded_at"} <= set(properties)
    assert CURRENT_SCHEMA_VERSION == 34
    assert contract["properties"]["schema_version"]["enum"] == list(range(1, 35))
    for version in range(1, 31):
        snapshot = WorkflowSnapshot.model_validate({"schema_version": version,
            "snapshot_at": datetime.now(UTC), "phase": "idle", "active_sessions": {"card": {"card_id": "card"}}})
        assert snapshot.active_sessions["card"].pending_pr_handoff is None
        assert snapshot.active_sessions["card"].pr_artefacts == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("action,phase", [("veto", "blocked"), ("restart", "dispatching"), ("skip", "monitoring_pr")])
async def test_new_human_override_precedes_pending_handoff(monkeypatch, action, phase):
    state = initial_state()
    state.update(current_card={**CARD, **PR}, phase="monitoring_performer",
                 performer_stage="implementing", lifecycle_sequence=["implementing"],
                 pending_pr_handoff={"stage": "implementing"}, agent_dispatch={},
                 pending_override={"action": action, "target_stage": "implementing"},
                 github_service=SimpleNamespace(move_card=AsyncMock()))
    gate = AsyncMock(return_value=({}, False))
    monkeypatch.setattr("coordinare.graph.nodes.monitor.body._evaluate_pr_checks_gate", gate)
    await monitor_performer(state)
    assert state["phase"] == phase
    assert state["pending_pr_handoff"] is None
    gate.assert_not_awaited()
    if action == "restart":
        assert state["pending_override"]["applied"]
    else:
        assert state["pending_override"] is None
    if action != "skip":
        state["github_service"].move_card.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("restart", ["none", "before_pause", "after_resume"])
@pytest.mark.parametrize("repause", [False, True])
async def test_actual_pending_handoff_board_pause_and_resume(monkeypatch, restart, repause):
    live_board = board()
    live_board["snapshot"] = {"BACKLOG": ["card"], "IN_REVIEW": ["sibling"]}
    github = SimpleNamespace(poll_board=AsyncMock(return_value=live_board), move_card=AsyncMock())
    holds = 3 if repause else 1
    gate = AsyncMock(side_effect=[({"phase": "monitoring_performer"}, True)] * holds + [({}, False)])
    monkeypatch.setattr("coordinare.graph.nodes.monitor.body._evaluate_pr_checks_gate", gate)
    dispatched = []

    async def route(state):
        if state["current_card"]["id"] == "card":
            if state["phase"] == "dispatching":
                dispatched.append("card")
            elif state["phase"] == "monitoring_performer":
                return await monitor_performer(state)
        return state

    builder = StateGraph(CoordinareState)
    builder.add_node("board", check_board)
    builder.add_node("route", route)
    builder.add_edge(START, "board")
    builder.add_edge("board", "route")
    builder.add_edge("route", END)
    daemon = CoordinareDaemon(builder.compile())
    session = create_session_from_card({**CARD, **PR})
    session.update(phase="monitoring_performer", pipeline_admitted=True,
                   pending_pr_handoff={"stage": "implementing"}, agent_dispatch={})
    sibling = create_session_from_card({"id": "sibling", "status": "IN_REVIEW"})
    sibling.update(phase="monitoring_pr", pipeline_admitted=True)
    daemon._state.update(active_sessions={"card": session, "sibling": sibling},
                         current_card=session["current_card"], active_card_id="card",
                         github_service=github, lifecycle_sequence=["implementing"],
                         config=SimpleNamespace(max_concurrent_cards=2))
    if restart == "before_pause":
        saved = WorkflowSnapshot.model_validate_json(daemon._build_snapshot().model_dump_json())
        daemon._restore_from_snapshot(saved)
        daemon._reconcile_sessions_with_board(live_board["snapshot"], saved)
        assert daemon._state["active_sessions"]["card"]["board_paused"]
    await daemon._invoke_multi_session()
    assert daemon._state["active_sessions"]["card"]["board_paused"]
    gate.assert_not_awaited()
    live_board["snapshot"] = {"TODO": ["card"], "IN_REVIEW": ["sibling"]}
    await daemon._invoke_multi_session()
    assert not daemon._state["active_sessions"]["card"]["board_paused"]
    assert gate.await_count == 1
    if restart == "after_resume":
        saved = WorkflowSnapshot.model_validate_json(daemon._build_snapshot().model_dump_json())
        daemon._restore_from_snapshot(saved)
        daemon._reconcile_sessions_with_board(live_board["snapshot"], saved)
    if repause:
        live_board["snapshot"] = {"IN_PROGRESS": ["card"], "IN_REVIEW": ["sibling"]}
        await daemon._invoke_multi_session()
        assert gate.await_count == 2
        live_board["snapshot"] = {"TODO": ["card"], "IN_REVIEW": ["sibling"]}
        await daemon._invoke_multi_session()
        assert daemon._state["active_sessions"]["card"]["board_paused"]
        assert gate.await_count == 2
        live_board["snapshot"] = {"IN_PROGRESS": ["card"], "IN_REVIEW": ["sibling"]}
        await daemon._invoke_multi_session()
        assert gate.await_count == 3
    await daemon._invoke_multi_session()
    assert gate.await_count == holds + 1
    assert daemon._state["active_sessions"]["card"]["phase"] == "monitoring_pr"
    assert dispatched == []


@pytest.mark.asyncio
async def test_pending_handoff_ci_bounce_invalidates_completed_turn(monkeypatch):
    state = initial_state()
    state.update(current_card={**CARD, **PR}, phase="monitoring_performer",
                 performer_stage="implementing", lifecycle_sequence=["implementing"],
                 pending_pr_handoff={"stage": "implementing"}, agent_dispatch={})
    monkeypatch.setattr("coordinare.graph.nodes.monitor.body._evaluate_pr_checks_gate",
                        AsyncMock(return_value=({"phase": "dispatching"}, True)))
    await monitor_performer(state)
    assert state["phase"] == "dispatching"
    assert state["pending_pr_handoff"] is None
