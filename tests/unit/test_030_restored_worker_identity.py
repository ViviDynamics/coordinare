from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from coordinare.__main__ import _compose_performer_pools
from coordinare.daemon import _restored_session_dict
from coordinare.graph.nodes.monitor_performer import monitor_performer
from coordinare.graph.state import initial_state
from coordinare.services.slot_manager import SlotManager
from coordinare.state_store import PersistedSession, WorkflowSnapshot


class Service:
    def __init__(self, performer_id: str) -> None:
        self._config = SimpleNamespace(id=performer_id, mode="persistent")
        self.polls: list[str] = []

    async def check_status(self, session_id: str, **kwargs: object) -> dict:
        self.polls.append(session_id)
        return {"status": "working"}


@pytest.mark.asyncio
@pytest.mark.parametrize("remapped_stage", [False, True])
async def test_restored_monitor_uses_retained_performer_identity(remapped_stage: bool) -> None:
    retained = Service("retained-endpoint")
    replacement_default = Service("new-stage-default")
    http_services = {"implementing": [retained]}
    if remapped_stage:
        http_services["assessing"] = [replacement_default]
    stage_services = {}
    config = SimpleNamespace(performers=SimpleNamespace(resolved_role=lambda _: None))
    _, _, by_id = _compose_performer_pools(config=config, service_lists={},
                                         http_services_by_stage=http_services, performer_services=stage_services)
    snapshot = WorkflowSnapshot(snapshot_at=datetime.now(UTC), phase="monitoring_performer", active_card_id="card-A")
    persisted = PersistedSession(card_id="card-A", phase="monitoring_performer", performer_stage="assessing",
                                 agent_session_id="existing-session", agent_performer_id="retained-endpoint")
    state = initial_state()
    state.update(_restored_session_dict("card-A", persisted, snapshot, {"id": "card-A", "status": "IN_PROGRESS"}))
    state.update(performer_services=stage_services, performer_services_by_id=by_id,
                 lifecycle_sequence=["implementing"])
    result = await monitor_performer(state)
    assert retained.polls == ["existing-session"]
    assert replacement_default.polls == []
    assert result["phase"] == "monitoring_performer"
    assert result["agent_dispatch"]["session_id"] == "existing-session"


@pytest.mark.asyncio
async def test_legacy_stage_only_monitor_keeps_existing_fallback() -> None:
    service = Service("stage-endpoint")
    state = initial_state()
    state.update(current_card={"id": "card-A", "status": "IN_PROGRESS"}, phase="monitoring_performer",
                 performer_stage="implementing", agent_dispatch={"session_id": "existing-session"},
                 performer_services={"implementing": service}, performer_services_by_id={})
    result = await monitor_performer(state)
    assert service.polls == ["existing-session"]
    assert result["phase"] == "monitoring_performer"


def moved_worker_pool(mode: str = "persistent", *, spare: bool = False, phase: str = "monitoring_performer") -> tuple[SlotManager, Service, dict]:
    retained = Service("retained-endpoint")
    retained._config.mode = mode
    services = [retained, Service("spare-endpoint")] if spare else [retained]
    config = SimpleNamespace(performers=SimpleNamespace(resolved_role=lambda _: None))
    pools, caps, _by_id = _compose_performer_pools(
        config=config, service_lists={}, http_services_by_stage={"implementing": services}, performer_services={},
    )
    manager = SlotManager()
    for stage, pool in pools.items():
        manager.register_pool(stage, pool, caps[stage])
    session = {"phase": phase, "performer_stage": "assessing",
               "agent_dispatch": {"session_id": "existing-session", "performer_id": "retained-endpoint"}}
    manager.sync_from_sessions({"card-A": session})
    return manager, retained, session


@pytest.mark.asyncio
async def test_moved_persistent_worker_keeps_sibling_queued_instead_of_busy_dispatch() -> None:
    from coordinare.graph.nodes.dispatch_performer import _acquire_via_slot_manager

    manager, retained, _session = moved_worker_pool()
    state = initial_state()
    state.update(phase="dispatching", slot_manager=manager, performer_services={"implementing": retained})
    ctx = {"performer_stage": "implementing", "card_id": "card-B",
           "performer_services": state["performer_services"]}
    result = await _acquire_via_slot_manager(state, ctx)
    assert result is state
    assert state["phase"] == "dispatching"
    assert isinstance(state["slot_queued_since"], datetime)
    assert manager.active_count("implementing") == 1


