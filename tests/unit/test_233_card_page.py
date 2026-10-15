"""#233: the card page route, its address check, and the registrations it needs.

The page is the dashboard shell at /cards/<symphony>/<card>. The route serves it for a plausible
address and answers 404 (with the shell, so the page can say so) for one the writer could not
have made. The page reads only the timeline endpoint; the browser behaviour is in
tests/e2e/test_233_card_page.py, and the client helpers are in test_233_card_page_js.py.
"""
from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from coordinare.dashboard import (
    _DASHBOARD_HTML,
    _DASHBOARD_JS_SOURCES,
    DashboardStore,
    create_dashboard_app,
)
from coordinare.dashboard.assets import _DASHBOARD_JS_ORDER, DASHBOARD_HTML_BUDGET_BYTES
from coordinare.dashboard.routers.pages import card_address_valid
from coordinare.dashboard_auth import _OIDC_PUBLIC_PATHS
from coordinare.lib.path_segments import UNATTRIBUTED
from coordinare.localhost_guard import build_permitted
from coordinare.mcp.tools import is_deferred
from coordinare.services.card_lifecycle import CardLifecycleLog

BASE = "http://127.0.0.1:8090"
TOKEN = "a-secret-operator-token-of-at-least-32-characters"
ALPHA = "alpha-sym"
CARD = "PVTI_1"
SHELL_MARKER = 'id="card-page"'


def _client() -> TestClient:
    daemon = MagicMock()
    daemon.state = {
        "phase": "idle",
        "error_count": 0,
        "symphony_configs": {ALPHA: SimpleNamespace()},
        "symphony_states": {},
        "lifecycle_sequence": [],
        "active_sessions": {},
        "card_lifecycle": CardLifecycleLog(),
    }
    daemon.state_store.last_snapshot = None
    app = create_dashboard_app(
        DashboardStore(), daemon, MagicMock(), MagicMock(),
        auth_token=SecretStr(TOKEN),
        permitted_origins=build_permitted(dashboard_host="127.0.0.1", dashboard_port=8090),
    )
    return TestClient(app, base_url=BASE, headers={"Authorization": f"Bearer {TOKEN}"})


@pytest.mark.parametrize(("symphony", "card"), [
    ("alpha-sym", "PVTI_1"),
    ("alpha.sym_2", "PVTI-9"),
    ("A", "B"),
])
def test_a_plausible_address_is_valid(symphony: str, card: str) -> None:
    assert card_address_valid(symphony, card) is True


@pytest.mark.parametrize(("symphony", "card"), [
    ("alpha-sym", "<script>"),
    ("alpha-sym", "a b"),
    ("alpha-sym", ""),
    ("alpha-sym", ".."),
    ("alpha-sym", ".hidden"),
    ("_conductor", "PVTI_1"),
    ("a/b", "PVTI_1"),
    ("alpha-sym", "x" * 300),
])
def test_an_address_the_writer_could_not_have_made_is_invalid(symphony: str, card: str) -> None:
    assert card_address_valid(symphony, card) is False


def test_a_card_address_serves_the_dashboard_shell() -> None:
    response = _client().get(f"/cards/{ALPHA}/{CARD}")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    assert SHELL_MARKER in response.text
    assert "/static/cardpage.js" in response.text


def test_a_hostile_card_segment_is_a_404_that_still_loads_the_shell() -> None:
    response = _client().get(f"/cards/{ALPHA}/%3Cscript%3Ealert(1)%3Cscript")
    assert response.status_code == 404
    assert SHELL_MARKER in response.text


def test_a_symphony_segment_the_writer_could_not_have_made_is_a_404() -> None:
    response = _client().get(f"/cards/%5Fconductor/{CARD}")
    assert response.status_code == 404
    assert SHELL_MARKER in response.text


def test_the_timeline_read_still_says_an_unknown_card_is_not_found() -> None:
    response = _client().get(f"/api/cards/{ALPHA}/NOPE/timeline")
    assert response.status_code == 404
    assert response.json()["error"] == "not_found"


def test_the_card_page_route_is_in_the_mcp_deferred_page_list() -> None:
    assert is_deferred("/cards/{symphony}/{card}")


def test_the_oidc_public_list_is_unchanged() -> None:
    assert frozenset({
        "/login", "/oidc/login", "/oidc/callback", "/oidc/logout", "/oidc/signed-out",
        "/static/coordinare-mark.svg", "/static/coordinare-conductor.webp",
    }) == _OIDC_PUBLIC_PATHS


def test_the_card_script_loads_after_the_timeline_and_before_the_application_script() -> None:
    order = list(_DASHBOARD_JS_ORDER)
    assert order.index("timeline") < order.index("cardpage") < order.index("app")


def test_the_card_script_is_served_under_its_content_hash() -> None:
    response = _client().get("/static/cardpage.js")
    assert response.status_code == 200
    assert "javascript" in response.headers["content-type"]


def test_the_page_stays_under_its_size_budget() -> None:
    assert len(_DASHBOARD_HTML.encode("utf-8")) <= DASHBOARD_HTML_BUDGET_BYTES


def test_the_run_routes_and_the_card_page_share_no_route_shadowing() -> None:
    daemon: Any = MagicMock()
    daemon.state = {"phase": "idle", "error_count": 0}
    app = create_dashboard_app(
        DashboardStore(), daemon, MagicMock(), MagicMock(),
        permitted_origins=build_permitted(dashboard_host="127.0.0.1", dashboard_port=8090),
    )
    paths = [getattr(route, "path", "") for route in app.routes]
    assert "/cards/{symphony}/{card}" in paths
    assert "/runs/{symphony}/{card}/{run}" in paths


# The address rule is the writer's: a segment is valid only when sanitize_segment returns it unchanged.
# Each value is the decoded segment the route receives, with whether the writer could have made it.
EDGE_SEGMENTS = [
    ("a/b", False), ("a b", False), ("abc.", False), ("-abc", True), ("-", True), ("a-", True),
    ("a..b", True), ("_abc", False), ("a_b_", False), ("x", True), ("1", True), ("a-b", True),
    ("ab_cd", True), ("unattributed", True), ("", False), (".", False), ("..", False),
    ("a\u00e9", False), ("a" * 120, True), ("a" * 121, False), ("a" * 119 + ".", False),
]


@pytest.mark.parametrize(("value", "valid"), EDGE_SEGMENTS)
def test_the_edge_cases_follow_the_writers_rule(value: str, valid: bool) -> None:
    assert card_address_valid("alpha-sym", value) is valid


@pytest.mark.parametrize("path", [
    "/cards/a%2Fb/c",
    "/cards/a/b/c",
    "/cards/alpha-sym",
    "/cards/a/b/c/d",
])
def test_a_card_path_of_the_wrong_shape_serves_the_shell_as_a_404(path: str) -> None:
    response = _client().get(path)
    assert response.status_code == 404
    assert SHELL_MARKER in response.text


def test_the_catch_all_never_answers_an_api_route() -> None:
    response = _client().get("/api/cards/x/y/z/w")
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    assert SHELL_MARKER not in response.text
    timeline = _client().get(f"/api/cards/{ALPHA}/NOPE/timeline")
    assert timeline.headers["content-type"].startswith("application/json")


def test_the_unattributed_constant_is_shared_with_the_client() -> None:
    match = re.search(r"^var CARD_PAGE_UNATTRIBUTED = '([^']*)';", _DASHBOARD_JS_SOURCES["cardpage"], re.MULTILINE)
    assert match is not None
    assert match.group(1) == UNATTRIBUTED
