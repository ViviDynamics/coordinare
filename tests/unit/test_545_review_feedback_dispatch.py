from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from coordinare.graph.builder import CoordinareGraphBuilder
from coordinare.graph.state import initial_state
from coordinare.session import state_to_session


@pytest.mark.asyncio
@pytest.mark.parametrize("with_assessor", [False, True])
@pytest.mark.parametrize(
    ("refused_board_move", "stale_on_third_tick"),
    [(False, False), (True, False), (True, True)],
)
@pytest.mark.parametrize(
    ("body", "target_stage"),
    [("Fix the implementation bug.", "implementing"),
     ("Update the documentation in the README.", "documenting")],
)
async def test_review_feedback_reaches_dispatch_on_next_cycle(
    with_assessor: bool, refused_board_move: bool, stale_on_third_tick: bool,
    body: str, target_stage: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A review must reach the selected performer despite its existing open PR."""
    state = initial_state()
    state["phase"] = "monitoring_pr"
    state["lifecycle_sequence"] = (
        (["assessing"] if with_assessor else [])
        + ["implementing", "reviewing", "documenting"]
    )
    state["current_card"] = {
        "id": "card19", "issue_id": "issue19", "status": "IN_REVIEW",
        "pr_url": "https://github.com/example/sample/pull/21",
    }
    github = AsyncMock()
    github.find_pr_for_issue.return_value = {"pr_url": state["current_card"]["pr_url"]}
    state["github_service"] = github
    if refused_board_move:
        state["config"] = SimpleNamespace(max_concurrent_cards=2)
        github.poll_board.return_value = {
            "snapshot": {"IN_REVIEW": ["card19"], "IN_PROGRESS": [], "TODO": []},
            "titles": {"card19": "Sample"}, "descriptions": {"card19": ""},
            "issue_numbers": {"card19": 19}, "issue_urls": {},
            "content_node_ids": {"card19": "issue19"},
        }
        github.move_card.return_value = False
        github.fetch_main_sha.return_value = None
        state["active_card_id"] = "card19"
        state["active_sessions"] = {"card19": state_to_session(state)}
    backend = AsyncMock()
    backend.prompt.return_value = {"data": [], "text": "[]"}
    state["conducting_backend"] = backend
    review = {"id": "5456752408", "body": body, "state": "CHANGES_REQUESTED"}
    dispatched = []
    monitored = []

    async def passthrough(current):
        return current

    async def monitor(current):
        current["phase"] = "monitoring_pr"
        if "5456752408" not in current.get("processed_review_ids", set()):
            current["pending_reviews"] = [review]
        return current

    async def dispatch(current):
        dispatched.append({
            "stage": current["performer_stage"],
            "feedback": current["relay_feedback"],
            "pr_url": current["current_card"]["pr_url"],
        })
        current["phase"] = "monitoring_performer"
        current["agent_dispatch"] = {
            "session_id": "performer19" if len(dispatched) == 1 else "replacement19",
        }
        current["relay_feedback"] = []
        return current

    async def monitor_performer(current):
        monitored.append(current["agent_dispatch"]["session_id"])
        return current

    overrides = {
        "route_issue_comments": passthrough,
        "monitor_pr": monitor,
        "dispatch_card": dispatch,
        "monitor_agent": monitor_performer,
        "notify": passthrough,
    }
    if not refused_board_move:
        overrides["check_board"] = passthrough
    graph = CoordinareGraphBuilder(node_overrides=overrides).build()

    state = await graph.ainvoke(state)
    assert state["phase"] == "dispatching"
    assert state["performer_stage"] == target_stage
    assert state["processed_review_ids"] == {"5456752408"}
    assert not dispatched

    # The daemon commits the flat phase/feedback fields back to its session
    # between graph ticks; preserve that boundary in the real-board case.
    if refused_board_move:
        state["active_sessions"]["card19"] = state_to_session(state)
    state = await graph.ainvoke(state)
    assert state["phase"] == "monitoring_performer"
    assert dispatched == [{
        "stage": target_stage, "feedback": [review],
        "pr_url": "https://github.com/example/sample/pull/21",
    }]
    if refused_board_move:
        state["performer_services"] = {
            target_stage: SimpleNamespace(_config=SimpleNamespace(mode="ephemeral"), has_live_session=lambda session_id: not stale_on_third_tick),
        }
        if stale_on_third_tick:
            from coordinare.services.reconciliation import DockerExecutor

            monkeypatch.setattr(DockerExecutor, "list_containers_by_label", AsyncMock(return_value=[]))
        state["active_sessions"]["card19"] = state_to_session(state)
        state = await graph.ainvoke(state)
        assert state["phase"] == "monitoring_performer"
        if stale_on_third_tick:
            assert monitored == []
            assert len(dispatched) == 2
            assert dispatched[-1]["stage"] == target_stage
            assert dispatched[-1]["pr_url"] == "https://github.com/example/sample/pull/21"
            assert state["agent_dispatch"] == {"session_id": "replacement19"}
        else:
            assert monitored == ["performer19"]
            assert len(dispatched) == 1


@pytest.mark.asyncio
async def test_initial_assessment_with_open_pr_still_returns_to_monitoring() -> None:
    """An existing PR still supersedes stale initial assessment without feedback."""
    from coordinare.graph.nodes.assess_card import assess_card
    from coordinare.graph.routing import route_from_board_check

    state = initial_state()
    state["phase"] = "dispatching"
    state["lifecycle_sequence"] = ["implementing"]
    state["current_card"] = {
        "id": "card19", "issue_id": "issue19",
        "pr_url": "https://github.com/example/sample/pull/21",
    }
    state["github_service"] = AsyncMock()
    state["github_service"].find_pr_for_issue.return_value = {
        "pr_url": state["current_card"]["pr_url"],
    }
    state["conducting_backend"] = AsyncMock()
    state["open_questions"] = [{"question": "A stale clarification"}]

    assert route_from_board_check(state) == "assess"
    result = await assess_card(state)
    assert result["phase"] == "monitoring_pr"
    assert result["open_questions"] == []


@pytest.mark.asyncio
@pytest.mark.parametrize("command", [
    "/coordinare restart-from implementing", "/coordinare skip-reviewer", "/coordinare veto",
])
async def test_pr_command_reaches_dispatch_without_initial_assessment(command: str) -> None:
    from coordinare.graph.nodes.check_board import check_board
    from coordinare.graph.nodes.classify_human_feedback import classify_human_feedback
    from coordinare.graph.routing import route_from_board_check

    state = initial_state()
    state["phase"] = "monitoring_pr"
    state["lifecycle_sequence"] = ["implementing", "reviewing"]
    state["pending_reviews"] = [{"id": "review-command", "body": command}]
    state["current_card"] = {
        "id": "card19", "issue_id": "issue19",
        "status": "IN_REVIEW",
        "pr_url": "https://github.com/example/sample/pull/21",
    }
    state["config"] = SimpleNamespace(max_concurrent_cards=2)
    github = AsyncMock()
    github.poll_board.return_value = {
        "snapshot": {"IN_REVIEW": ["card19"], "IN_PROGRESS": [], "TODO": []},
        "titles": {"card19": "Sample"}, "descriptions": {"card19": ""},
        "issue_numbers": {"card19": 19}, "issue_urls": {},
        "content_node_ids": {"card19": "issue19"},
    }
    github.fetch_main_sha.return_value = None
    state["github_service"] = github
    result = await classify_human_feedback(state)

    assert result["pending_override"] is not None
    assert not result["relay_feedback"]
    assert route_from_board_check(result) == "dispatch"
    result["active_card_id"] = "card19"
    result["active_sessions"] = {"card19": state_to_session(result)}
    result = await check_board(result)
    assert result["phase"] == "dispatching"
    assert result["pending_override"] is not None
    assert route_from_board_check(result) == "dispatch"


@pytest.mark.asyncio
@pytest.mark.parametrize("capacity", [2, 3])
async def test_stale_sibling_reconciles_on_its_own_tick(
    capacity: int, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from coordinare.graph.nodes.check_board import check_board
    from coordinare.graph.routing import route_from_board_check
    from coordinare.services.reconciliation import DockerExecutor
    from coordinare.session import session_to_state

    monkeypatch.setattr(DockerExecutor, "list_containers_by_label", AsyncMock(return_value=[]))
    state = initial_state()
    state["config"] = SimpleNamespace(max_concurrent_cards=capacity)
    state["lifecycle_sequence"] = ["implementing", "reviewing"]
    github = AsyncMock()
    github.poll_board.return_value = {
        "snapshot": {"IN_REVIEW": ["A", "B"], "IN_PROGRESS": [], "TODO": []},
        "titles": {"A": "Live", "B": "Stale"},
        "descriptions": {}, "issue_numbers": {"A": 18, "B": 19},
        "issue_urls": {}, "content_node_ids": {},
    }
    github.fetch_main_sha.return_value = None
    state["github_service"] = github
    state["performer_services"] = {
        "implementing": SimpleNamespace(_config=SimpleNamespace(mode="ephemeral"), has_live_session=lambda sid: sid == "live-A"),
    }
    for card_id, session_id in [("A", "live-A"), ("B", "dead-B")]:
        state["current_card"] = {
            "id": card_id, "status": "IN_REVIEW",
            "pr_url": f"https://github.com/example/sample/pull/{card_id}",
        }
        state["phase"] = "monitoring_performer"
        state["performer_stage"] = "implementing"
        state["agent_dispatch"] = {"session_id": session_id}
        state["active_sessions"][card_id] = state_to_session(state)

    state["active_card_id"] = "A"
    session_to_state(state["active_sessions"]["A"], state)
    state = await check_board(state)
    assert state["phase"] == "monitoring_performer"
    # Another card's tick must not consume B's stale-session evidence.
    assert state["active_sessions"]["B"]["agent_dispatch"] == {"session_id": "dead-B"}

    state["active_card_id"] = "B"
    session_to_state(state["active_sessions"]["B"], state)
    state = await check_board(state)
    assert state["phase"] == "dispatching"
    assert state["agent_dispatch"] == {}
    assert state["performer_stage"] == "implementing"
    assert route_from_board_check(state) == "dispatch"
    assert state["current_card"]["pr_url"].endswith("/B")

    from coordinare.graph.nodes.dispatch_performer import dispatch_performer
    from coordinare.services import pipeline_budget

    monkeypatch.setattr(pipeline_budget, "dispatch_has_pipeline_slot", lambda *_: False)
    state = await dispatch_performer(state)
    assert state["phase"] == "dispatching"
    state["active_sessions"]["B"] = state_to_session(state)
    session_to_state(state["active_sessions"]["B"], state)
    state = await check_board(state)
    assert state["phase"] == "dispatching"
    assert route_from_board_check(state) == "dispatch"


@pytest.mark.asyncio
async def test_replacement_dispatch_survives_restart_and_clears_on_success() -> None:
    from datetime import UTC, datetime

    from coordinare.daemon import _persist_one_session, _restored_session_dict
    from coordinare.graph.nodes.dispatch_performer import _finalise_success
    from coordinare.graph.state import _retire_active_session
    from coordinare.session import create_session_from_card, session_to_state
    from coordinare.state_store import PersistedSession, WorkflowSnapshot

    state = initial_state()
    card = {"id": "B", "status": "IN_REVIEW"}
    state["current_card"] = card
    state["performer_stage"] = "reviewing"
    state["phase"] = "dispatching"
    state["reconciled_dispatch_pending"] = True
    session = state_to_session(state)
    persisted = _persist_one_session("B", session)
    persisted = PersistedSession.model_validate_json(persisted.model_dump_json())
    restored = _restored_session_dict("B", persisted, WorkflowSnapshot(snapshot_at=datetime.now(UTC), phase="dispatching"), card)
    state["reconciled_dispatch_pending"] = False
    session_to_state(restored, state)
    assert state["reconciled_dispatch_pending"] is True
    state = await _finalise_success(state, {"session_id": "replacement"}, {
        "card_context": {}, "performer_stage": "reviewing", "card": card,
    })
    assert state["reconciled_dispatch_pending"] is False
    assert state["phase"] == "monitoring_performer"
    assert PersistedSession(card_id="old").reconciled_dispatch_pending is False
    assert create_session_from_card(card)["reconciled_dispatch_pending"] is False
    state["active_card_id"] = "B"
    state["active_sessions"] = {"B": state_to_session(state)}
    state["reconciled_dispatch_pending"] = True
    _retire_active_session(state)
    assert state["reconciled_dispatch_pending"] is False
    state["reconciled_dispatch_pending"] = True
    session_to_state({"current_card": {"id": "legacy"}}, state)
    assert state["reconciled_dispatch_pending"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("stale", [False, True])
async def test_daemon_ticks_in_review_performer_with_eligible_sibling(
    stale: bool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from coordinare.daemon import CoordinareDaemon
    from coordinare.services.reconciliation import DockerExecutor

    monkeypatch.setattr(DockerExecutor, "list_containers_by_label", AsyncMock(return_value=[]))
    ticked = []
    dispatched = []

    async def passthrough(current):
        return current

    async def monitor(current):
        ticked.append(current["current_card"]["id"])
        return current

    async def dispatch(current):
        dispatched.append(current["current_card"]["id"])
        current["agent_dispatch"] = {"session_id": "replacement-B"}
        current["phase"] = "monitoring_performer"
        current["reconciled_dispatch_pending"] = False
        return current

    graph = CoordinareGraphBuilder(node_overrides={
        "route_issue_comments": passthrough, "notify": passthrough,
        "monitor_agent": monitor, "dispatch_card": dispatch,
    }).build()
    daemon = CoordinareDaemon(graph)
    state = daemon._state
    state["config"] = SimpleNamespace(max_concurrent_cards=2)
    state["lifecycle_sequence"] = ["implementing", "reviewing"]
    state["performer_services"] = {
        "implementing": SimpleNamespace(_config=SimpleNamespace(mode="ephemeral"), has_live_session=lambda sid: not (stale and sid == "B")),
    }
    github = AsyncMock()
    github.poll_board.return_value = {
        "snapshot": {"IN_PROGRESS": ["A"], "IN_REVIEW": ["B"], "TODO": []},
        "titles": {}, "descriptions": {}, "issue_numbers": {},
        "issue_urls": {}, "content_node_ids": {},
    }
    github.fetch_main_sha.return_value = None
    state["github_service"] = github
    for card_id, status in [("A", "IN_PROGRESS"), ("B", "IN_REVIEW")]:
        state["current_card"] = {"id": card_id, "status": status, "pr_url": f"https://example.com/pr/{card_id}"}
        state["phase"] = "monitoring_performer"
        state["performer_stage"] = "implementing"
        state["agent_dispatch"] = {"session_id": card_id}
        state["active_sessions"][card_id] = state_to_session(state)
    await daemon._invoke_multi_session()
    assert "A" in ticked
    if stale:
        assert dispatched == ["B"]
        assert daemon._state["active_sessions"]["B"]["agent_dispatch"] == {"session_id": "replacement-B"}
    else:
        assert "B" in ticked
        assert not dispatched
    assert "B" not in daemon._state["session_skip_reasons"]


@pytest.mark.parametrize("column", ["TODO", "BACKLOG"])
def test_operator_kickback_still_excludes_performer(column: str) -> None:
    from coordinare.daemon import _compute_eligibility

    eligibility = _compute_eligibility("B", {
        "current_card": {"id": "B"}, "phase": "monitoring_performer",
        "agent_dispatch": {"session_id": "B"},
    }, {column: ["B"], "IN_PROGRESS": []}, None)
    assert eligibility.eligible is False
    assert eligibility.reason == "kicked_back"


def test_replacement_intent_advances_snapshot_schema() -> None:
    from coordinare.state_store import CURRENT_SCHEMA_VERSION

    assert CURRENT_SCHEMA_VERSION == 31


@pytest.mark.parametrize("version", range(1, 26))
def test_old_snapshot_defaults_replacement_intent_off(version: int) -> None:
    from datetime import UTC, datetime

    from coordinare.state_store import WorkflowSnapshot

    snapshot = WorkflowSnapshot.model_validate({
        "schema_version": version, "snapshot_at": datetime.now(UTC), "phase": "dispatching",
        "active_sessions": {"B": {"card_id": "B", "phase": "dispatching"}},
    })
    assert snapshot.active_sessions["B"].reconciled_dispatch_pending is False


def test_snapshot_contract_validates_replacement_intent() -> None:
    import json
    from pathlib import Path

    import jsonschema

    schema = json.loads((
        Path(__file__).resolve().parents[2]
        / "specs/003-state-persistence/contracts/workflow-snapshot.schema.json"
    ).read_text())
    payload = {
        "schema_version": 25, "snapshot_at": "2026-10-08T00:00:00Z", "phase": "dispatching",
        "active_sessions": {"B": {"card_id": "B", "reconciled_dispatch_pending": "yes"}},
    }
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(payload, schema)
    payload["schema_version"] = 26
    payload["active_sessions"]["B"]["reconciled_dispatch_pending"] = True
    jsonschema.validate(payload, schema)


@pytest.mark.asyncio
@pytest.mark.parametrize("focus", ["A", "B"])
@pytest.mark.parametrize("intent", ["feedback", "replacement", "live", "missing", "unhealthy", "legacy"])
async def test_restart_preserves_review_work_through_startup_and_fanout(
    focus: str, intent: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import UTC, datetime

    from coordinare.daemon import CoordinareDaemon
    from coordinare.services.docker_executor import ContainerInfo, DockerExecutor
    from coordinare.state_store import WorkflowSnapshot

    monitored = []
    dispatched = []
    deferred = True

    async def passthrough(current):
        return current

    async def monitor(current):
        monitored.append((current["current_card"]["id"], current["agent_dispatch"]["session_id"]))
        return current

    async def dispatch(current):
        if deferred:
            return current
        dispatched.append(current["current_card"]["id"])
        current["phase"] = "monitoring_performer"
        current["agent_dispatch"] = {"session_id": "replacement-B"}
        current["relay_feedback"] = []
        current["reconciled_dispatch_pending"] = False
        return current

    graph = CoordinareGraphBuilder(node_overrides={
        "route_issue_comments": passthrough, "notify": passthrough,
        "monitor_agent": monitor, "dispatch_card": dispatch,
    }).build()
    source = CoordinareDaemon(graph)
    state = source._state
    state["lifecycle_sequence"] = ["implementing", "reviewing"]
    for card_id, status in [("A", "IN_PROGRESS"), ("B", "IN_REVIEW")]:
        state["current_card"] = {"id": card_id, "status": status, "pr_url": f"https://example.com/pr/{card_id}"}
        state["phase"] = "monitoring_performer"
        state["performer_stage"] = "implementing"
        state["agent_dispatch"] = {"session_id": card_id, "performer_id": f"pool-{card_id}"}
        if card_id == "B" and intent in ("feedback", "replacement"):
            state["phase"] = "dispatching"
            state["agent_dispatch"] = {}
            state["processed_review_ids"] = {"review-B"}
            state["relay_feedback"] = [{"id": "review-B", "body": "Fix the test"}] if intent == "feedback" else []
            state["reconciled_dispatch_pending"] = intent == "replacement"
        if card_id == "B" and intent == "legacy":
            state["agent_dispatch"] = {}
        state["active_sessions"][card_id] = state_to_session(state)
    from coordinare.session import session_to_state

    state["active_card_id"] = focus
    session_to_state(state["active_sessions"][focus], state)
    snapshot = WorkflowSnapshot.model_validate_json(source._build_snapshot().model_dump_json())

    daemon = CoordinareDaemon(graph)
    daemon._state["config"] = SimpleNamespace(max_concurrent_cards=2)
    services = {
        card_id: SimpleNamespace(_active_jobs={}, _config=SimpleNamespace(mode="ephemeral"))
        for card_id in ("A", "B")
    }
    for service in services.values():
        service.has_live_session = lambda sid, svc=service: sid in svc._active_jobs
    daemon._state["performer_services"] = {"implementing": services["A"]}
    daemon._state["performer_services_by_id"] = {f"pool-{cid}": svc for cid, svc in services.items()}
    github = AsyncMock()
    github.project_id = 123
    github.poll_board.return_value = {
        "snapshot": {"IN_PROGRESS": ["A"], "IN_REVIEW": ["B"], "TODO": []},
        "titles": {}, "descriptions": {}, "issue_numbers": {},
        "issue_urls": {}, "content_node_ids": {},
    }
    github.fetch_main_sha.return_value = None
    daemon._state["github_service"] = github
    daemon._restore_from_snapshot(snapshot)
    await daemon._reconcile_with_board(snapshot)
    expected_phase = "dispatching" if intent in ("feedback", "replacement", "legacy") else "monitoring_performer"
    assert daemon._state["active_sessions"]["B"]["phase"] == expected_phase

    containers = [ContainerInfo(
        container_id=f"ctr-{cid}", name=f"performer-{cid}", image="performer:full",
        started_at=datetime.now(UTC), labels={
            "coordinare.session_id": cid, "coordinare.card_id": cid,
            "coordinare.performer_stage": "implementing", "coordinare.spec_version": "076",
        },
    ) for cid in (["A", "B"] if intent in ("live", "unhealthy") else ["A"])]
    async def list_containers(filters, **kwargs):
        return [container for container in containers if all(
            container.labels.get(key) == value for key, value in filters.items()
        )]

    monkeypatch.setattr(DockerExecutor, "list_containers_by_label", AsyncMock(side_effect=list_containers))
    monkeypatch.setattr(DockerExecutor, "port_of", AsyncMock(return_value=55555))
    monkeypatch.setattr(
        "coordinare.transport.http_transport.PerformerHTTPClient.get_status",
        AsyncMock(return_value=SimpleNamespace(current_job_id="runner-job")),
    )
    monkeypatch.setattr(DockerExecutor, "probe_healthz", AsyncMock(
        side_effect=lambda cid, **kwargs: not (cid == "ctr-B" and intent == "unhealthy"),
    ))
    stop = AsyncMock(return_value=True)
    monkeypatch.setattr(DockerExecutor, "stop_container", stop)
    await daemon._startup_reconciliation_pass()
    assert "A" in services["A"]._active_jobs
    if intent == "live":
        assert "B" in services["B"]._active_jobs
        assert "B" not in services["A"]._active_jobs
    else:
        assert daemon._state["active_sessions"]["B"]["phase"] == "dispatching"
        if intent != "feedback":
            assert daemon._state["active_sessions"]["B"]["reconciled_dispatch_pending"] is True
    if intent == "unhealthy":
        stop.assert_any_await("ctr-B", timeout=5.0)
        assert all(call.args[0] == "ctr-B" for call in stop.await_args_list)

    await daemon._invoke_multi_session()
    assert ("A", "A") in monitored
    if intent == "live":
        assert ("B", "B") in monitored
        assert not dispatched
    else:
        assert daemon._state["active_sessions"]["B"]["phase"] == "dispatching"
        deferred = False
        await daemon._invoke_multi_session()
        assert dispatched == ["B"]
        assert daemon._state["active_sessions"]["B"]["phase"] == "monitoring_performer"


@pytest.mark.asyncio
@pytest.mark.parametrize("health", [
    "live", "missing", "unhealthy", "unknown", "unowned", "pending", "multiple",
    "auth", "stop-unconfirmed", "persistent",
])
async def test_kubernetes_restart_reconciliation_uses_owned_session_without_docker(
    health: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import MagicMock

    from coordinare.daemon import CoordinareDaemon
    from coordinare.services.docker_executor import DockerExecutor
    from coordinare.services.kubernetes_runtime import KubernetesRuntime
    from coordinare.transport.http_transport import PerformerAuthError, PerformerHTTPClient

    core = MagicMock()
    own = SimpleNamespace(
        metadata=SimpleNamespace(name="owned-B", labels={
            "coordinare.vividynamics.com/owner": "test-owner", "coordinare.session_id": "B",
        }), status=SimpleNamespace(phase="Running", pod_ip="10.0.0.2"),
    )
    foreign = SimpleNamespace(
        metadata=SimpleNamespace(name="foreign-B", labels={
            "coordinare.vividynamics.com/owner": "another-owner", "coordinare.session_id": "B",
        }), status=SimpleNamespace(phase="Running", pod_ip="10.0.0.3"),
    )
    core.list_namespaced_pod.return_value = SimpleNamespace(items=[foreign, *([] if health == "missing" else [own])])
    if health == "unknown":
        core.list_namespaced_pod.side_effect = RuntimeError("API temporarily unavailable")
    if health == "pending":
        own.status.phase = "Pending"
        own.status.pod_ip = None
    if health == "multiple":
        core.list_namespaced_pod.return_value.items.append(own)
    runtime = KubernetesRuntime(namespace="test", owner=None if health == "unowned" else "test-owner", core_v1=core)
    stopped = []

    async def stop(handle):
        stopped.append(handle)
        if health != "stop-unconfirmed":
            core.list_namespaced_pod.return_value = SimpleNamespace(items=[foreign])

    monkeypatch.setattr(runtime, "stop", stop)
    monkeypatch.setattr(PerformerHTTPClient, "get_status", AsyncMock(
        return_value=SimpleNamespace(current_job_id="runner-B"),
        side_effect=(
            PerformerAuthError("invalid authentication") if health == "auth"
            else RuntimeError("dead worker") if health in ("unhealthy", "stop-unconfirmed") else None
        ),
    ))
    docker = AsyncMock(side_effect=AssertionError("Kubernetes reconciliation must not enumerate Docker"))
    monkeypatch.setattr(DockerExecutor, "list_containers_by_label", docker)
    service = SimpleNamespace(_runtime=runtime, _config=SimpleNamespace(
        mode="persistent" if health == "persistent" else "ephemeral",
    ), _active_jobs={})
    daemon = CoordinareDaemon(SimpleNamespace())
    daemon._state["performer_services"] = {"implementing": service}
    daemon._state["active_sessions"] = {"B": {
        "current_card": {"id": "B", "status": "IN_REVIEW"}, "phase": "monitoring_performer",
        "performer_stage": "implementing", "agent_dispatch": {"session_id": "B"},
    }}
    await daemon._startup_reconciliation_pass()
    session = daemon._state["active_sessions"]["B"]
    docker.assert_not_awaited()
    assert daemon._reconciliation_blocked_by_docker is False
    if health == "live":
        assert service._active_jobs["B"].container_id == "owned-B"
        assert service._active_jobs["B"].endpoint == "http://10.0.0.2:8088"
        assert session["phase"] == "monitoring_performer"
        await service._active_jobs["B"].client.aclose()
    elif health in ("unknown", "unowned", "pending", "multiple", "auth", "stop-unconfirmed", "persistent"):
        assert session["agent_dispatch"] == {"session_id": "B"}
        assert session["phase"] == "monitoring_performer"
    else:
        assert session["phase"] == "dispatching"
        assert session["reconciled_dispatch_pending"] is True
        assert session["agent_dispatch"] == {}
    assert stopped == (["owned-B"] if health in ("unhealthy", "stop-unconfirmed") else [])


@pytest.mark.parametrize("version", [1, 25])
def test_legacy_focus_session_identity_is_canonical_and_not_inherited(version: int) -> None:
    from datetime import UTC, datetime

    from coordinare.daemon import CoordinareDaemon
    from coordinare.session import session_to_state
    from coordinare.state_store import PersistedSession, WorkflowSnapshot

    snapshot = WorkflowSnapshot(
        schema_version=version, snapshot_at=datetime.now(UTC), phase="monitoring_performer",
        active_card_id="A", active_card_column="IN_REVIEW", agent_session_id="legacy-A",
        active_sessions={} if version == 1 else {
            cid: PersistedSession(card_id=cid, phase="monitoring_performer", performer_stage="implementing")
            for cid in ("A", "B")
        },
    )
    daemon = CoordinareDaemon(SimpleNamespace())
    daemon._restore_from_snapshot(snapshot)
    assert daemon._state["active_sessions"]["A"]["agent_dispatch"] == {"session_id": "legacy-A"}
    if version == 25:
        session_to_state(daemon._state["active_sessions"]["B"], daemon._state)
        assert daemon._state["agent_dispatch"] == {}


def test_snapshot_keeps_only_performer_routing_identity() -> None:
    from coordinare.daemon import _persist_one_session

    persisted = _persist_one_session("A", {"agent_dispatch": {
        "session_id": "A", "performer_id": "pool-A", "job_id": "runner-A",
        "auth_token": "never-persist-this-value", "endpoint": "http://private-performer:8088",
    }})
    assert persisted.agent_session_id == "A"
    assert persisted.agent_performer_id == "pool-A"
    assert persisted.agent_job_id == "runner-A"
    raw = persisted.model_dump_json()
    assert "never-persist-this-value" not in raw
    assert "http://private-performer:8088" not in raw


@pytest.mark.asyncio
@pytest.mark.parametrize("uncertainty", ["api", "auth", "status_timeout"])
async def test_unknown_kubernetes_session_holds_real_daemon_tick(
    uncertainty: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import MagicMock

    from coordinare.daemon import CoordinareDaemon
    from coordinare.models.performer_endpoint import PerformerEndpointConfig
    from coordinare.services.http_performer_service import HTTPPerformerService
    from coordinare.services.kubernetes_runtime import KubernetesRuntime
    from coordinare.transport.base import TransportTimeoutError
    from coordinare.transport.http_transport import PerformerAuthError, PerformerHTTPClient

    core = MagicMock()
    if uncertainty == "api":
        core.list_namespaced_pod.side_effect = RuntimeError("API temporarily unavailable")
    else:
        core.list_namespaced_pod.return_value = SimpleNamespace(items=[SimpleNamespace(
            metadata=SimpleNamespace(name="owned-B", labels={
                "coordinare.vividynamics.com/owner": "test-owner", "coordinare.session_id": "B",
            }), status=SimpleNamespace(phase="Running", pod_ip="10.0.0.2"),
        )])
        monkeypatch.setattr(PerformerHTTPClient, "get_status", AsyncMock(side_effect=(
            TransportTimeoutError("status timed out") if uncertainty == "status_timeout" else PerformerAuthError("auth unknown")
        )))
    runtime = KubernetesRuntime(namespace="test", owner="test-owner", core_v1=core)
    stop = AsyncMock()
    monkeypatch.setattr(runtime, "stop", stop)
    service = HTTPPerformerService(PerformerEndpointConfig(
        id="pool-B", mode="ephemeral", image="performer:full", roles=["implementer"],
    ), runtime=runtime)
    status = AsyncMock(wraps=service.check_status)
    dispatch = AsyncMock(side_effect=AssertionError("uncertain session must not dispatch"))
    monkeypatch.setattr(service, "check_status", status)
    monkeypatch.setattr(service, "dispatch_card", dispatch)

    async def passthrough(current):
        return current

    graph = CoordinareGraphBuilder(node_overrides={
        "route_issue_comments": passthrough, "notify": passthrough,
    }).build()
    daemon = CoordinareDaemon(graph)
    state = daemon._state
    state["config"] = SimpleNamespace(max_concurrent_cards=1)
    state["lifecycle_sequence"] = ["implementing", "reviewing"]
    state["current_card"] = {"id": "B", "status": "IN_REVIEW", "pr_url": "https://example.com/pr/B"}
    state["phase"] = "monitoring_performer"
    state["performer_stage"] = "implementing"
    state["agent_dispatch"] = {"session_id": "B", "performer_id": "pool-B"}
    state["active_card_id"] = "B"
    state["active_sessions"] = {"B": state_to_session(state)}
    state["performer_services"] = {"implementing": service}
    state["performer_services_by_id"] = {"pool-B": service}
    github = AsyncMock()
    github.poll_board.return_value = {
        "snapshot": {"IN_REVIEW": ["B"], "IN_PROGRESS": [], "TODO": []},
        "titles": {}, "descriptions": {}, "issue_numbers": {}, "issue_urls": {}, "content_node_ids": {},
    }
    github.fetch_main_sha.return_value = None
    state["github_service"] = github
    for _ in range(2):
        await daemon._invoke_multi_session()
        session = daemon._state["active_sessions"]["B"]
        assert session["phase"] == "monitoring_performer"
        assert session["agent_dispatch"]["session_id"] == "B"
        assert session["system_error_count"] == 0
    status.assert_not_awaited()
    dispatch.assert_not_awaited()
    stop.assert_not_awaited()


@pytest.mark.asyncio
async def test_persistent_session_startup_does_not_require_docker(monkeypatch: pytest.MonkeyPatch) -> None:
    from coordinare.daemon import CoordinareDaemon
    from coordinare.services.docker_executor import DockerExecutor

    docker = AsyncMock(side_effect=AssertionError("persistent endpoint must not require Docker"))
    monkeypatch.setattr(DockerExecutor, "list_containers_by_label", docker)
    daemon = CoordinareDaemon(SimpleNamespace())
    daemon._state["performer_services"] = {"implementing": SimpleNamespace(_config=SimpleNamespace(mode="persistent"))}
    daemon._state["active_sessions"] = {"B": {
        "phase": "monitoring_performer", "performer_stage": "implementing",
        "current_card": {"id": "B"}, "agent_dispatch": {"session_id": "persistent-B"},
    }}
    await daemon._startup_reconciliation_pass()
    docker.assert_not_awaited()
    assert daemon._state["active_sessions"]["B"]["agent_dispatch"] == {"session_id": "persistent-B"}
    assert daemon._state["active_sessions"]["B"]["phase"] == "monitoring_performer"


@pytest.mark.asyncio
@pytest.mark.parametrize("configured_native", [False, True])
async def test_native_restart_identity_never_enters_docker_reconciliation(
    configured_native: bool, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from coordinare.services.agent_service import AgentService
    from coordinare.services.docker_executor import DockerExecutor
    from coordinare.services.reconciliation import (
        ReconciliationDecision,
        handle_potentially_stale_session,
        run_startup_reconciliation,
    )

    docker = AsyncMock()
    docker.list_containers_by_label.side_effect = AssertionError("native sessions have no Docker container")
    monkeypatch.setattr(DockerExecutor, "list_containers_by_label", docker.list_containers_by_label)
    state = initial_state()
    state["performer_services"] = {"implementing": AgentService(AsyncMock())} if configured_native else {}
    session = {
        "phase": "monitoring_agent", "performer_stage": "implementing",
        "agent_dispatch": {"session_id": "native-session"},
    }
    state["active_sessions"] = {"native": session}
    report = await run_startup_reconciliation(state, docker)
    assert report.decisions["native"] == ReconciliationDecision.DEFERRED
    assert not report.docker_unreachable
    assert await handle_potentially_stale_session(state, "native", docker_executor=docker) == ReconciliationDecision.DEFERRED
    assert session["agent_dispatch"] == {"session_id": "native-session"}
    assert session["phase"] == "monitoring_agent"
    assert not session.get("reconciled_dispatch_pending")
    docker.list_containers_by_label.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime_kind", ["docker", "kubernetes"])
@pytest.mark.parametrize("identity", ["saved_busy", "saved_terminal", "legacy_busy", "unknown", "mismatch", "legacy_finished_during_restart"])
async def test_restored_runner_identity_reaches_real_status_poll(
    runtime_kind: str, identity: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from datetime import UTC, datetime
    from unittest.mock import MagicMock

    from coordinare.daemon import CoordinareDaemon
    from coordinare.models.performer_endpoint import PerformerEndpointConfig
    from coordinare.services.docker_executor import ContainerInfo
    from coordinare.services.http_performer_service import HTTPPerformerService
    from coordinare.services.kubernetes_runtime import KubernetesRuntime
    from coordinare.services.reconciliation import ReconciliationDecision, run_startup_reconciliation
    from coordinare.state_store import WorkflowSnapshot
    from coordinare.transport.http_transport import PerformerHTTPClient

    session_id, runner_id = "session-545", "runner-job-545"
    core = MagicMock()
    core.list_namespaced_pod.return_value = SimpleNamespace(items=[SimpleNamespace(
        metadata=SimpleNamespace(name="owned-545", labels={
            "coordinare.vividynamics.com/owner": "test-owner", "coordinare.session_id": session_id,
        }), status=SimpleNamespace(phase="Running", pod_ip="10.0.0.2"),
    )])
    runtime = KubernetesRuntime(namespace="test", owner="test-owner", core_v1=core) if runtime_kind == "kubernetes" else None
    service = HTTPPerformerService(PerformerEndpointConfig(
        id="pool-545", mode="ephemeral", image="performer:full", roles=["implementer"],
    ), runtime=runtime)
    stop = AsyncMock()
    monkeypatch.setattr(service._runtime, "stop", stop)
    current_job_id = None if identity in ("saved_terminal", "unknown") else runner_id
    monkeypatch.setattr(PerformerHTTPClient, "get_status", AsyncMock(return_value=SimpleNamespace(current_job_id=current_job_id)))

    async def get_job(client, job_id):
        assert job_id == runner_id, "poll must use runner ID, never Coordinare session ID"
        return SimpleNamespace(
            state="succeeded" if identity == "saved_terminal" else "running",
            result=SimpleNamespace(summary='{"status":"success"}') if identity == "saved_terminal" else None,
            events=[], metrics=None,
        )

    monkeypatch.setattr(PerformerHTTPClient, "get_job", get_job)
    source = CoordinareDaemon(SimpleNamespace())
    source._state["current_card"] = {"id": "545", "status": "IN_REVIEW"}
    source._state["active_card_id"] = "545"
    source._state["phase"] = "monitoring_performer"
    source._state["performer_stage"] = "implementing"
    source._state["agent_dispatch"] = {"session_id": session_id, "performer_id": "pool-545"}
    if identity.startswith("saved") or identity == "mismatch":
        source._state["agent_dispatch"]["job_id"] = "wrong-job" if identity == "mismatch" else runner_id
    source._state["active_sessions"] = {"545": state_to_session(source._state)}
    snapshot = WorkflowSnapshot.model_validate_json(source._build_snapshot().model_dump_json())
    daemon = CoordinareDaemon(SimpleNamespace())
    daemon._restore_from_snapshot(snapshot)
    daemon._state["performer_services"] = {"implementing": service}
    executor = AsyncMock()
    executor.list_containers_by_label.return_value = [ContainerInfo(
        container_id="owned-545", name="owned-545", image="performer:full", started_at=datetime.now(UTC),
        labels={"coordinare.session_id": session_id},
    )]
    executor.port_of.return_value = 55555
    executor.probe_healthz.return_value = True
    previous = daemon._lifecycle_signature()
    daemon._state_store = SimpleNamespace(save=AsyncMock())
    report = await run_startup_reconciliation(daemon._state, executor)
    if identity == "legacy_finished_during_restart":
        await daemon._save_snapshot_if_changed(previous)
        saved = daemon._state_store.save.await_args.args[0]
        assert saved.active_sessions["545"].agent_job_id == runner_id
        await service._active_jobs[session_id].client.aclose()
        service._active_jobs.clear()
        daemon = CoordinareDaemon(SimpleNamespace())
        daemon._restore_from_snapshot(WorkflowSnapshot.model_validate_json(saved.model_dump_json()))
        daemon._state["performer_services"] = {"implementing": service}
        monkeypatch.setattr(PerformerHTTPClient, "get_status", AsyncMock(return_value=SimpleNamespace(current_job_id=None)))
        identity = "saved_terminal"
        report = await run_startup_reconciliation(daemon._state, executor)
    session = daemon._state["active_sessions"]["545"]
    if identity in ("unknown", "mismatch"):
        assert report.decisions["545"] == ReconciliationDecision.DEFERRED
        assert session["agent_dispatch"]["session_id"] == session_id
        assert not service.has_live_session(session_id)
        stop.assert_not_awaited()
        executor.stop_container.assert_not_awaited()
    else:
        assert report.decisions["545"] == ReconciliationDecision.ADOPTED
        assert service._active_jobs[session_id].job_id == runner_id
        result = await service.check_status(session_id)
        assert result["status"] == ("success" if identity == "saved_terminal" else "working")
        if identity != "saved_terminal":
            stop.assert_not_awaited()
            await service._active_jobs[session_id].client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize("runtime_kind", ["docker", "kubernetes"])
async def test_deferred_side_writer_recovers_on_later_daemon_cycle(
    runtime_kind: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio
    from datetime import UTC, datetime
    from unittest.mock import MagicMock

    from coordinare.daemon import CoordinareDaemon
    from coordinare.models.performer_endpoint import PerformerEndpointConfig
    from coordinare.services.docker_executor import ContainerInfo, DockerExecutor
    from coordinare.services.http_performer_service import HTTPPerformerService
    from coordinare.services.kubernetes_runtime import KubernetesRuntime
    from coordinare.transport.base import TransportTimeoutError
    from coordinare.transport.http_transport import PerformerHTTPClient

    core = MagicMock()
    core.list_namespaced_pod.return_value = SimpleNamespace(items=[SimpleNamespace(
        metadata=SimpleNamespace(name="side-pod", labels={
            "coordinare.vividynamics.com/owner": "test-owner", "coordinare.session_id": "side-session",
        }), status=SimpleNamespace(phase="Running", pod_ip="10.0.0.2"),
    )])
    runtime = KubernetesRuntime(namespace="test", owner="test-owner", core_v1=core) if runtime_kind == "kubernetes" else None
    service = HTTPPerformerService(PerformerEndpointConfig(
        id="doc", mode="ephemeral", image="performer:full", roles=["documenting"],
    ), runtime=runtime)
    stop = AsyncMock()
    monkeypatch.setattr(service._runtime, "stop", stop)
    container = ContainerInfo(container_id="side-pod", name="side-pod", image="performer:full",
                              started_at=datetime.now(UTC), labels={"coordinare.session_id": "side-session"})
    monkeypatch.setattr(DockerExecutor, "list_containers_by_label", AsyncMock(return_value=[container]))
    monkeypatch.setattr(DockerExecutor, "port_of", AsyncMock(return_value=55555))
    monkeypatch.setattr(DockerExecutor, "probe_healthz", AsyncMock(return_value=True))
    health = AsyncMock(side_effect=[TransportTimeoutError("status unknown"), SimpleNamespace(current_job_id="side-job")])
    monkeypatch.setattr(PerformerHTTPClient, "get_status", health)
    polled = []

    async def get_job(client, job_id):
        polled.append(job_id)
        return SimpleNamespace(state="succeeded", result=SimpleNamespace(summary='{"status":"docs_committed"}'))

    monkeypatch.setattr(PerformerHTTPClient, "get_job", get_job)
    dispatch = AsyncMock(side_effect=AssertionError("must not dispatch a second writer"))
    monkeypatch.setattr(service, "dispatch_card", dispatch)
    daemon = CoordinareDaemon(SimpleNamespace())
    session = {"phase": "monitoring_pr", "performer_stage": "implementing",
               "agent_dispatch": {"job_id": "foreground-job"},
               "documenting_side": {"status": "running", "writer_active": True,
                                    "session_id": "side-session", "job_id": "side-job"}}
    daemon._state["active_sessions"] = {"545": session}
    daemon._state["performer_services"] = {"documenting": service}
    await daemon._dispatch_documenting_side_runs({})
    if daemon._documenting_side_tasks:
        await asyncio.gather(*daemon._documenting_side_tasks)
    assert not polled
    assert session["documenting_side"]["status"] == "running"
    assert session["documenting_side"]["writer_active"]
    stop.assert_not_awaited()
    await daemon._dispatch_documenting_side_runs({})
    if daemon._documenting_side_tasks:
        await asyncio.gather(*daemon._documenting_side_tasks)
    assert polled == ["side-job"]
    assert session["documenting_side"]["status"] == "done"
    assert session["agent_dispatch"] == {"job_id": "foreground-job"}
    dispatch.assert_not_awaited()


@pytest.mark.parametrize("phase", ["monitoring_performer", "monitoring_agent"])
@pytest.mark.parametrize("stage", ["implementing", "reviewing"])
def test_restored_monitor_without_identity_requests_replacement(phase: str, stage: str) -> None:
    from datetime import UTC, datetime

    from coordinare.daemon import _restored_session_dict
    from coordinare.state_store import PersistedSession, WorkflowSnapshot

    snapshot = WorkflowSnapshot(schema_version=25, snapshot_at=datetime.now(UTC), phase=phase, active_card_id="A")
    restored = _restored_session_dict("B", PersistedSession(card_id="B", phase=phase, performer_stage=stage), snapshot, None)
    assert restored["agent_dispatch"] == {}
    assert restored["phase"] == "dispatching"
    assert restored["reconciled_dispatch_pending"] is True


def test_restored_slot_uses_saved_performer_pool_member() -> None:
    from coordinare.services.slot_manager import SlotManager

    services = [SimpleNamespace(_config=SimpleNamespace(id=f"pool-{name}")) for name in ("A", "B")]
    manager = SlotManager()
    manager.register_pool("implementing", services, 2)
    sessions = {"B": {"phase": "monitoring_performer", "performer_stage": "implementing", "agent_dispatch": {"session_id": "session-B", "performer_id": "pool-B"}}}
    manager.sync_from_sessions(sessions)
    assert manager.acquire("implementing", "B") is services[1]
    assert manager.acquire("implementing", "A") is services[0]


@pytest.mark.asyncio
async def test_monitor_uses_restored_slot_endpoint() -> None:
    from coordinare.graph.nodes.monitor.body import _BodyCtx, _phase_slot_setup
    from coordinare.services.slot_manager import SlotManager

    services = [SimpleNamespace(_config=SimpleNamespace(id=f"pool-{name}")) for name in ("A", "B")]
    manager = SlotManager()
    manager.register_pool("implementing", services, 2)
    session = {"phase": "monitoring_performer", "performer_stage": "implementing", "agent_dispatch": {"session_id": "session-B", "performer_id": "pool-B"}}
    manager.sync_from_sessions({"B": session})
    state = {**session, "slot_manager": manager, "performer_services": {"implementing": services[0]}}
    ctx = _BodyCtx(card={"id": "B"}, card_id="B", stage="implementing", performer_services=state["performer_services"])
    assert await _phase_slot_setup(state, ctx) is None
    assert ctx.service is services[1]
    assert ctx.session_id == "session-B"


def test_live_restored_pool_member_is_not_mistaken_for_gone_implementer() -> None:
    from coordinare.graph.nodes.monitor.gates import _implementer_session_gone

    primary = SimpleNamespace(has_live_session=lambda sid: False)
    adopted = SimpleNamespace(has_live_session=lambda sid: sid == "session-B")
    state = {"agent_dispatch": {"session_id": "session-B", "performer_id": "pool-B"}, "performer_services": {"implementing": primary}, "performer_services_by_id": {"pool-B": adopted}}
    assert _implementer_session_gone(state) is False


@pytest.mark.parametrize("phase", ["monitoring_performer", "monitoring_agent"])
def test_v1_missing_identity_requests_replacement(phase: str) -> None:
    from datetime import UTC, datetime

    from coordinare.daemon import CoordinareDaemon
    from coordinare.state_store import WorkflowSnapshot

    daemon = CoordinareDaemon(SimpleNamespace())
    daemon._restore_from_snapshot(WorkflowSnapshot(schema_version=1, snapshot_at=datetime.now(UTC), phase=phase, active_card_id="B", active_card_column="IN_REVIEW"))
    session = daemon._state["active_sessions"]["B"]
    assert session["phase"] == "dispatching"
    assert session["reconciled_dispatch_pending"] is True


@pytest.mark.asyncio
@pytest.mark.parametrize("side", [False, True])
@pytest.mark.parametrize("startup", [False, True])
async def test_discovered_job_identity_is_saved_without_stage_change(side: bool, startup: bool) -> None:
    from coordinare.daemon import CoordinareDaemon

    daemon = CoordinareDaemon(SimpleNamespace())
    daemon._state_store = SimpleNamespace(save=AsyncMock())
    session = {"performer_stage": "implementing", "phase": "monitoring_performer", "agent_dispatch": {"session_id": "B"}, "documenting_side": {"session_id": "writer-B", "blueprint_hash": "blueprint-B", "status": "running", "writer_active": True}}
    daemon._state["active_sessions"] = {"B": session}
    previous = daemon._lifecycle_signature()

    async def adopt():
        if side:
            session["documenting_side"]["job_id"] = "runner-writer"
        else:
            session["agent_dispatch"]["job_id"] = "runner-B"

    if startup:
        daemon._startup_load_snapshot = AsyncMock()
        daemon._startup_reconciliation_pass = adopt
        daemon._notify_daemon_restart = AsyncMock()
        _, signature = await daemon._run_startup_sequence()
    else:
        await adopt()
        signature = await daemon._save_snapshot_if_changed(previous)
    daemon._state_store.save.assert_awaited_once()
    saved = daemon._state_store.save.await_args.args[0].active_sessions["B"]
    assert (saved.documenting_side.job_id if side else saved.agent_job_id) == ("runner-writer" if side else "runner-B")
    await daemon._save_snapshot_if_changed(signature)
    daemon._state_store.save.assert_awaited_once()


@pytest.mark.asyncio
async def test_noop_startup_does_not_write_snapshot() -> None:
    from coordinare.daemon import CoordinareDaemon

    daemon = CoordinareDaemon(SimpleNamespace())
    daemon._state_store = SimpleNamespace(save=AsyncMock())
    daemon._startup_load_snapshot = AsyncMock()
    daemon._startup_reconciliation_pass = AsyncMock()
    daemon._notify_daemon_restart = AsyncMock()
    await daemon._run_startup_sequence()
    daemon._state_store.save.assert_not_awaited()


@pytest.mark.asyncio
async def test_startup_snapshot_failure_retains_signature_for_retry() -> None:
    from coordinare.daemon import CoordinareDaemon

    daemon = CoordinareDaemon(SimpleNamespace())
    daemon._state_store = SimpleNamespace(save=AsyncMock(side_effect=[OSError("disk unavailable"), None]))
    daemon._state["active_sessions"] = {"B": {"phase": "monitoring_performer", "agent_dispatch": {"session_id": "B"}}}
    previous = daemon._lifecycle_signature()
    daemon._startup_load_snapshot = AsyncMock()
    daemon._notify_daemon_restart = AsyncMock()

    async def adopt():
        daemon._state["active_sessions"]["B"]["agent_dispatch"]["job_id"] = "runner-B"

    daemon._startup_reconciliation_pass = adopt
    _, signature = await daemon._run_startup_sequence()
    assert signature == previous
    signature = await daemon._save_snapshot_if_changed(signature)
    assert signature == daemon._lifecycle_signature()
    assert daemon._state_store.save.await_count == 2


@pytest.mark.asyncio
async def test_actual_snapshot_write_failure_retries_discovered_identity(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    from coordinare.daemon import CoordinareDaemon
    from coordinare.metrics import METRICS
    from coordinare.state_store import StateStore

    daemon = CoordinareDaemon(SimpleNamespace())
    store = StateStore(tmp_path / "state.json", METRICS)
    daemon._state_store = store
    daemon._state["active_sessions"] = {"B": {"phase": "monitoring_performer", "agent_dispatch": {"session_id": "B"}}}
    previous = daemon._lifecycle_signature()
    daemon._startup_load_snapshot = AsyncMock()
    daemon._notify_daemon_restart = AsyncMock()

    async def adopt():
        daemon._state["active_sessions"]["B"]["agent_dispatch"]["job_id"] = "runner-B"

    daemon._startup_reconciliation_pass = adopt
    def fail_replace(*_):
        raise OSError("disk unavailable")

    with monkeypatch.context() as patch:
        patch.setattr("coordinare.state_store.os.replace", fail_replace)
        _, signature = await daemon._run_startup_sequence()
    assert signature == previous
    assert store.last_snapshot is None
    assert not (tmp_path / "state.json").exists()
    await daemon._save_snapshot_if_changed(signature)
    saved = await store.load()
    assert saved.active_sessions["B"].agent_job_id == "runner-B"
