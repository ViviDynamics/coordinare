"""Spec 551 T012: the registry completeness execution map.

Asserts the full tool contract from ``contracts/mcp-tools.md``. This test is
designed to FAIL until every story's tools land; per-story progress shows up as
registered tools appearing in ``REGISTRY``.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi.routing import APIRoute
from mcp.types import ToolAnnotations

from coordinare.dashboard import DashboardStore, create_dashboard_app
from coordinare.mcp.tools import DEFERRED_PAGE_PATHS, DEFERRED_REST_PREFIXES, REGISTRY, find_tool


def _ann(read_only: bool, destructive: bool, idempotent: bool) -> ToolAnnotations:
    return ToolAnnotations(
        readOnlyHint=read_only, destructiveHint=destructive, idempotent_hint=idempotent,
    )


EXPECTED_TOOLS: dict[str, dict[str, Any]] = {
    # config
    "get_config": {
        "domain": "config",
        "annotations": _ann(True, False, True),
        "rest_calls": [
            ("GET", "/api/config/all"),
            ("GET", "/api/config/effective"),
            ("GET", "/api/config/global"),
            ("GET", "/api/config/section/{section_id}"),
            ("GET", "/api/config/catalog/{catalog}"),
            ("GET", "/api/config/routing"),
        ],
    },
    "update_config": {
        "domain": "config",
        "annotations": _ann(False, True, False),
        "rest_calls": [
            ("PUT", "/api/config/global"),
            ("PUT", "/api/config/section/{section_id}"),
            ("POST", "/api/config/catalog/{catalog}"),
            ("PUT", "/api/config/catalog/{catalog}/{item_id}"),
            ("DELETE", "/api/config/catalog/{catalog}/{item_id}"),
            ("POST", "/api/config/routing/entry"),
            ("PUT", "/api/config/routing/entry/{index}"),
            ("DELETE", "/api/config/routing/entry/{index}"),
            ("POST", "/api/config/reload"),
        ],
    },
    "validate_config": {
        "domain": "config",
        "annotations": _ann(True, False, True),
        "rest_calls": [("POST", "/api/config/validate")],
    },
    # symphonies
    "list_symphonies": {
        "domain": "symphonies",
        "annotations": _ann(True, False, True),
        "rest_calls": [("GET", "/api/symphonies")],
    },
    "get_symphony": {
        "domain": "symphonies",
        "annotations": _ann(True, False, True),
        "rest_calls": [("GET", "/api/symphonies/{name}")],
    },
    "upsert_symphony": {
        "domain": "symphonies",
        "annotations": _ann(False, True, True),
        "rest_calls": [
            ("PUT", "/api/symphonies/{name}"),
            ("POST", "/api/symphonies"),
            ("POST", "/api/symphonies/{name}/env-bootstrap"),
            ("POST", "/api/symphonies/{name}/wiki-init"),
        ],
    },
    "delete_symphony": {
        "domain": "symphonies",
        "annotations": _ann(False, True, False),
        "rest_calls": [("DELETE", "/api/symphonies/{name}")],
    },
    "validate_symphony": {
        "domain": "symphonies",
        "annotations": _ann(True, False, True),
        "rest_calls": [("POST", "/api/symphonies/{name}/validate")],
    },
    # personas
    "get_personas": {
        "domain": "personas",
        "annotations": _ann(True, False, True),
        "rest_calls": [("GET", "/api/personas")],
    },
    "upsert_persona": {
        "domain": "personas",
        "annotations": _ann(False, True, True),
        "rest_calls": [("PUT", "/api/personas/{role}")],
    },
    "delete_persona": {
        "domain": "personas",
        "annotations": _ann(False, True, False),
        "rest_calls": [("DELETE", "/api/personas/{role}")],
    },
    # lifecycle
    "control": {
        "domain": "lifecycle",
        "annotations": _ann(False, True, False),
        "rest_calls": [
            ("POST", "/api/force-poll"),
            ("POST", "/api/cancel"),
            ("POST", "/api/skip-role"),
            ("POST", "/api/restart-from/{role}"),
            ("POST", "/api/veto"),
            ("POST", "/api/dry-run/{card_id}"),
        ],
    },
    # state
    "query_events": {
        "domain": "state",
        "annotations": _ann(True, False, True),
        "rest_calls": [("GET", "/api/events")],
    },
    "get_state": {
        "domain": "state",
        "annotations": _ann(True, False, True),
        "rest_calls": [("GET", "/api/state")],
    },
    "get_performer_logs": {
        "domain": "state",
        "annotations": _ann(True, False, True),
        "rest_calls": [("GET", "/api/performer-logs")],
    },
    "get_activity": {
        "domain": "state",
        "annotations": _ann(True, False, True),
        "rest_calls": [("GET", "/api/activity")],
    },
    "get_metrics": {
        "domain": "state",
        "annotations": _ann(True, False, True),
        "rest_calls": [("GET", "{health_url}/metrics")],
    },
}


def test_registry_matches_contract() -> None:
    assert len(REGISTRY) == len(EXPECTED_TOOLS), (
        f"expected {len(EXPECTED_TOOLS)} tools, found {len(REGISTRY)}: "
        f"{sorted(t.name for t in REGISTRY)}"
    )
    for tool in REGISTRY:
        expected = EXPECTED_TOOLS.get(tool.name)
        assert expected is not None, f"unexpected tool registered: {tool.name}"
        assert tool.domain == expected["domain"], tool.name
        assert tool.annotations == expected["annotations"], tool.name
        assert sorted(tool.rest_calls) == sorted(expected["rest_calls"]), tool.name
    for name in EXPECTED_TOOLS:
        assert find_tool(name) is not None, f"missing tool: {name}"


def _dashboard_routes() -> list[tuple[str, str]]:
    app = create_dashboard_app(
        MagicMock(spec=DashboardStore), MagicMock(), MagicMock(), MagicMock(),
    )
    routes = []
    for route in app.routes:
        if isinstance(route, APIRoute):
            for method in route.methods:
                if method in ("GET", "POST", "PUT", "DELETE"):
                    routes.append((method, route.path))
    return routes


def test_dashboard_routes_covered_or_deferred() -> None:
    covered: set[tuple[str, str]] = set()
    for tool in REGISTRY:
        for rest_call in tool.rest_calls:
            if rest_call[1].startswith("{"):
                continue  # health-server call, not a dashboard route
            covered.add(rest_call)
    uncovered = [
        f"{method} {path}"
        for method, path in _dashboard_routes()
        if (method, path) not in covered
        and not _is_deferred(path)
    ]
    assert uncovered == [], f"dashboard routes not covered by tools: {uncovered}"


def _is_deferred(path: str) -> bool:
    return any(path.startswith(prefix) for prefix in DEFERRED_REST_PREFIXES) or (
        path in DEFERRED_PAGE_PATHS
    )


@pytest.mark.parametrize("deferred_path", ["/oidc/login", "/static/app.js", "/events", "/api/assistant/status", "/api/account", "/login"])
def test_defer_list_asserted(deferred_path: str) -> None:
    from coordinare.mcp.tools import is_deferred

    assert is_deferred(deferred_path)
