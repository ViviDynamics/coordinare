"""#365 (a): History and Logs live under one Activity page.

The server serves the two views at ``/activity/history`` and ``/activity/logs``, opens
``/activity`` on History, and redirects the old ``/history`` and ``/logs`` addresses
with their query string. The in-app router does the same for a link that is still
handed the old path. Browser behaviour is in tests/e2e/test_365_activity_page.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from coordinare.dashboard import _DASHBOARD_HTML, _DASHBOARD_JS_SOURCES
from coordinare.mcp.tools._registry import DEFERRED_PAGE_PATHS
from tests.unit.dashboard_js_slices import shipped_function
from tests.unit.test_dashboard_config_api import _make_client


@pytest.fixture
def client(temp_config_path: Path) -> TestClient:
    return _make_client(temp_config_path, yaml.safe_load(temp_config_path.read_text()))


@pytest.mark.parametrize("path", ["/activity/history", "/activity/logs"])
def test_each_activity_view_is_served_at_its_address(client: TestClient, path: str) -> None:
    response = client.get(path)
    assert response.status_code == 200
    assert 'id="activity-switch-history"' in response.text


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("/activity", "/activity/history"),
        ("/history", "/activity/history"),
        ("/logs", "/activity/logs"),
        ("/history?symphony=beta&card=7", "/activity/history?symphony=beta&card=7"),
        ("/logs?level=error", "/activity/logs?level=error"),
    ],
)
def test_the_old_and_bare_addresses_redirect_with_their_query(client: TestClient, old: str, new: str) -> None:
    response = client.get(old, follow_redirects=False)
    assert response.status_code in (301, 302, 307, 308)
    assert response.headers["location"] == new


def test_the_old_addresses_are_permanent_moves(client: TestClient) -> None:
    assert client.get("/history", follow_redirects=False).status_code == 301
    assert client.get("/logs", follow_redirects=False).status_code == 301


def test_the_nav_has_one_activity_item_in_place_of_history_and_logs() -> None:
    assert 'href="/activity"' in _DASHBOARD_HTML
    assert 'aria-label="Activity"' in _DASHBOARD_HTML
    assert 'href="/history"' not in _DASHBOARD_HTML
    assert 'href="/logs"' not in _DASHBOARD_HTML


def test_each_view_says_in_one_line_what_it_holds() -> None:
    html = _DASHBOARD_HTML
    assert 'data-activity-summary="history"' in html
    assert 'data-activity-summary="logs"' in html
    # The two lines name their sources, so a reader can tell the views apart.
    assert "run transcripts" in html
    assert "event store" in html


def test_the_switch_marks_the_view_shown() -> None:
    html = _DASHBOARD_HTML
    for view in ("history", "logs"):
        assert f'id="activity-switch-{view}"' in html
        assert f'href="/activity/{view}"' in html


def test_the_client_moves_the_old_and_bare_addresses_too() -> None:
    redirect = shipped_function("app", "redirectRetiredPages")
    for path in ("'/history'", "'/logs'", "'/activity'"):
        assert path in redirect
    assert "'/activity/history'" in redirect
    assert "'/activity/logs'" in redirect
    assert "location.search" in redirect


def test_no_script_still_checks_for_the_old_paths() -> None:
    for name, source in _DASHBOARD_JS_SOURCES.items():
        assert "pathname !== '/logs'" not in source, name
        assert "pathname === '/logs'" not in source, name
        assert "pathname !== '/history'" not in source, name
        assert "pathname === '/history'" not in source, name
        assert "path === '/logs'" not in source, name
        assert "path === '/history'" not in source, name


def test_the_mcp_registry_lists_the_new_addresses_and_keeps_the_old() -> None:
    for path in ("/activity", "/activity/history", "/activity/logs", "/history", "/logs"):
        assert path in DEFERRED_PAGE_PATHS, path
