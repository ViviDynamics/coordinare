"""#146: the run detail has its own address, is one click from the dashboard, and stays behind auth.

The browser behaviour (focus, Enter, Back, the links) is in tests/e2e/test_146_run_detail.py.
This covers the server route, the auth gate, the route registry, the glossary entry and the
client helpers under node.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path
from unittest.mock import MagicMock

from fastapi.testclient import TestClient
from pydantic import SecretStr

from coordinare import vocabulary
from coordinare.config import DashboardOidcConfig
from coordinare.dashboard import (
    _DASHBOARD_HTML,
    _DASHBOARD_JS_SOURCES,
    DashboardStore,
    create_dashboard_app,
)
from coordinare.dashboard_oidc import OidcFlow
from coordinare.localhost_guard import build_permitted
from coordinare.mcp.tools import is_deferred
from tests.unit.dashboard.oidc_double import CLIENT_ID, CLIENT_SECRET, ISSUER
from tests.unit.dashboard_js_slices import app_js, label_helpers_js, shipped_function
from tests.unit.test_dashboard import _make_app

_BASE = "http://127.0.0.1:8090"
_PERFORMERS = _DASHBOARD_JS_SOURCES["performers"]
_ACTIONS = _DASHBOARD_JS_SOURCES["actions"]
_NEW_CLIENT_FUNCTIONS = (
    ("performers", "isPerformersPath"),
    ("performers", "performerRoleFromPath"),
    ("performers", "performerRouteView"),
    ("performers", "viewRunLinkHtml"),
)


# --- the route ----------------------------------------------------------------- #


def test_run_detail_route_serves_the_same_shell_as_the_performers_page() -> None:
    client = _make_app()
    run_page = client.get("/performers/implementing")
    assert run_page.status_code == 200
    assert run_page.text == client.get("/performers").text
    assert 'id="performers-page"' in run_page.text


def test_run_detail_route_leaves_the_unknown_role_to_the_client() -> None:
    """An unknown role is a client-side not-found state, so the shell is still served."""
    assert _make_app().get("/performers/no-such-role").status_code == 200


def test_run_detail_route_is_auth_gated_like_the_other_pages() -> None:
    oidc = OidcFlow(DashboardOidcConfig(
        discovery_url=f"{ISSUER}/.well-known/openid-configuration",
        client_id=CLIENT_ID,
        client_secret=SecretStr(CLIENT_SECRET),
        redirect_url="https://dashboard.example/oidc/callback",
    ))
    daemon = MagicMock()
    daemon.state = {"phase": "idle", "error_count": 0}
    daemon.state_store.last_snapshot = None
    app = create_dashboard_app(
        DashboardStore(), daemon, MagicMock(), MagicMock(),
        permitted_origins=build_permitted(dashboard_host="127.0.0.1", dashboard_port=8090),
        oidc=oidc,
    )
    with TestClient(app, base_url=_BASE) as client:
        response = client.get("/performers/implementing", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"] == "/login"


# --- the route registry and the glossary ---------------------------------------- #


def test_run_detail_path_is_declared_deferred_in_the_mcp_registry() -> None:
    assert is_deferred("/performers/{role}")


def test_run_detail_is_a_glossary_concept() -> None:
    assert vocabulary.CONCEPTS["run_detail"].label == "Run detail"
    assert any(entry["label"] == "Run detail" for entry in vocabulary.payload()["concepts"])


# --- the wiring ----------------------------------------------------------------- #


def test_the_router_treats_every_performers_path_as_the_performers_page() -> None:
    app = app_js()
    assert "if (isPerformersPath(path)) {" in app
    # 139: both renders take the snapshot the symphony choice leaves, not the whole of it.
    assert "if (isPerformersPath(path)) renderPerformersPage(scoped);" in app
    assert "if (_lastState) renderPerformersPage(scopedView(_lastState));" in app
    assert "path === '/performers') renderPerformersPage" not in app


def test_the_role_selection_lives_in_the_url_not_in_module_globals() -> None:
    for source in (app_js(), _PERFORMERS):
        assert "_performersSelectedRole" not in source
        assert "_performersDetailOpen" not in source


def test_rows_show_a_chevron_and_the_page_styles_it() -> None:
    assert "row-chevron" in _PERFORMERS
    assert "td.row-chevron::after" in _DASHBOARD_HTML
    assert "tr.performers-row:hover" in _DASHBOARD_HTML


def test_every_run_link_uses_the_shared_helper() -> None:
    # The Active performers tile, the swimlane card and the card detail.
    assert _PERFORMERS.count("viewRunLinkHtml(sess) +") == 3
    assert "function viewRunLinkHtml(" in _PERFORMERS


def test_the_run_detail_helpers_use_no_dash_characters() -> None:
    dashes = (chr(0x2013), chr(0x2014))  # en dash, em dash; kept out of literals for RUF001
    for source_name, function_name in _NEW_CLIENT_FUNCTIONS:
        body = shipped_function(source_name, function_name)
        assert not any(dash in body for dash in dashes), function_name


# --- client helpers under node -------------------------------------------------- #


def test_run_detail_client_js(tmp_path: Path) -> None:
    """Runs under node, which the project already requires (spec 124)."""
    node = shutil.which("node")
    assert node is not None, "node is required for the dashboard JS gates"
    js_path = tmp_path / "run_detail.js"
    js_path.write_text(
        "\n\n".join(
            [shipped_function("helpers", "esc"), label_helpers_js()]
            + [shipped_function(source_name, name) for source_name, name in _NEW_CLIENT_FUNCTIONS],
        ),
        encoding="utf-8",
    )
    checks = Path(__file__).resolve().parents[1] / "js" / "run_detail_checks.js"
    result = subprocess.run([node, str(checks), str(js_path)], capture_output=True, text=True, timeout=60, check=False)
    report = result.stdout + result.stderr
    assert result.returncode == 0, f"client-side JS checks failed:\n{report}"
    summary = re.search(r"^SUMMARY (\d+) (\d+)$", result.stdout, re.MULTILINE)
    assert summary is not None, f"no summary line:\n{report}"
    assert int(summary.group(2)) == 0, report
    assert int(summary.group(1)) >= 20, f"only {summary.group(1)} checks ran:\n{report}"
