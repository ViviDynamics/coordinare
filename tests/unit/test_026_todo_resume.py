from __future__ import annotations

from typing import Any

import pytest

from coordinare.graph.nodes.check_board import _reset_and_rehydrate


def blocked_session() -> dict[str, Any]:
    return {
        "phase": "blocked", "board_paused": False,
        "current_card": {"id": "B", "status": "TODO", "previous_status": "BLOCKED",
                         "pr_url": "https://github.example/org/repo/pull/7",
                         "pushed_branch": "conductor/card-b/existing", "head_after": "current-head"},
        "open_questions": ["Previous interrupted attempt"],
        "feedback_cycle_count": 2, "content_feedback_cycles": 2, "transient_error_cycles": 1,
        "total_feedback_cycles": 14, "triage_blocks": 3,
        "dispatched_feedback": {"reviews": [{"body": "Retained PR change request"}]},
        "card_clarifications": [{"body": "Existing B answer", "author": "human"}],
        "assessor_open_questions": ["Previous attempt question"],
    }


@pytest.mark.asyncio
async def test_explicit_todo_resume_uses_blocked_phase_after_status_reconciliation() -> None:
    session = blocked_session()
    state: dict[str, Any] = {"phase": "blocked", "active_card_id": "B", "active_sessions": {"B": session}}
    await _reset_and_rehydrate(state, {"TODO": ["B"]}, ["B"], 1, state["active_sessions"])
    assert session["phase"] == "dispatching"
    assert state["phase"] == "dispatching"
    assert session["feedback_cycle_count"] == 0
    assert session["content_feedback_cycles"] == 0
    assert session["transient_error_cycles"] == 0
    assert session["open_questions"] == []
    assert session["total_feedback_cycles"] == 14
    assert session["triage_blocks"] == 3
    assert session["dispatched_feedback"] == {"reviews": [{"body": "Retained PR change request"}]}
    assert session["card_clarifications"] == [{"body": "Existing B answer", "author": "human"}]
    assert session["current_card"]["pr_url"] == "https://github.example/org/repo/pull/7"
    assert session["current_card"]["pushed_branch"] == "conductor/card-b/existing"
    assert session["current_card"]["head_after"] == "current-head"


@pytest.mark.asyncio
async def test_todo_does_not_dispatch_before_owned_pause_is_cleared() -> None:
    session = blocked_session()
    session["board_paused"] = True
    state: dict[str, Any] = {"active_sessions": {"B": session}}
    await _reset_and_rehydrate(state, {"TODO": ["B"]}, ["B"], 1, state["active_sessions"])
    assert session["phase"] == "blocked"
    assert session["feedback_cycle_count"] == 2
    assert session["open_questions"] == ["Previous interrupted attempt"]


@pytest.mark.asyncio
async def test_noneligible_blocked_session_keeps_its_hold() -> None:
    session = blocked_session()
    state: dict[str, Any] = {"active_sessions": {"B": session}}
    await _reset_and_rehydrate(state, {"BLOCKED": ["B"]}, [], 1, state["active_sessions"])
    assert session["phase"] == "blocked"
    assert session["feedback_cycle_count"] == 2
    assert session["open_questions"] == ["Previous interrupted attempt"]


@pytest.mark.asyncio
async def test_active_monitoring_session_does_not_get_a_second_dispatch() -> None:
    session = blocked_session()
    session["phase"] = "monitoring_performer"
    session["agent_dispatch"] = {"session_id": "live-worker"}
    state: dict[str, Any] = {"active_sessions": {"B": session}}
    await _reset_and_rehydrate(state, {"TODO": ["B"]}, ["B"], 1, state["active_sessions"])
    assert session["phase"] == "monitoring_performer"
    assert session["agent_dispatch"] == {"session_id": "live-worker"}
    assert session["feedback_cycle_count"] == 2
