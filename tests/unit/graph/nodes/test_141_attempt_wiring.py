"""Attempt records follow real dispatch and terminal nodes across session copies."""
from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from coordinare.attempt_log import AttemptLog
from coordinare.daemon import CoordinareDaemon
from coordinare.graph.nodes.dispatch_performer import dispatch_performer
from coordinare.graph.nodes.merge_pr import merge_pr
from coordinare.graph.nodes.monitor_performer import _feedback_cycle_exhausted
from coordinare.session import session_to_state, state_to_session
from tests.unit.graph.nodes.test_dispatch_performer import _base_state, _Service
from tests.unit.graph.nodes.test_merge_pr import _GitHub


def _rows(path):
    return [json.loads(line) for f in sorted(path.glob("*.jsonl")) for line in f.read_text().splitlines()]


def _state(tmp_path):
    return _base_state(
        performer_services={"implementing": _Service()},
        performer_stage="implementing",
        lifecycle_sequence=["assessing", "implementing", "reviewing", "qa"],
        attempt_log=AttemptLog(tmp_path),
        current_card={"id": "ITEM_1", "status": "TODO", "description": "Given x when y then z"},
    )


@pytest.mark.asyncio
async def test_two_bounces_then_merge_emit_six_rows(tmp_path):
    state = _state(tmp_path)
    for cycle in range(3):
        state["agent_dispatch"] = {}
        state = await dispatch_performer(state)
        assert state["phase"] == "monitoring_performer"
        if cycle < 2:
            _feedback_cycle_exhausted(state, "ITEM_1", "reviewing", "changes_requested", [])
        # Emulate the daemon's private per-card state copy and canonical writeback.
        canonical = state_to_session(state)
        copied = dict(state)
        session_to_state(deepcopy(canonical), copied)
        state = copied
    state["github_service"] = _GitHub()
    state["current_card"]["pr_node_id"] = "PR_1"
    state["current_card"]["description"] = "Changed after dispatch"
    await merge_pr(state)
    rows = _rows(tmp_path)
    assert len(rows) == 6
    assert [r["verdict"] for r in rows[1::2]] == ["fail", "fail", "pass"]
    assert rows[2]["parent_attempt_id"] == rows[0]["attempt_id"]
    assert rows[4]["parent_attempt_id"] == rows[2]["attempt_id"]
    assert rows[-1]["terminal_state"] == "merged"
    assert all(r["spec_has_acceptance_tests"] for r in rows[::2])
    assert state["last_attempt_id"] is state["last_attempt_log_path"] is None


@pytest.mark.asyncio
async def test_dispatch_survives_snapshot_and_reopen(tmp_path):
    state = await dispatch_performer(_state(tmp_path))
    daemon = CoordinareDaemon(AsyncMock(), max_cycles=1, sleep_func=AsyncMock())
    daemon._state.update(state)
    daemon._state["active_sessions"] = {"ITEM_1": state_to_session(state)}
    snapshot = daemon._build_snapshot()
    saved = snapshot.active_sessions["ITEM_1"]
    assert saved.last_attempt_id == state["last_attempt_id"]
    assert saved.last_attempt_log_path == state["last_attempt_log_path"]
    restored = AttemptLog(tmp_path / "different-day")
    restored.reopen_attempt(saved.last_attempt_id, "ITEM_1", Path(saved.last_attempt_log_path))
    restored.close_attempt(saved.last_attempt_id, "pass", "human", "merged")
    rows = _rows(tmp_path)
    assert len(rows) == 2
    assert rows[-1]["started_at"] is None
    assert rows[-1]["wall_time_s"] is None


@pytest.mark.asyncio
async def test_infrastructure_retry_retains_attempt(tmp_path):
    state = await dispatch_performer(_state(tmp_path))
    first = state["last_attempt_id"]
    state["agent_dispatch"] = {}
    state["transient_error_cycles"] = 1
    await dispatch_performer(state)
    assert state["last_attempt_id"] == first
    assert len(_rows(tmp_path)) == 1


@pytest.mark.asyncio
async def test_failed_dispatch_emits_nothing(tmp_path):
    state = _state(tmp_path)
    state["performer_services"]["implementing"].dispatch_card = AsyncMock(
        return_value={"status": "error", "reason": "unavailable"},
    )
    result = await dispatch_performer(state)
    assert result["phase"] == "system_error"
    assert not _rows(tmp_path)


