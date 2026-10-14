"""133 and 132 share the ``/api/runs`` prefix.

``/api/runs/{run_id}/log`` (a run's full log) and ``/api/runs/{symphony}/{card}`` (a
card's transcripts) have the same shape, so the first one registered answers a request
for a run's log. The log route must come first, or the viewer would take its requests.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from fastapi.routing import APIRoute

from coordinare.dashboard import DashboardStore, create_dashboard_app
from coordinare.localhost_guard import build_permitted

LOG_ROUTE = "/api/runs/{run_id}/log"
CARD_RUNS_ROUTE = "/api/runs/{symphony}/{card}"


def test_the_run_log_route_is_matched_before_the_card_transcripts_route() -> None:
    daemon = MagicMock()
    daemon.state = {"phase": "idle", "error_count": 0}
    app = create_dashboard_app(
        DashboardStore(), daemon, MagicMock(), MagicMock(),
        permitted_origins=build_permitted(dashboard_host="127.0.0.1", dashboard_port=8090),
    )
    paths = [route.path for route in app.routes if isinstance(route, APIRoute)]
    assert LOG_ROUTE in paths
    assert CARD_RUNS_ROUTE in paths
    assert paths.index(LOG_ROUTE) < paths.index(CARD_RUNS_ROUTE)