@pytest.mark.parametrize("terminal", ["idle", "blocked", "system_error", "removed", "release"])
def test_moved_persistent_reservation_releases_with_its_original_stage(terminal: str) -> None:
    manager, retained, session = moved_worker_pool()
    assert manager.acquire("implementing", "card-B") is None
    if terminal == "release":
        manager.release("assessing", "card-A")
    else:
        session["phase"] = terminal
        manager.sync_from_sessions({} if terminal == "removed" else {"card-A": session})
    assert manager.acquire("implementing", "card-B") is retained


def test_moved_persistent_worker_does_not_consume_a_different_free_endpoint() -> None:
    manager, retained, _session = moved_worker_pool(spare=True)
    manager.pools["implementing"].max_concurrency = 2
    acquired = manager.acquire("implementing", "card-B")
    assert acquired is not None and acquired is not retained


def test_moved_ephemeral_endpoint_preserves_independent_role_capacity() -> None:
    manager, retained, _session = moved_worker_pool("ephemeral")
    assert manager.acquire("implementing", "card-B") is retained


@pytest.mark.parametrize("phase", ["monitoring_performer", "monitoring_agent"])
def test_both_live_monitoring_phases_retain_moved_persistent_capacity(phase: str) -> None:
    manager, retained, session = moved_worker_pool(phase=phase)
    assert manager.acquire("implementing", "card-B") is None
    assert manager.active_count("implementing") == 1
    assert next(row for row in manager.utilization() if row["role"] == "implementing")["active"] == 1
    session["phase"] = "idle"
    manager.sync_from_sessions({"card-A": session})
    assert manager.acquire("implementing", "card-B") is retained


def test_monitoring_agent_restores_capacity_in_unchanged_persistent_role_pool() -> None:
    manager, retained, session = moved_worker_pool(phase="monitoring_agent")
    manager.register_pool("assessing", [retained], 1)
    manager.sync_from_sessions({"card-A": session})
    assert manager.acquire("assessing", "card-B") is None
    assert manager.active_count("assessing") == 1
    manager.release("assessing", "card-A")
    assert manager.acquire("assessing", "card-B") is retained


@pytest.mark.parametrize("phase", ["monitoring_performer", "monitoring_agent"])
def test_live_shared_persistent_service_reserves_every_registered_role(phase: str) -> None:
    manager, retained, session = moved_worker_pool(phase=phase)
    manager.register_pool("assessing", [retained], 1)
    manager.sync_from_sessions({"card-A": session})
    assert manager.active_count("assessing") == 1
    assert manager.active_count("implementing") == 1
    assert manager.acquire("implementing", "card-B") is None
    manager.release("assessing", "card-A")
    assert manager.active_count("implementing") == 0
    assert manager.acquire("implementing", "card-B") is retained


@pytest.mark.parametrize("mode", ["persistent", "ephemeral"])
@pytest.mark.parametrize("first_stage,second_stage", [("assessing", "implementing"), ("implementing", "assessing")])
def test_fresh_shared_endpoint_allocation_reserves_persistent_roles_before_sync(
    mode: str, first_stage: str, second_stage: str,
) -> None:
    retained = Service("shared-endpoint")
    retained._config.mode = mode
    manager = SlotManager()
    for stage in (first_stage, second_stage):
        manager.register_pool(stage, [retained], 1)
    assert manager.acquire(first_stage, "card-A") is retained
    assert manager.active_count(first_stage) == 1
    sibling = manager.acquire(second_stage, "card-B")
    if mode == "persistent":
        assert sibling is None
        assert manager.active_count(second_stage) == 1
        manager.release(first_stage, "card-A")
        assert manager.acquire(second_stage, "card-B") is retained
    else:
        assert sibling is retained
