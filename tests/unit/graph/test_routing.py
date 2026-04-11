from __future__ import annotations

from coordinare.graph.routing import (
    route_from_agent_status,
    route_from_assess,
    route_from_board_check,
    route_from_dispatch,
    route_from_review,
    route_from_system_error,
)


def test_route_from_board_check_paths() -> None:
    # Legacy path: no assessor in lifecycle → route to assess_card
    assert route_from_board_check({"phase": "dispatching"}) == "assess"
    assert route_from_board_check({"phase": "dispatching", "lifecycle_sequence": ["implementing"]}) == "assess"
    # Assessor performer in lifecycle → skip assess_card, go to dispatch
    assert route_from_board_check({"phase": "dispatching", "lifecycle_sequence": ["assessing", "implementing"]}) == "dispatch"
    assert route_from_board_check({"phase": "monitoring_pr"}) == "monitor_pr"
    assert route_from_board_check({"phase": "monitoring_agent"}) == "monitor_agent"
    assert route_from_board_check({"phase": "monitoring_performer"}) == "monitor_agent"  # 019
    assert route_from_board_check({"phase": "blocked"}) == "blocked"
    assert route_from_board_check({"phase": "system_error"}) == "handle_system_error"
    assert route_from_board_check({}) == "idle"


def test_route_from_review_paths() -> None:
    assert route_from_review({"phase": "merging"}) == "merge"
    assert route_from_review({"phase": "blocked"}) == "blocked"
    assert route_from_review({"pending_reviews": [{"id": "1"}]}) == "relay"
    assert route_from_review({}) == "monitor"


def test_route_from_agent_status_paths() -> None:
    assert route_from_agent_status({"phase": "monitoring_pr"}) == "review"
    assert route_from_agent_status({"phase": "blocked"}) == "blocked"
    assert route_from_agent_status({"phase": "system_error"}) == "handle_system_error"
    assert route_from_agent_status({"phase": "dispatching"}) == "dispatch"  # 019: lifecycle advancement
    assert route_from_agent_status({}) == "monitor"


def test_route_from_assess_paths() -> None:
    assert route_from_assess({"phase": "blocked"}) == "blocked"
    assert route_from_assess({"phase": "dispatching"}) == "dispatch"
    assert route_from_assess({}) == "dispatch"


def test_route_from_dispatch_paths() -> None:
    assert route_from_dispatch({"phase": "blocked"}) == "blocked"
    assert route_from_dispatch({"phase": "system_error"}) == "handle_system_error"
    assert route_from_dispatch({"phase": "monitoring_agent"}) == "notify"
    assert route_from_dispatch({}) == "notify"


def test_route_from_system_error_paths() -> None:
    assert route_from_system_error({"phase": "dispatching"}) == "dispatch"
    assert route_from_system_error({"phase": "idle"}) == "idle"
    assert route_from_system_error({}) == "idle"
