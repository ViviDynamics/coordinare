"""Live web dashboard for the coordinare daemon (010).

Serves a single-page dashboard at / and an SSE stream at /events.
No new dependencies — uses FastAPI/Starlette StreamingResponse (already present).

Decomposed into a package by 436: ``app.py`` holds the FastAPI factory,
``store.py`` the state store, ``routers/`` the per-area route modules,
``templates/`` the UI assets, and ``assets.py`` loads them.
"""
from __future__ import annotations

import asyncio

from coordinare.dashboard.app import check_port_available, create_dashboard_app
from coordinare.dashboard.assets import (
    _ACTIVITY_STREAM_JS,
    _ASSISTANT_FRAGMENT,
    _DASHBOARD_HTML,
    _DASHBOARD_JS_FILES,
    _DASHBOARD_JS_REVISIONS,
    _DASHBOARD_JS_SOURCES,
    DASHBOARD_HTML_BUDGET_BYTES,
    _page_html,
)
from coordinare.dashboard.guards import (
    _config_assistant_enabled,
    _entity_tags,
    _version_headers,
    _version_refusal,
)
from coordinare.dashboard.helpers import (
    _json_default,
    compute_overall_health,
    format_phase_label,
    is_session_stale,
    ownership_hint,
    render_performer_pool_widget,
)
from coordinare.dashboard.sse import SSEBroadcaster
from coordinare.dashboard.store import SESSION_EVENT_LIMIT, DashboardStore
from coordinare.dashboard.webhook import register_webhook_route, verify_github_signature

__all__ = [
    "DASHBOARD_HTML_BUDGET_BYTES",
    "SESSION_EVENT_LIMIT",
    "_ACTIVITY_STREAM_JS",
    "_ASSISTANT_FRAGMENT",
    "_DASHBOARD_HTML",
    "_DASHBOARD_JS_FILES",
    "_DASHBOARD_JS_REVISIONS",
    "_DASHBOARD_JS_SOURCES",
    "DashboardStore",
    "SSEBroadcaster",
    "_config_assistant_enabled",
    "_entity_tags",
    "_json_default",
    "_page_html",
    "_version_headers",
    "_version_refusal",
    "asyncio",
    "check_port_available",
    "compute_overall_health",
    "create_dashboard_app",
    "format_phase_label",
    "is_session_stale",
    "ownership_hint",
    "register_webhook_route",
    "render_performer_pool_widget",
    "verify_github_signature",
]
