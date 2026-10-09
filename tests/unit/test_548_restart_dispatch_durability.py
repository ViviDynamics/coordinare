from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import coordinare.graph.nodes.dispatch_performer as dispatch_module
from coordinare.daemon import CoordinareDaemon, _persist_one_session, _restored_session_dict
from coordinare.graph.nodes.classify_human_feedback import classify_human_feedback
from coordinare.graph.nodes.monitor.verdict import _apply_pending_override
from coordinare.graph.state import initial_state
from coordinare.session import session_to_state, state_to_session
from coordinare.state_store import PersistedSession, WorkflowSnapshot


def restore(state):
    card = state["current_card"]
    persisted = _persist_one_session("card", state_to_session(state))
    persisted = PersistedSession.model_validate_json(persisted.model_dump_json())
    restored = _restored_session_dict("card", persisted, WorkflowSnapshot(snapshot_at=datetime.now(UTC), phase=state["phase"]), card)
    fresh = initial_state()
    session_to_state(restored, fresh)
    return fresh


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["architecting", "reviewing", "documenting"])
@pytest.mark.parametrize("hold", ["capacity", "inflight", "rebase"])
async def test_accepted_restart_survives_dispatch_hold_json_restore_and_cache(monkeypatch, stage, hold):
    state = initial_state()
    state.update(current_card={"id": "card", "status": "IN_REVIEW", "pr_node_id": "PR"},
                 performer_stage="reviewing", lifecycle_sequence=["architecting", "implementing", "reviewing", "documenting"],
                 human_reviewers=["alice"], blueprint={"milestones": [{"name": "cached"}]},
                 pending_reviews=[{"id": "restart-command", "author_login": "alice", "state": "COMMENTED",
                                   "body": f"/coordinare restart-from {stage}"}])
    dispatch_module.stamp_blueprint_signature(state)
    await classify_human_feedback(state)
    assert "restart-command" in state["processed_review_ids"]
    original_override = dict(state["pending_override"])
    assert original_override["target_stage"] == stage
    monkeypatch.setattr("coordinare.services.pipeline_budget.dispatch_has_pipeline_slot", lambda *_: hold != "capacity")
    monkeypatch.setattr(dispatch_module, "_inflight_gates", AsyncMock(side_effect=lambda st, *_: st if hold == "inflight" else None))
    monkeypatch.setattr(dispatch_module, "_pre_dispatch_rebase_guard", AsyncMock(return_value=hold != "rebase"))
    body = AsyncMock()
    monkeypatch.setattr(dispatch_module, "_dispatch_performer_body", body)
    await dispatch_module.dispatch_performer(state)
    body.assert_not_awaited()
    assert state["pending_override"] is not None
    fresh = restore(state)
    assert "restart-command" in fresh["processed_review_ids"]
    # Applied override is an effect awaiting handoff, not a command to replay.
    fresh["agent_dispatch"] = {"session_id": "tracked-old"}
    assert _apply_pending_override(fresh) is None
    assert fresh["agent_dispatch"] == {"session_id": "tracked-old"}
    fresh["stage_verdicts"] = {stage: {"head_sha": "head", "verdict": "approved"}}
    fresh["github_service"] = SimpleNamespace(check_mergeability=AsyncMock(return_value={"head_ref_oid": "head"}))
    assert not dispatch_module.blueprint_reuse_allowed(fresh, stage)
    skipped, _ = await dispatch_module._verdict_cache_check(fresh, fresh["current_card"], stage)
    assert not skipped
    assert await dispatch_module._stage_advance_gates(fresh, {"performer_stage": stage, "card": fresh["current_card"], "card_id": "card"}) is None
    if stage == "documenting":
        sha_gate = AsyncMock(return_value=(True, "already_documented", 0))
        monkeypatch.setattr(dispatch_module, "_documenting_sha_gate", sha_gate)
        assert await dispatch_module._documenting_gate_skip(fresh, {"performer_stage": stage, "card": fresh["current_card"], "card_id": "card", "live_head": "head"}) is None
        sha_gate.assert_not_awaited()
    # Refusal after cache evaluation must preserve the effect through another restart.
    await dispatch_module._dispatch_result_error(fresh, {"reason": "temporary refusal"}, {
        "card_id": "card", "performer_stage": stage,
    }, lambda: None)
    fresh = restore(fresh)
    assert fresh["pending_override"] is not None
    await dispatch_module._finalise_success(fresh, {"session_id": "accepted"}, {
        "card": fresh["current_card"], "performer_stage": stage, "card_context": {},
    })
    assert fresh["pending_override"] is None
    assert fresh["phase"] == "monitoring_performer"


@pytest.mark.asyncio
async def test_applied_restart_receipt_triggers_actual_same_phase_snapshot_save():
    daemon = CoordinareDaemon(AsyncMock(), poll_interval_seconds=1, max_cycles=1)
    state = initial_state()
    state.update(current_card={"id": "card"}, phase="dispatching", performer_stage="reviewing",
                 lifecycle_sequence=["implementing", "reviewing"],
                 pending_override={"action": "restart", "target_stage": "reviewing"})
    daemon._state = state
    daemon._state_store = SimpleNamespace(save=AsyncMock())
    state["active_card_id"] = "card"
    state["active_sessions"] = {"card": state_to_session(state)}
    before = daemon._lifecycle_signature()
    _apply_pending_override(state)
    state["active_sessions"]["card"] = state_to_session(state)
    assert state["phase"] == "dispatching" and state["performer_stage"] == "reviewing"
    await daemon._save_snapshot_if_changed(before)
    daemon._state_store.save.assert_awaited_once()
    assert daemon._state_store.save.await_args.args[0].active_sessions["card"].pending_override["applied"] is True


@pytest.mark.asyncio
async def test_other_stage_handoff_and_sibling_restore_do_not_consume_restart():
    state = initial_state()
    state.update(current_card={"id": "card"}, performer_stage="reviewing", phase="dispatching",
                 lifecycle_sequence=["architecting", "reviewing"],
                 pending_override={"action": "restart", "target_stage": "architecting", "applied": True})
    await dispatch_module._finalise_success(state, {"session_id": "other"}, {
        "card": state["current_card"], "performer_stage": "reviewing", "card_context": {},
    })
    assert state["pending_override"]["target_stage"] == "architecting"
    sibling = initial_state()
    session_to_state(state_to_session(state), sibling)
    assert not dispatch_module._restart_requires_dispatch(sibling, "reviewing")
    session_to_state({"current_card": {"id": "sibling"}}, sibling)
    assert sibling["pending_override"] is None


@pytest.mark.parametrize("action", ["skip", "veto"])
def test_skip_and_veto_are_not_replayed_from_snapshot(action):
    state = initial_state()
    state.update(current_card={"id": "card"}, lifecycle_sequence=["implementing", "reviewing"],
                 performer_stage="implementing", pending_override={"action": action})
    assert _apply_pending_override(state) is not None
    fresh = restore(state)
    assert fresh["pending_override"] is None
    assert _apply_pending_override(fresh) is None
