from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest

from coordinare.daemon import CoordinareDaemon, _persist_one_session, _restored_session_dict
from coordinare.graph.state import _retire_active_session, initial_state
from coordinare.session import session_to_state, state_to_session
from coordinare.state_store import PersistedSession, WorkflowSnapshot


def test_comment_revision_tracking_survives_snapshot_and_clears_between_cards() -> None:
    state = initial_state()
    card = {"id": "card", "pr_node_id": "PR"}
    tracking = {"pr_node_id": "PR", "updated_since": "2026-10-08T22:00:00Z", "versions": {"100": "digest"}}
    state.update(current_card=card, pr_comment_tracking=tracking)
    persisted = _persist_one_session("card", state_to_session(state))
    persisted = PersistedSession.model_validate_json(persisted.model_dump_json())
    restored = _restored_session_dict("card", persisted, WorkflowSnapshot(snapshot_at=datetime.now(UTC), phase="monitoring_pr"), card)
    replacement = initial_state()
    session_to_state(restored, replacement)
    assert replacement["pr_comment_tracking"] == tracking
    session_to_state({"current_card": {"id": "legacy"}}, replacement)
    assert replacement["pr_comment_tracking"] == {}
    replacement["pr_comment_tracking"] = tracking
    _retire_active_session(replacement)
    assert replacement["pr_comment_tracking"] == {}
    assert PersistedSession(card_id="legacy").pr_comment_tracking == {}


@pytest.mark.asyncio
async def test_comment_tracking_change_triggers_real_snapshot_save_gate() -> None:
    daemon = CoordinareDaemon(AsyncMock(), poll_interval_seconds=1, max_cycles=1)
    daemon._state_store = AsyncMock()
    session = {"current_card": {"id": "card"}, "phase": "monitoring_pr", "pr_comment_tracking": {}}
    daemon._state.update(active_sessions={"card": session}, current_card=session["current_card"], phase="monitoring_pr")
    signature = daemon._lifecycle_signature()
    session["pr_comment_tracking"] = {"pr_node_id": "PR", "updated_since": "2026-10-08T22:00:00Z", "versions": {"100": "digest"}}
    await daemon._save_snapshot_if_changed(signature)
    daemon._state_store.save.assert_awaited_once()
    snapshot = daemon._state_store.save.call_args.args[0]
    assert snapshot.active_sessions["card"].pr_comment_tracking == session["pr_comment_tracking"]
