from __future__ import annotations

from datetime import datetime

from coordinare.graph.state import initial_state


def test_initial_state_defaults() -> None:
    state = initial_state()
    assert state["current_card"] is None
    assert state["board_snapshot"] == {}
    assert state["phase"] == "idle"
    assert state["pending_reviews"] == []
    assert isinstance(state["last_poll_at"], datetime)
    assert state["error_count"] == 0
    assert state["github_field_cache"] == {}


def test_state_update_pattern() -> None:
    state = initial_state()
    state["phase"] = "dispatching"
    state["error_count"] += 1
    state["board_snapshot"] = {"TODO": ["PVI_1"]}

    assert state["phase"] == "dispatching"
    assert state["error_count"] == 1
    assert state["board_snapshot"]["TODO"] == ["PVI_1"]
