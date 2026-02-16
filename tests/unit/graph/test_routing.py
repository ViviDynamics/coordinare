from __future__ import annotations

from coordinare.graph.routing import (
    route_from_agent_status,
    route_from_board_check,
    route_from_review,
)


def test_route_from_board_check_paths() -> None:
    assert route_from_board_check({"phase": "dispatching"}) == "dispatch"
    assert route_from_board_check({"phase": "monitoring_pr"}) == "monitor_pr"
    assert route_from_board_check({"phase": "monitoring_agent"}) == "monitor_agent"
    assert route_from_board_check({}) == "idle"


def test_route_from_review_paths() -> None:
    assert route_from_review({"phase": "merging"}) == "merge"
    assert route_from_review({"phase": "blocked"}) == "blocked"
    assert route_from_review({"pending_reviews": [{"id": "1"}]}) == "relay"
    assert route_from_review({}) == "monitor"


def test_route_from_agent_status_paths() -> None:
    assert route_from_agent_status({"phase": "monitoring_pr"}) == "review"
    assert route_from_agent_status({"phase": "blocked"}) == "blocked"
    assert route_from_agent_status({}) == "monitor"
