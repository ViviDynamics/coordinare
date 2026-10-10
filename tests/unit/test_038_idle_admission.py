from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest

from coordinare.graph.nodes.check_board import _count_slot_consuming_sessions, _pickup_todo_cards


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [1, 2])
async def test_paused_completed_and_blocked_history_releases_fresh_todo_capacity(limit: int) -> None:
    sessions = {
        "paused": {"phase": "idle", "current_card": {"id": "paused", "status": "BACKLOG"},
                   "dispatched_feedback_batch": [{"body": "Keep the original requested change"}]},
        "completed": {"phase": "idle", "current_card": {"id": "completed", "status": "DONE"},
                      "lifecycle_completed_at": "2026-10-10T06:00:00Z"},
        "question": {"phase": "blocked", "current_card": {"id": "question", "status": "BLOCKED"},
                     "open_questions": ["What should empty input do?"]},
    }
    retained = deepcopy(sessions)
    state = {"config": SimpleNamespace(max_concurrent_cards=limit), "active_sessions": sessions,
             "lifecycle_sequence": ["assessing", "implementing"], "phase": "blocked"}
    result = await _pickup_todo_cards(state, {"titles": {"fresh": "Fresh story", "sibling": "Sibling"}},
                                    ["fresh", "sibling"])
    assert "fresh" in result["active_sessions"]
    assert result["active_sessions"]["fresh"]["performer_stage"] == "assessing"
    assert ("sibling" in result["active_sessions"]) is (limit == 2)
    assert {cid: result["active_sessions"][cid] for cid in retained} == retained


@pytest.mark.parametrize("phase", ["idle", "done", "env_blocked", "blocked", "monitoring_pr"])
@pytest.mark.parametrize("side", [
    {"status": "running", "session_id": "owned-side"},
    {"status": "failed", "writer_active": True, "session_id": "owned-side"},
    {"status": "waiting", "writer_active": False, "session_id": "owned-side"},
    {"status": "running", "writer_active": True},
])
def test_passive_history_releases_capacity_but_owned_live_side_writer_retains_it(phase: str, side: dict) -> None:
    state = {"active_sessions": {"held": {"phase": phase, "documenting_side": side}}}
    live = bool(side.get("session_id")) and (side.get("status") == "running" or side.get("writer_active"))
    assert _count_slot_consuming_sessions(state) == int(live)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["dispatching", "monitoring_performer", "monitoring_agent"])
async def test_active_foreground_work_keeps_its_slot_and_does_not_admit_a_sibling(phase: str) -> None:
    session = {"phase": phase, "current_card": {"id": "active", "status": "IN_PROGRESS"},
               "agent_dispatch": {"session_id": "existing", "performer_id": "same-worker"}}
    state = {"config": SimpleNamespace(max_concurrent_cards=1), "active_sessions": {"active": session},
             "lifecycle_sequence": ["assessing"], "phase": phase}
    result = await _pickup_todo_cards(state, {}, ["fresh"])
    assert "fresh" not in result["active_sessions"]
    assert result["active_sessions"]["active"] is session
    assert session["agent_dispatch"]["session_id"] == "existing"