@pytest.mark.asyncio
async def test_daemon_fanout_writes_dispatch_identity_to_canonical_snapshot(tmp_path):
    state = _state(tmp_path)
    graph = AsyncMock()

    async def invoke(private_state):
        private_state["github_service"] = state["github_service"]
        return await dispatch_performer(private_state)

    graph.ainvoke.side_effect = invoke
    daemon = CoordinareDaemon(graph, max_cycles=1, sleep_func=AsyncMock())
    daemon._state.update(state)
    daemon._state["github_service"] = None
    daemon._state["board_snapshot"] = {"IN_PROGRESS": ["ITEM_1"]}
    daemon._state["active_sessions"] = {"ITEM_1": state_to_session(state)}
    await daemon._invoke_multi_session()
    assert graph.ainvoke.await_count == 1
    canonical = daemon._state["active_sessions"]["ITEM_1"]
    assert canonical["last_attempt_id"] == _rows(tmp_path)[0]["attempt_id"]
    assert daemon._build_snapshot().active_sessions["ITEM_1"].last_attempt_id == canonical["last_attempt_id"]


@pytest.mark.asyncio
async def test_write_failure_does_not_interrupt_dispatch(tmp_path, monkeypatch):
    import builtins
    state = _state(tmp_path)
    original_open = builtins.open

    def fail_logs(path, *args, **kwargs):
        if str(path).endswith(".jsonl"):
            raise OSError("read-only volume")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", fail_logs)
    result = await dispatch_performer(state)
    assert result["phase"] == "monitoring_performer"
    assert result["last_attempt_id"]
    assert result["last_attempt_log_path"]


def test_new_attempt_triggers_save_without_stage_or_phase_change():
    daemon = CoordinareDaemon(AsyncMock(), max_cycles=1, sleep_func=AsyncMock())
    session = {"performer_stage": "implementing", "last_attempt_id": "old"}
    daemon._state["active_sessions"] = {"ITEM_1": session}
    before = daemon._lifecycle_signature()
    session["last_attempt_id"] = "new"
    assert daemon._lifecycle_signature() != before


@pytest.mark.asyncio
async def test_terminal_token_limit_is_timeout(tmp_path):
    from coordinare.graph.nodes.monitor_performer import monitor_performer
    from tests.unit.graph.nodes.test_monitor_performer import _Performer

    state = await dispatch_performer(_state(tmp_path))
    state["performer_services"] = {"implementing": _Performer({"status": "token_limit"})}
    await monitor_performer(state)
    assert _rows(tmp_path)[-1]["verdict"] == "timeout"
    assert _rows(tmp_path)[-1]["verdict_source"] == "system"


@pytest.mark.asyncio
async def test_exhausted_system_error_is_not_content_failure(tmp_path):
    from coordinare.graph.nodes.handle_system_error import handle_system_error

    state = await dispatch_performer(_state(tmp_path))
    state["system_error_count"] = 3
    state["system_error_reason"] = "Docker unreachable"
    state["system_error_last_at"] = None
    await handle_system_error(state)
    assert _rows(tmp_path)[-1]["verdict"] == "error"
    assert _rows(tmp_path)[-1]["verdict_source"] == "system"


def test_duplicate_close_does_not_emit_a_second_end_row(tmp_path):
    log = AttemptLog(tmp_path)
    attempt_id = log.open_attempt("ITEM_1", "default_policy")
    log.close_attempt(attempt_id, "pass", "human", "merged")
    log.close_attempt(attempt_id, "pass", "human", "merged")
    assert len(_rows(tmp_path)) == 2


@pytest.mark.asyncio
async def test_human_content_feedback_starts_child_attempt(tmp_path):
    from coordinare.graph.nodes.classify_human_feedback import classify_human_feedback

    state = await dispatch_performer(_state(tmp_path))
    parent = state["last_attempt_id"]
    state["pending_reviews"] = [{"body": "The code has a bug in the error handling logic."}]
    await classify_human_feedback(state)
    await dispatch_performer(state)
    assert _rows(tmp_path)[1]["verdict"] == "fail"
    assert _rows(tmp_path)[2]["parent_attempt_id"] == parent


@pytest.mark.asyncio
async def test_workflow_permission_feedback_retains_attempt(tmp_path):
    state = await dispatch_performer(_state(tmp_path))
    first = state["last_attempt_id"]
    _feedback_cycle_exhausted(state, "ITEM_1", "implementing", "workflow_push_permission", [])
    state["agent_dispatch"] = {}
    await dispatch_performer(state)
    assert state["last_attempt_id"] == first
    assert len(_rows(tmp_path)) == 1


@pytest.mark.asyncio
async def test_exhausted_expired_session_is_timeout(tmp_path):
    from coordinare.graph.nodes.monitor_performer import monitor_performer
    from tests.unit.graph.nodes.test_monitor_performer import _Performer

    state = await dispatch_performer(_state(tmp_path))
    state["current_card"]["pr_node_id"] = "PR_1"
    state["system_error_count"] = 3
    state["performer_services"] = {"implementing": _Performer({"status": "session_expired"})}
    await monitor_performer(state)
    assert state["phase"] == "blocked"
    assert _rows(tmp_path)[-1]["verdict"] == "timeout"
