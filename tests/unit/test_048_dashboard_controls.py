"""Human controls must reach one owning session and survive concurrent ticks."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from coordinare.daemon import (
    ELIGIBLE,
    CoordinareDaemon,
    SessionEligibility,
    _FanoutContext,
    _invoke_session_tick,
    _invoke_single_graph,
    _merge_fanout_results,
    _persist_one_session,
)
from coordinare.graph.builder import CoordinareGraphBuilder
from coordinare.graph.nodes.check_board import (
    _ensure_active_card_id,
    _pickup_todo_cards,
    check_board,
)
from coordinare.graph.nodes.dispatch_performer import dispatch_performer
from coordinare.graph.nodes.monitor.verdict import _advance_stage, _apply_pending_override
from coordinare.graph.state import SymphonyRuntimeState, _retire_active_session
from coordinare.session import create_session_from_card, session_to_state
from tests.unit.test_dashboard import _make_app, _make_mock_daemon

CONTROLS = [
    ("/api/restart-from/assessor", "restart"),
    ("/api/skip-role", "skip"),
    ("/api/veto", "veto"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize(("path", "action"), CONTROLS)
@pytest.mark.parametrize("repeat", [False, True])
async def test_flat_fallback_preserves_new_request_across_graph_copy(path, action, repeat):
    entered, release = asyncio.Event(), asyncio.Event()

    class Graph:
        async def ainvoke(self, state):
            updated = dict(state)
            updated["pending_override"] = deepcopy(state.get("pending_override"))
            _apply_pending_override(updated)
            entered.set()
            await release.wait()
            return updated

    daemon = CoordinareDaemon(Graph())
    daemon.state.update(
        phase="monitoring_performer", performer_stage="implementing",
        current_card={"id": "legacy"},
        lifecycle_sequence=["assessing", "implementing", "reviewing", "qa"],
    )
    client = _make_app(daemon=daemon)
    if repeat:
        assert client.post(path).status_code == 200
    first = deepcopy(daemon.state.get("pending_override"))
    task = asyncio.create_task(daemon._invoke_multi_session())
    await asyncio.wait_for(entered.wait(), 3)
    response = client.post(path)
    accepted = deepcopy(daemon.state["pending_override"])
    release.set()
    await task
    assert response.status_code == 200
    expected = {"status": "override_queued", "action": action}
    if action == "restart":
        expected["target_stage"] = "assessing"
    assert response.json() == expected
    assert daemon.state["pending_override"] == accepted
    assert isinstance(accepted.get("control_id"), str)
    if first:
        assert first["control_id"] != accepted["control_id"]
    assert _apply_pending_override(daemon.state) is not None
    assert _apply_pending_override(daemon.state) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(("path", "action"), CONTROLS)
async def test_flat_fallback_does_not_replay_command_consumed_inside_graph(path, action):
    daemon = CoordinareDaemon(None)
    daemon.state.update(
        phase="monitoring_performer", performer_stage="implementing",
        current_card={"id": "legacy"},
        lifecycle_sequence=["assessing", "implementing", "reviewing", "qa"],
    )
    assert _make_app(daemon=daemon).post(path).status_code == 200

    async def tick(state):
        updated = dict(state)
        updated["pending_override"] = deepcopy(state["pending_override"])
        _apply_pending_override(updated)
        return updated

    updated = await _invoke_single_graph(daemon.state, SimpleNamespace(ainvoke=tick))
    expected = "assessing" if action == "restart" else "reviewing" if action == "skip" else "implementing"
    assert updated["performer_stage"] == expected
    assert _apply_pending_override(updated) is None


@pytest.mark.asyncio
@pytest.mark.parametrize(("path", "_action"), CONTROLS)
@pytest.mark.parametrize("next_card", [None, {"id": "other"}])
async def test_flat_fallback_does_not_transfer_late_control_to_a_new_card(path, _action, next_card):
    entered, release = asyncio.Event(), asyncio.Event()

    class Graph:
        async def ainvoke(self, state):
            updated = dict(state)
            entered.set()
            await release.wait()
            updated.update(current_card=next_card, pending_override=None, phase="idle")
            return updated

    daemon = CoordinareDaemon(Graph())
    daemon.state.update(phase="monitoring_performer", current_card={"id": "legacy"},
                        lifecycle_sequence=["assessing", "implementing"])
    task = asyncio.create_task(daemon._invoke_multi_session())
    try:
        await asyncio.wait_for(entered.wait(), 3)
        assert _make_app(daemon=daemon).post(path).status_code == 200
    finally:
        release.set()
        await task
    assert daemon.state["current_card"] == next_card
    assert daemon.state["pending_override"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(("path", "action"), CONTROLS)
async def test_flat_fallback_does_not_replay_late_control_consumed_inside_graph(path, action):
    entered, release = asyncio.Event(), asyncio.Event()

    class Graph:
        async def ainvoke(self, state):
            updated = dict(state)
            entered.set()
            await release.wait()
            updated["pending_override"] = deepcopy(state["pending_override"])
            _apply_pending_override(updated)
            return updated

    daemon = CoordinareDaemon(Graph())
    daemon.state.update(phase="monitoring_performer", performer_stage="implementing",
                        current_card={"id": "legacy"},
                        lifecycle_sequence=["assessing", "implementing", "reviewing", "qa"])
    task = asyncio.create_task(daemon._invoke_multi_session())
    try:
        await asyncio.wait_for(entered.wait(), 3)
        assert _make_app(daemon=daemon).post(path).status_code == 200
    finally:
        release.set()
        await task
    assert daemon.state["performer_stage"] == (
        "assessing" if action == "restart" else "reviewing" if action == "skip" else "implementing"
    )
    assert _apply_pending_override(daemon.state) is None


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


@pytest.mark.asyncio
@pytest.mark.parametrize(("path", "action"), CONTROLS)
@pytest.mark.parametrize("startup", [True, False])
async def test_single_graph_writeback_preserves_new_control(path: str, action: str, startup: bool) -> None:
    entered, finish = asyncio.Event(), asyncio.Event()

    class Graph:
        async def ainvoke(self, state):
            if startup:
                state = await _pickup_todo_cards(state, {"titles": {"card-a": "Synthetic story"}}, ["card-a"])
                _ensure_active_card_id(state)
            _apply_pending_override(state)
            entered.set()
            await finish.wait()
            state.update(phase="monitoring_performer", agent_dispatch={"session_id": "synthetic-worker"})
            return state

    daemon = CoordinareDaemon(Graph())
    daemon.state.update(lifecycle_sequence=["assessing", "implementing", "reviewing", "qa", "closing_review"],
                        config=SimpleNamespace(max_concurrent_cards=1), github_service=None)
    if not startup:
        daemon.state.update(active_card_id="card-a", active_sessions={"card-a": live_session()},
                            board_snapshot={"BLOCKED": ["card-a"]})
    task = asyncio.create_task(daemon._invoke_multi_session())
    try:
        await asyncio.wait_for(entered.wait(), 3)
        response = _make_app(daemon=daemon).post(path, params={"card_id": "card-a"})
        assert response.status_code == 200
        accepted = deepcopy(daemon.state["active_sessions"]["card-a"]["pending_override"])
    finally:
        finish.set()
    await task
    assert daemon.state["active_sessions"]["card-a"]["pending_override"] == accepted
    assert accepted["action"] == action


@pytest.mark.asyncio
@pytest.mark.parametrize(("path", "_action"), CONTROLS)
async def test_restore_routing_cannot_accept_a_retired_aggregate_target(path: str, _action: str) -> None:
    entered, finish = asyncio.Event(), asyncio.Event()

    class Board:
        async def poll_board(self):
            entered.set()
            await finish.wait()
            return {"snapshot": {"IN_PROGRESS": ["restored"]}}

    class Graph:
        async def ainvoke(self, state):
            if state.get("current_symphony") == "owner":
                state["phase"] = "idle"
                _retire_active_session(state, trigger="synthetic_terminal_completion")
            return state

    daemon = CoordinareDaemon(Graph())
    retired = live_session("retired")
    owner = SymphonyRuntimeState(name="owner", active_sessions={"retired": retired})
    peer = SymphonyRuntimeState(name="peer", active_sessions={})
    daemon.state.update(active_sessions={"retired": retired}, symphony_states={"owner": owner, "peer": peer},
                        symphony_github_services={"peer": Board()}, lifecycle_sequence=["assessing", "implementing"])
    await daemon._conduct_single_symphony("owner", SimpleNamespace())
    assert not owner.active_sessions
    daemon._unassigned_restored_sessions = {"restored": dict(create_session_from_card({"id": "restored"}))}
    task = asyncio.create_task(daemon._conduct_single_symphony("peer", SimpleNamespace()))
    try:
        await asyncio.wait_for(entered.wait(), 3)
        response = _make_app(daemon=daemon).post(path, params={"card_id": "retired"})
        assert response.status_code == 400
        assert retired["pending_override"] is None
    finally:
        finish.set()
        await task
    assert "retired" not in owner.active_sessions and "retired" not in peer.active_sessions


@pytest.mark.asyncio
@pytest.mark.parametrize(("path", "action"), CONTROLS)
@pytest.mark.parametrize("newer_request", [False, True])
async def test_reconciled_compiled_graph_consumes_exact_control_once(path: str, action: str, newer_request: bool) -> None:
    entered, recover = asyncio.Event(), asyncio.Event()
    consumed, finish = asyncio.Event(), asyncio.Event()

    class Docker:
        async def list_containers_by_label(self, *_args, **_kwargs):
            entered.set()
            await recover.wait()
            return []

    class GitHub:
        def __init__(self):
            self.polls = 0

        async def poll_board(self):
            self.polls += 1
            return {"snapshot": {"BLOCKED" if self.polls == 1 else "IN_PROGRESS": ["card-a"]},
                    "titles": {"card-a": "Synthetic story"}}

        async def move_card(self, *_args):
            return None

    async def passthrough(state):
        return state

    async def dispatch_leaf(state):
        state.update(phase="monitoring_performer", agent_dispatch={"session_id": "synthetic-new-worker"})
        return state

    async def notify(state):
        consumed.set()
        await finish.wait()
        return state

    graph = CoordinareGraphBuilder(node_overrides={
        "route_issue_comments": passthrough, "check_board": check_board,
        "classify_scope": passthrough, "dispatch_card": dispatch_performer,
        "notify": notify, "handle_blocked": passthrough,
    }).build()
    daemon = CoordinareDaemon(graph)
    live = live_session()
    live.update(performer_stage="implementing", system_error_reason="Synthetic prior technical failure")
    service = SimpleNamespace(_config=SimpleNamespace(mode="ephemeral"), has_live_session=lambda _sid: False)
    daemon.state.update(active_sessions={"card-a": live}, active_card_id="card-a",
                        lifecycle_sequence=["assessing", "implementing", "reviewing", "qa", "closing_review"],
                        performer_services={"implementing": service}, github_service=GitHub())
    session_to_state(live, daemon.state)
    client = _make_app(daemon=daemon)
    with patch("coordinare.services.reconciliation.DockerExecutor", return_value=Docker()), \
         patch("coordinare.graph.nodes.dispatch_performer._dispatch_performer_body", side_effect=dispatch_leaf):
        task = asyncio.create_task(daemon._invoke_multi_session())
        try:
            await asyncio.wait_for(entered.wait(), 3)
            assert client.post(path, params={"card_id": "card-a"}).status_code == 200
            first_id = live["pending_override"]["control_id"]
            recover.set()
            await asyncio.wait_for(consumed.wait(), 3)
            if newer_request:
                assert client.post(path, params={"card_id": "card-a"}).status_code == 200
                accepted = deepcopy(live["pending_override"])
                assert accepted["control_id"] != first_id
        finally:
            recover.set()
            finish.set()
            await task
    final = daemon.state["active_sessions"]["card-a"]
    assert final["performer_stage"] == ("assessing" if action == "restart" else "reviewing" if action == "skip" else "implementing")
    flat = dict(daemon.state)
    session_to_state(final, flat)
    if newer_request:
        assert final["pending_override"] == accepted
        assert _apply_pending_override(flat) is not None
    else:
        if action == "restart":
            assert final["pending_override"]["applied"] is True
            assert final["pending_override"]["control_id"] == first_id
        else:
            assert final["pending_override"] is None
        assert _apply_pending_override(flat) is None