@pytest.mark.asyncio
async def test_idle_todo_retry_is_rehydrated_before_capacity_is_counted() -> None:
    existing = {"phase": "idle", "current_card": {"id": "retry", "status": "TODO"},
                "processed_review_ids": ["original-review"], "feedback_cycle_count": 1}
    state = {"config": SimpleNamespace(max_concurrent_cards=1), "active_sessions": {"retry": existing},
             "lifecycle_sequence": ["assessing"], "phase": "idle"}
    result = await _pickup_todo_cards(state, {}, ["retry", "fresh"])
    assert result["active_sessions"]["retry"]["phase"] == "dispatching"
    assert "fresh" not in result["active_sessions"]
    assert existing["processed_review_ids"] == ["original-review"]
    assert existing["feedback_cycle_count"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("side", [None, {"status": "running", "session_id": "owned-side"}])
async def test_explicit_paused_todo_history_only_holds_capacity_for_a_live_side_writer(side: dict | None) -> None:
    paused = {"phase": "idle", "board_paused": True, "current_card": {"id": "paused", "status": "TODO"},
              "agent_dispatch": {"session_id": "historical-worker"}, "documenting_side": side}
    state = {"config": SimpleNamespace(max_concurrent_cards=1), "active_sessions": {"paused": paused},
             "lifecycle_sequence": ["assessing"], "phase": "idle"}
    result = await _pickup_todo_cards(state, {}, ["paused", "fresh"])
    assert ("fresh" in result["active_sessions"]) is (side is None)
    assert paused["phase"] == "idle"
    assert paused["board_paused"] is True
    assert paused["agent_dispatch"]["session_id"] == "historical-worker"


@pytest.mark.parametrize("status", ["BACKLOG", "BLOCKED", "DONE", "CLOSED", "CANCELED"])
def test_stale_queued_terminal_or_held_card_does_not_keep_admission_capacity(status: str) -> None:
    session = {"phase": "dispatching", "current_card": {"id": "old", "status": status},
               "agent_dispatch": {"session_id": "historical-worker"}}
    assert _count_slot_consuming_sessions({"active_sessions": {"old": session}}) == 0


@pytest.mark.parametrize("phase", ["monitoring_performer", "monitoring_agent"])
def test_owned_foreground_worker_keeps_capacity_during_board_pause_or_completion(phase: str) -> None:
    session = {"phase": phase, "board_paused": True, "current_card": {"id": "old", "status": "DONE"},
               "agent_dispatch": {"session_id": "still-running-worker"}}
    assert _count_slot_consuming_sessions({"active_sessions": {"old": session}}) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("saved_status", ["DONE", "BLOCKED"])
@pytest.mark.parametrize("phase,live_status", [
    ("monitoring_agent", "IN_PROGRESS"),
    ("dispatching", "IN_PROGRESS"),
    ("dispatching", "IN_REVIEW"),
])
@pytest.mark.parametrize("board_id", ["retained", "retained-content"])
async def test_fresh_active_board_status_keeps_restored_work_capacity(
    saved_status: str, phase: str, live_status: str, board_id: str,
) -> None:
    retained = {"phase": phase,
                "current_card": {"id": "retained", "content_id": "retained-content", "status": saved_status},
                "processed_review_ids": ["original-review"],
                "dispatched_feedback": {"stage": "implementing", "items": [{"body": "Keep the requested change"}]}}
    state = {"config": SimpleNamespace(max_concurrent_cards=1),
             "active_sessions": {"retained": retained}, "lifecycle_sequence": ["assessing"],
             "board_snapshot": {live_status: [board_id], "TODO": ["fresh"]}}
    result = await _pickup_todo_cards(state, {}, ["fresh"])
    assert "fresh" not in result["active_sessions"]
    assert result["active_sessions"]["retained"] is retained
    assert retained["processed_review_ids"] == ["original-review"]
    assert retained["dispatched_feedback"]["items"][0]["body"] == "Keep the requested change"


@pytest.mark.asyncio
@pytest.mark.parametrize("live_status", ["BACKLOG", "BLOCKED", "DONE", "CLOSED", "CANCELED"])
async def test_fresh_held_board_status_releases_stale_queued_work(live_status: str) -> None:
    retained = {"phase": "dispatching", "current_card": {"id": "retained", "status": "IN_PROGRESS"}}
    state = {"config": SimpleNamespace(max_concurrent_cards=1),
             "active_sessions": {"retained": retained}, "lifecycle_sequence": ["assessing"],
             "board_snapshot": {live_status: ["retained"], "TODO": ["fresh"]}}
    result = await _pickup_todo_cards(state, {}, ["fresh"])
    assert result["active_sessions"]["fresh"]["performer_stage"] == "assessing"
    assert result["active_sessions"]["retained"] is retained


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["idle", "done", "env_blocked"])
@pytest.mark.parametrize("saved_status", ["DONE", "BLOCKED", "TODO"])
async def test_live_in_progress_retained_history_holds_board_admission(phase: str, saved_status: str) -> None:
    retained = {"phase": phase, "current_card": {"id": "retained", "status": saved_status},
                "processed_review_ids": ["original-review"]}
    state = {"config": SimpleNamespace(max_concurrent_cards=1),
             "active_sessions": {"retained": retained}, "lifecycle_sequence": ["assessing"],
             "board_snapshot": {"IN_PROGRESS": ["retained"], "TODO": ["fresh"]}}
    result = await _pickup_todo_cards(state, {}, ["fresh"])
    assert "fresh" not in result["active_sessions"]
    assert result["active_sessions"]["retained"] is retained
    assert retained["processed_review_ids"] == ["original-review"]


@pytest.mark.asyncio
async def test_explicitly_paused_history_still_releases_capacity_on_live_in_progress_board() -> None:
    retained = {"phase": "idle", "board_paused": True, "current_card": {"id": "retained", "status": "DONE"}}
    state = {"config": SimpleNamespace(max_concurrent_cards=1),
             "active_sessions": {"retained": retained}, "lifecycle_sequence": ["assessing"],
             "board_snapshot": {"IN_PROGRESS": ["retained"], "TODO": ["fresh"]}}
    result = await _pickup_todo_cards(state, {}, ["fresh"])
    assert result["active_sessions"]["fresh"]["performer_stage"] == "assessing"
    assert retained["board_paused"] is True
