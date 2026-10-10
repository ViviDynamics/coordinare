from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from coordinare.daemon import CoordinareDaemon
from coordinare.graph.nodes.check_board import _pickup_todo_cards
from coordinare.state_store import PersistedSession, WorkflowSnapshot


@pytest.mark.asyncio
@pytest.mark.parametrize("configured,saved,expected", [
    (["assessing", "implementing", "reviewing", "qa", "closing_review"], ["implementing"], "assessing"),
    (["implementing"], ["assessing", "implementing"], "implementing"),
    (["assessing", "implementing"], ["assessing", "implementing"], "assessing"),
    (["assessing", "implementing"], [], "assessing"),
    ([], ["assessing", "implementing"], "assessing"),
])
async def test_new_todo_after_restart_uses_current_configured_lifecycle(configured, saved, expected):
    daemon = CoordinareDaemon(AsyncMock(), poll_interval_seconds=1)
    daemon._state["lifecycle_sequence"] = configured
    if configured:
        daemon._state["config"] = SimpleNamespace(max_concurrent_cards=1)
    daemon._restore_from_snapshot(WorkflowSnapshot(
        snapshot_at=datetime.now(UTC), phase="idle", lifecycle_sequence=saved,
    ))
    await _pickup_todo_cards(daemon._state, {"titles": {"new-card": "Small new task"}}, ["new-card"])
    assert daemon._state["active_sessions"]["new-card"]["performer_stage"] == expected


@pytest.mark.asyncio
async def test_current_lifecycle_keeps_existing_session_stage_feedback_and_pr():
    daemon = CoordinareDaemon(AsyncMock(), poll_interval_seconds=1)
    daemon._state.update(lifecycle_sequence=["assessing", "implementing", "reviewing", "qa", "closing_review"], config=SimpleNamespace(max_concurrent_cards=2))
    snapshot = WorkflowSnapshot(
        snapshot_at=datetime.now(UTC), phase="monitoring_pr",
        active_card_id="existing", active_card_column="IN_REVIEW",
        lifecycle_sequence=["implementing"], performer_stage="implementing",
        pr_url="https://github.com/example/sample/pull/1", pr_node_id="PR_existing",
        active_sessions={"existing": PersistedSession(
            card_id="existing", phase="monitoring_pr", performer_stage="implementing",
            relay_feedback=[{"message": "Keep prior acceptance criteria"}],
        )},
    )
    daemon._restore_from_snapshot(snapshot)
    await _pickup_todo_cards(daemon._state, {"titles": {"new-card": "Small new task"}}, ["new-card"])
    sessions = daemon._state["active_sessions"]
    assert sessions["new-card"]["performer_stage"] == "assessing"
    assert sessions["existing"]["performer_stage"] == "implementing"
    assert sessions["existing"]["phase"] == "monitoring_pr"
    assert sessions["existing"]["current_card"]["pr_node_id"] == "PR_existing"
    assert sessions["existing"]["relay_feedback"] == [{"message": "Keep prior acceptance criteria"}]


@pytest.mark.asyncio
async def test_removed_active_stage_keeps_saved_continuation_across_two_restarts():
    from coordinare.graph.nodes.monitor.verdict import _advance_stage
    from coordinare.session import session_to_state, state_to_session

    daemon = CoordinareDaemon(AsyncMock(), poll_interval_seconds=1)
    daemon._state.update(lifecycle_sequence=["implementing"], config=SimpleNamespace(max_concurrent_cards=2))
    snapshot = WorkflowSnapshot(
        snapshot_at=datetime.now(UTC), phase="monitoring_performer",
        lifecycle_sequence=["assessing", "implementing", "reviewing"],
        active_sessions={"existing": PersistedSession(
            card_id="existing", phase="monitoring_performer", performer_stage="assessing",
            agent_session_id="old-assessor",
        )},
    )
    daemon._restore_from_snapshot(snapshot)
    existing = daemon._state["active_sessions"]["existing"]
    session_to_state(existing, daemon._state)
    update = _advance_stage(daemon._state)
    assert update["phase"] == "dispatching"
    assert update["performer_stage"] == "implementing"
    daemon._state.update(update)
    daemon._state["active_sessions"]["existing"] = state_to_session(daemon._state)
    saved = daemon._build_snapshot()
    restarted = CoordinareDaemon(AsyncMock(), poll_interval_seconds=1)
    restarted._state.update(lifecycle_sequence=["implementing"], config=SimpleNamespace(max_concurrent_cards=2))
    restarted._restore_from_snapshot(saved)
    session_to_state(restarted._state["active_sessions"]["existing"], restarted._state)
    assert _advance_stage(restarted._state)["performer_stage"] == "reviewing"
    await _pickup_todo_cards(restarted._state, {"titles": {"new-card": "New task"}}, ["new-card"])
    session_to_state(restarted._state["active_sessions"]["new-card"], restarted._state)
    assert _advance_stage(restarted._state)["phase"] == "monitoring_pr"


