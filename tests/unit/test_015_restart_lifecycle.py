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
