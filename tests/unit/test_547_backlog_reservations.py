from __future__ import annotations

from datetime import UTC, datetime

import pytest

from coordinare.daemon import _persist_one_session, _restored_session_dict
from coordinare.services.pipeline_budget import select_pipelines
from coordinare.state_store import PersistedSession, WorkflowSnapshot


@pytest.mark.parametrize("status,phase", [("BACKLOG", "monitoring_pr"), ("BACKLOG", "idle"), ("IN_PROGRESS", "idle")])
@pytest.mark.parametrize("next_phase", ["dispatching", "monitoring_pr"])
@pytest.mark.parametrize("restart", [False, True])
def test_paused_reservation_releases_capacity_one_for_sibling(status, phase, next_phase, restart) -> None:
    card = {"id": "paused", "status": status}
    paused = {"current_card": card, "phase": phase, "performer_stage": "implementing", "pipeline_admitted": True}
    if restart:
        record = _persist_one_session("paused", paused)
        record = PersistedSession.model_validate_json(record.model_dump_json())
        paused = _restored_session_dict("paused", record, WorkflowSnapshot(snapshot_at=datetime.now(UTC), phase=phase, active_card_id="paused"), card)
    work = {"paused": paused, "sibling": {"current_card": {"id": "sibling", "status": "TODO" if next_phase == "dispatching" else "IN_REVIEW"}, "phase": next_phase}}
    assert select_pipelines(work, 1) == {"sibling"}
    assert not work["paused"]["pipeline_admitted"]
    assert work["sibling"]["pipeline_admitted"]


def test_running_paused_worker_remains_reserved_until_stopped() -> None:
    work = {"running": {"current_card": {"id": "running", "status": "BACKLOG"}, "phase": "monitoring_performer", "agent_dispatch": {"session_id": "worker"}},
            "queued": {"current_card": {"id": "queued", "status": "TODO"}, "phase": "dispatching"}}
    assert select_pipelines(work, 1) == {"running"}


@pytest.mark.parametrize("live", [False, True])
def test_fresh_board_backlog_overrides_stale_canonical_review_status(live) -> None:
    from coordinare.daemon import _compute_session_eligibilities
    from coordinare.graph.state import initial_state

    paused = {"current_card": {"id": "paused", "status": "IN_REVIEW"}, "phase": "monitoring_performer" if live else "monitoring_pr", "pipeline_admitted": True}
    if live:
        paused["agent_dispatch"] = {"session_id": "still-running"}
    work = {"paused": paused, "sibling": {"current_card": {"id": "sibling", "status": "TODO"}, "phase": "dispatching"}}
    state = initial_state()
    state["board_snapshot"] = {"BACKLOG": ["paused"], "TODO": ["sibling"], "IN_PROGRESS": [], "IN_REVIEW": []}
    eligibility = _compute_session_eligibilities(state, work, 1)
    assert not eligibility["paused"].eligible
    assert state["_pipeline_selected"] == ({"paused"} if live else {"sibling"})
    assert eligibility["sibling"].eligible is not live