@pytest.mark.asyncio
async def test_legacy_snapshot_migrates_continuation_to_versioned_contract(tmp_path):
    import json
    from pathlib import Path

    import jsonschema

    from coordinare.metrics import CoordinareMetrics
    from coordinare.state_store import CURRENT_SCHEMA_VERSION, StateStore

    assert CURRENT_SCHEMA_VERSION == 33
    path = tmp_path / "state.json"
    path.write_text(json.dumps({
        "schema_version": 31, "snapshot_at": datetime.now(UTC).isoformat(), "phase": "idle",
        "lifecycle_sequence": ["assessing", "implementing", "reviewing"],
        "active_sessions": {"existing": {"card_id": "existing", "performer_stage": "assessing", "phase": "dispatching"}},
    }))
    store = StateStore(path=path, metrics=CoordinareMetrics())
    legacy = await store.load()
    assert legacy.active_sessions["existing"].lifecycle_continuation == []
    daemon = CoordinareDaemon(AsyncMock(), poll_interval_seconds=1)
    daemon._state.update(lifecycle_sequence=["implementing"], config=SimpleNamespace(max_concurrent_cards=2))
    daemon._restore_from_snapshot(legacy)
    await store.save(daemon._build_snapshot())
    saved = json.loads(path.read_text())
    assert saved["schema_version"] == CURRENT_SCHEMA_VERSION
    schema = json.loads((Path(__file__).resolve().parents[2] / "specs/003-state-persistence/contracts/workflow-snapshot.schema.json").read_text())
    assert CURRENT_SCHEMA_VERSION in schema["properties"]["schema_version"]["enum"]
    declared = schema["properties"]["active_sessions"]["additionalProperties"]["properties"]["lifecycle_continuation"]
    assert declared["items"]["type"] == "string"
    jsonschema.validate(saved, schema)
    restored = await store.load()
    assert restored.active_sessions["existing"].lifecycle_continuation == ["assessing", "implementing", "reviewing"]


@pytest.mark.parametrize("saved_continuation", [[], ["implementing", "reviewing"]])
@pytest.mark.parametrize("completed_phase", ["monitoring_pr", "idle"])
def test_completed_lifecycle_does_not_reconstruct_removed_stage_continuation(saved_continuation, completed_phase):
    daemon = CoordinareDaemon(AsyncMock(), poll_interval_seconds=1)
    daemon._state.update(lifecycle_sequence=["implementing", "qa"], config=SimpleNamespace(max_concurrent_cards=2))
    daemon._restore_from_snapshot(WorkflowSnapshot(
        snapshot_at=datetime.now(UTC), phase="monitoring_pr",
        lifecycle_sequence=["implementing", "reviewing"],
        active_sessions={"existing": PersistedSession(
            card_id="existing", phase=completed_phase, performer_stage="reviewing",
            lifecycle_continuation=saved_continuation, lifecycle_completed_at=datetime.now(UTC),
        )},
    ))
    assert daemon._state["active_sessions"]["existing"]["lifecycle_continuation"] == []


def test_completed_continuation_does_not_skip_current_stages_on_later_feedback():
    from coordinare.graph.nodes.monitor.verdict import _advance_stage

    state = {
        "performer_stage": "reviewing", "phase": "monitoring_performer",
        "lifecycle_sequence": ["implementing", "qa", "closing_review"],
        "lifecycle_continuation": ["assessing", "implementing", "reviewing"],
        "current_card": {"id": "existing", "status": "IN_PROGRESS"},
    }
    state.update(_advance_stage(state))
    assert state["phase"] == "monitoring_pr"
    state.update(performer_stage="implementing", phase="monitoring_performer")
    assert _advance_stage(state)["performer_stage"] == "qa"


def test_final_lint_bounce_keeps_unfinished_continuation(monkeypatch):
    from coordinare.graph.nodes.monitor import verdict

    state = {"performer_stage": "reviewing", "lifecycle_sequence": ["implementing", "qa"],
             "lifecycle_continuation": ["implementing", "reviewing"],
             "current_card": {"id": "existing", "status": "IN_PROGRESS"}}
    monkeypatch.setattr(verdict, "_ci_lint_gate", lambda state, card: {"phase": "dispatching", "performer_stage": "implementing"})
    update = verdict._advance_stage(state)
    state.update(update)
    assert state["phase"] == "dispatching"
    assert state["lifecycle_continuation"] == ["implementing", "reviewing"]
    assert "lifecycle_completed_at" not in update


@pytest.mark.asyncio
@pytest.mark.parametrize("saved_continuation", [[], ["assessing", "implementing", "reviewing"]])
async def test_unfinished_idle_todo_keeps_removed_role_continuation_across_restart(saved_continuation):
    from coordinare.graph.nodes.monitor.verdict import _advance_stage
    from coordinare.session import session_to_state, state_to_session

    daemon = CoordinareDaemon(AsyncMock(), poll_interval_seconds=1)
    daemon._state.update(lifecycle_sequence=["implementing"], config=SimpleNamespace(max_concurrent_cards=2))
    daemon._restore_from_snapshot(WorkflowSnapshot(
        snapshot_at=datetime.now(UTC), phase="idle", lifecycle_sequence=["assessing", "implementing", "reviewing"],
        active_sessions={"existing": PersistedSession(card_id="existing", performer_stage="assessing", phase="idle",
                                                      lifecycle_continuation=saved_continuation)},
    ))
    session = daemon._state["active_sessions"]["existing"]
    assert session["lifecycle_continuation"] == ["assessing", "implementing", "reviewing"]
    await _pickup_todo_cards(daemon._state, {"titles": {"existing": "Unfinished task"}}, ["existing"])
    session_to_state(session, daemon._state)
    update = _advance_stage(daemon._state)
    assert update["phase"] == "dispatching"
    assert update["performer_stage"] == "implementing"
    daemon._state.update(update)
    daemon._state["active_sessions"]["existing"] = state_to_session(daemon._state)
    restarted = CoordinareDaemon(AsyncMock(), poll_interval_seconds=1)
    restarted._state.update(lifecycle_sequence=["implementing"], config=SimpleNamespace(max_concurrent_cards=2))
    restarted._restore_from_snapshot(daemon._build_snapshot())
    session_to_state(restarted._state["active_sessions"]["existing"], restarted._state)
    assert _advance_stage(restarted._state)["performer_stage"] == "reviewing"
