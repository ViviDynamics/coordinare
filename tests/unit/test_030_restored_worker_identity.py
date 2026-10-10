from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from coordinare.__main__ import _compose_performer_pools
from coordinare.daemon import _restored_session_dict
from coordinare.graph.nodes.monitor_performer import monitor_performer
from coordinare.graph.state import initial_state
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
