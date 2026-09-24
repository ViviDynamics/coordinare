"""349: the dashboard front end lives in /static JS files, not one inline string.

Gates the extraction of the shared helpers, the 081 config-editing UI and the
performers-page rendering out of ``_DASHBOARD_HTML``: the page references each
file through a content-hash ``?v=`` query, the static route serves exactly the
shipped source with immutable caching, the HTML budget comes back down, and the
extracted sources are syntax-checked (``node --check``) — the linting the
inline string could never have.
"""
from __future__ import annotations

import hashlib
import shutil
import subprocess
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient

from coordinare.dashboard import (
    _DASHBOARD_HTML,
    _DASHBOARD_JS_FILES,
    _DASHBOARD_JS_REVISIONS,
    _DASHBOARD_JS_SOURCES,
    DASHBOARD_HTML_BUDGET_BYTES,
    DashboardStore,
    create_dashboard_app,
)

# ---------------------------------------------------------------------------
# URL wiring: hashed script tags in the page, matching served sources
# ---------------------------------------------------------------------------


def test_dashboard_html_references_hashed_static_js() -> None:
    """The page must load each extracted file with its content-hash query."""
    for name in _DASHBOARD_JS_FILES:
        rev = _DASHBOARD_JS_REVISIONS[name]
        tag = f'<script src="/static/{name}.js?v={rev}"></script>'
        assert tag in _DASHBOARD_HTML, f"missing/unaltered script tag for {name}: {tag}"


def test_revisions_match_source_bytes() -> None:
    """The ?v= hash is the sha256 of the served bytes — a stale cache hit is
    impossible because a redeploy that changes the bytes changes the URL."""
    for name, source in _DASHBOARD_JS_SOURCES.items():
        expected = hashlib.sha256(source.encode("utf-8")).hexdigest()[:12]
        assert _DASHBOARD_JS_REVISIONS[name] == expected


# ---------------------------------------------------------------------------
# The static route
# ---------------------------------------------------------------------------


def _make_app() -> TestClient:
    store = DashboardStore()
    daemon = MagicMock()
    daemon.state = {"phase": "idle", "error_count": 0}
    daemon.state_store = MagicMock()
    daemon._cycle_active = False
    daemon.running = True
    metrics = MagicMock()
    metrics.cycles_completed_total._value.get.return_value = 0
    metrics.build_info.labels.return_value._value.get.return_value = {}
    health = MagicMock()
    health.snapshot.return_value.probes = []
    return TestClient(
        create_dashboard_app(store, daemon, metrics, health),
        base_url="http://127.0.0.1:8090",
    )


@pytest.mark.parametrize("name", list(_DASHBOARD_JS_FILES))
def test_static_js_route_serves_source(name: str) -> None:
    client = _make_app()
    response = client.get(f"/static/{name}.js")
    assert response.status_code == 200
    assert response.text == _DASHBOARD_JS_SOURCES[name]
    assert "immutable" in response.headers.get("cache-control", "")
    # Exact type: Starlette appends charset=utf-8 to any text/* type, so a
    # "text/javascript" mutant would pass a startswith/substring check.
    assert response.headers["content-type"] == "application/javascript; charset=utf-8"


def test_static_js_route_unknown_name_404() -> None:
    client = _make_app()
    assert client.get("/static/not-a-real-file.js").status_code == 404


# ---------------------------------------------------------------------------
# The budget comes back down
# ---------------------------------------------------------------------------


def test_dashboard_html_budget_lowered() -> None:
    """Extraction must lower the cap, not raise it: the whole point of #349.

    Equality on purpose: a raise here is a deliberate change that must edit
    this test, not one that slips through a `<=` re-derivation.
    """
    size = len(_DASHBOARD_HTML.encode())
    assert DASHBOARD_HTML_BUDGET_BYTES == 112 * 1024, (
        f"budget {DASHBOARD_HTML_BUDGET_BYTES} is not the lowered 112 KB cap"
    )
    assert size < DASHBOARD_HTML_BUDGET_BYTES, (
        f"_DASHBOARD_HTML is {size} bytes (limit: {DASHBOARD_HTML_BUDGET_BYTES})"
    )


# ---------------------------------------------------------------------------
# The extracted sources are lintable JS
# ---------------------------------------------------------------------------


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
@pytest.mark.parametrize("name", list(_DASHBOARD_JS_FILES))
def test_extracted_js_passes_node_check(name: str, tmp_path: Any) -> None:
    """node --check on every shipped source — impossible while the JS was a
    Python string ruff could not see."""
    path = tmp_path / f"{name}.js"
    path.write_text(_DASHBOARD_JS_SOURCES[name], encoding="utf-8")
    result = subprocess.run(
        ["node", "--check", str(path)], capture_output=True, text=True, timeout=60, check=False,
    )
    assert result.returncode == 0, f"node --check {name}.js:\n{result.stdout}{result.stderr}"


# ---------------------------------------------------------------------------
# The latent dead branch #348 could not see
# ---------------------------------------------------------------------------


def test_token_derivation_has_no_dead_session_stats_branch() -> None:
    """derivePerformerTokenTotal used to read session_stats.total_tokens, a
    field SessionStats (title/files_changed/lines_added/lines_removed) has
    never had. The branch could never fire, so it is gone."""
    assert "total_tokens" not in _DASHBOARD_JS_SOURCES["performers"]
