"""Human controls must reach one owning session and survive concurrent ticks."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from types import SimpleNamespace

import pytest

from coordinare.daemon import (
    ELIGIBLE,
    SessionEligibility,
    _FanoutContext,
    _invoke_session_tick,
    _merge_fanout_results,
    _persist_one_session,
)
from coordinare.graph.nodes.monitor.verdict import _advance_stage, _apply_pending_override
from coordinare.session import create_session_from_card, session_to_state
from tests.unit.test_dashboard import _make_app, _make_mock_daemon

CONTROLS = [
    ("/api/restart-from/assessor", "restart"),
    ("/api/skip-role", "skip"),
    ("/api/veto", "veto"),
]


def control_daemon(phase: str):
    daemon = _make_mock_daemon(phase=phase)
    daemon.state["lifecycle_sequence"] = ["assessing", "implementing", "reviewing", "qa", "closing_review"]
    return daemon


def live_session(card_id: str = "card-a") -> dict:
    session = dict(create_session_from_card({"id": card_id, "title": "Synthetic story"}))
    session.update(
        phase="monitoring_performer",
        agent_dispatch={"session_id": "worker-" + card_id},
    )
    return session


@pytest.mark.parametrize("flat_phase", ["blocked", "monitoring_performer"])
@pytest.mark.parametrize(("path", "action"), CONTROLS)
def test_control_targets_the_only_live_session(flat_phase: str, path: str, action: str) -> None:
    daemon = control_daemon(phase=flat_phase)
    live = live_session()
    paused = live_session("paused")
    paused.update(phase="blocked", board_paused=True, agent_dispatch={})
    before_paused = deepcopy(paused)
    daemon.state["active_sessions"] = {"card-a": live, "paused": paused}
    response = _make_app(daemon=daemon).post(path)
    assert response.status_code == 200
    assert live["pending_override"]["action"] == action
    if action == "restart":
        assert live["pending_override"]["target_stage"] == "assessing"
    assert not daemon.state.get("pending_override")
    assert paused == before_paused
    persisted = _persist_one_session("card-a", live)
    assert persisted.model_validate_json(persisted.model_dump_json()).pending_override == live["pending_override"]


@pytest.mark.parametrize(("path", "action"), CONTROLS)
def test_ambiguous_controls_require_an_explicit_card(path: str, action: str) -> None:
    daemon = control_daemon(phase="monitoring_performer")
    daemon.state["lifecycle_sequence"] = ["assessing", "implementing"]
    first, second = live_session(), live_session("card-b")
    daemon.state["active_sessions"] = {"card-a": first, "card-b": second}
    client = _make_app(daemon=daemon)
    before = deepcopy(daemon.state)
    assert client.post(path).status_code == 409
    assert daemon.state == before
    assert client.post(path, params={"card_id": "card-b"}).status_code == 200
    assert not first["pending_override"]
    assert second["pending_override"]["action"] == action
    assert not daemon.state.get("pending_override")


@pytest.mark.parametrize(("path", "_action"), CONTROLS)
@pytest.mark.parametrize("target", ["missing", "paused", "stale"])
def test_invalid_explicit_targets_never_fall_back_to_a_live_sibling(path: str, _action: str, target: str) -> None:
    daemon = control_daemon(phase="monitoring_performer")
    daemon.state["lifecycle_sequence"] = ["assessing", "implementing"]
    live, paused, stale = live_session(), live_session("paused"), live_session("stale")
    paused["board_paused"] = True
    stale["current_card"] = None
    daemon.state["active_sessions"] = {"card-a": live, "paused": paused, "stale": stale}
    before = deepcopy(daemon.state)
    response = _make_app(daemon=daemon).post(path, params={"card_id": target})
    assert response.status_code == 400
    assert daemon.state == before


@pytest.mark.parametrize(("path", "action"), CONTROLS)
def test_control_finds_the_owning_symphony_while_another_is_swapped_in(path: str, action: str) -> None:
    daemon = control_daemon(phase="blocked")
    live = live_session()
    other = live_session("paused")
    other.update(phase="blocked", board_paused=True)
    daemon.state.update(
        active_sessions={"paused": other}, current_symphony="other",
        symphony_states={
            "owner": SimpleNamespace(active_sessions={"card-a": live}),
            "other": SimpleNamespace(active_sessions={"paused": other}),
        },
    )
    before_other = deepcopy(other)
    response = _make_app(daemon=daemon).post(path)
    assert response.status_code == 200
    assert live["pending_override"]["action"] == action
    assert other == before_other


def test_restart_rejects_roles_outside_the_configured_lifecycle_before_mutating() -> None:
    daemon = control_daemon(phase="monitoring_performer")
    live = live_session()
    daemon.state["active_sessions"] = {"card-a": live}
    before = deepcopy(daemon.state)
    response = _make_app(daemon=daemon).post("/api/restart-from/architect")
    assert response.status_code == 400
    assert daemon.state == before


@pytest.mark.asyncio
@pytest.mark.parametrize(("path", "action"), CONTROLS)
@pytest.mark.parametrize("repeat", [False, True])
async def test_control_accepted_after_tick_snapshot_survives_and_is_consumed_once(path: str, action: str, repeat: bool) -> None:
    daemon = control_daemon(phase="blocked")
    live = live_session()
    sessions = {"card-a": live}
    daemon.state["active_sessions"] = sessions
    client = _make_app(daemon=daemon)
    if repeat:
        assert client.post(path).status_code == 200
    entered, finish = asyncio.Event(), asyncio.Event()

    class Graph:
        async def ainvoke(self, state):
            entered.set()
            await finish.wait()
            _apply_pending_override(state)
            return state

    eligibility = {"card-a": SessionEligibility("card-a", True, ELIGIBLE)}
    context = _FanoutContext(daemon.state, sessions, eligibility, set(), Graph(), asyncio.Semaphore(1))
    task = asyncio.create_task(_invoke_session_tick(context, "card-a", live))
    try:
        await entered.wait()
        assert client.post(path).status_code == 200
        accepted = deepcopy(live["pending_override"])
    finally:
        finish.set()
    result = await task
    assert result.ok
    await _merge_fanout_results(daemon.state, sessions, eligibility, [result])
    assert sessions["card-a"]["pending_override"] == accepted
    # The next genuine tick consumes this request. The stale result above
    # cannot erase it; nor can the merge resurrect it after consumption.
    context = _FanoutContext(daemon.state, sessions, eligibility, set(), Graph(), asyncio.Semaphore(1))
    result = await _invoke_session_tick(context, "card-a", sessions["card-a"])
    await _merge_fanout_results(daemon.state, sessions, eligibility, [result])
    final = sessions["card-a"]
    if action == "restart":
        assert final["performer_stage"] == "assessing"
        assert final["pending_override"]["applied"] is True
        flat = {**final}
        assert _apply_pending_override(flat) is None
    else:
        assert final["pending_override"] is None
        assert final["phase"] == ("blocked" if action == "veto" else "dispatching")
        if action == "skip":
            assert final["performer_stage"] == ("qa" if repeat else "reviewing")


@pytest.mark.asyncio
@pytest.mark.parametrize(("path", "action"), CONTROLS)
async def test_sibling_graph_mutation_cannot_erase_a_new_control(path: str, action: str) -> None:
    daemon = control_daemon(phase="blocked")
    first, second = live_session(), live_session("card-b")
    sessions = {"card-a": first, "card-b": second}
    daemon.state["active_sessions"] = sessions
    entered, finish = asyncio.Event(), asyncio.Event()

    class Graph:
        async def ainvoke(self, state):
            entered.set()
            await finish.wait()
            state["active_sessions"]["card-b"]["phase"] = "blocked"
            return state

    eligibility = {"card-a": SessionEligibility("card-a", True, ELIGIBLE)}
    context = _FanoutContext(daemon.state, sessions, eligibility, set(), Graph(), asyncio.Semaphore(1))
    task = asyncio.create_task(_invoke_session_tick(context, "card-a", first))
    try:
        await entered.wait()
        response = _make_app(daemon=daemon).post(path, params={"card_id": "card-b"})
        assert response.status_code == 200
        accepted = deepcopy(second["pending_override"])
    finally:
        finish.set()
    result = await task
    assert result.ok
    await _merge_fanout_results(daemon.state, sessions, eligibility, [result])
    assert sessions["card-b"]["phase"] == "blocked"
    assert sessions["card-b"]["pending_override"] == accepted
    assert sessions["card-b"]["pending_override"]["action"] == action
    assert not sessions["card-a"]["pending_override"]


@pytest.mark.parametrize(("role", "stage", "next_stage"), [
    ("assessor", "assessing", "implementing"),
    ("reviewer", "reviewing", "qa"),
    ("qa", "qa", "closing_review"),
])
def test_explicit_restart_uses_configured_continuation(role: str, stage: str, next_stage: str) -> None:
    daemon = control_daemon(phase="blocked")
    live = live_session()
    live["lifecycle_continuation"] = ["implementing", "reviewing"]
    daemon.state["active_sessions"] = {"card-a": live}
    response = _make_app(daemon=daemon).post("/api/restart-from/" + role)
    assert response.status_code == 200
    flat = dict(daemon.state)
    session_to_state(live, flat)
    _apply_pending_override(flat)
    assert flat["performer_stage"] == stage
    advanced = _advance_stage(flat)
    assert advanced["phase"] == "dispatching"
    assert advanced["performer_stage"] == next_stage
    assert flat["lifecycle_continuation"] == []


def test_invalid_restart_retains_the_existing_continuation() -> None:
    daemon = control_daemon(phase="monitoring_performer")
    daemon.state.update(
        performer_stage="implementing", lifecycle_continuation=["implementing", "reviewing"],
        pending_override={"action": "restart", "target_stage": "unknown"},
    )
    _apply_pending_override(daemon.state)
    assert daemon.state["performer_stage"] == "implementing"
    assert daemon.state["lifecycle_continuation"] == ["implementing", "reviewing"]
