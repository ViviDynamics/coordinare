"""E2E browser tests: verify _DASHBOARD_HTML renders in Chrome and Firefox.

Run with:
    pytest tests/e2e/ --override-ini="addopts=" --browser chromium --browser firefox

Skipped in the standard pytest run (requires Playwright browser binaries).
Each test is marked @pytest.mark.e2e; the default addopts excludes that marker.

049/053 additions: navbar, multi-page routing (pushState), active-performer tiles,
/performers drilldown, /personas page, /history live rendering.
"""
from __future__ import annotations

import re
from types import SimpleNamespace
from typing import Any

import pytest
from playwright.sync_api import Page, expect

from coordinare.dashboard import DashboardStore, ownership_hint

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_WAIT_SSE = 3_000   # ms — time for first SSE event to arrive and render
_WAIT_LIVE = 5_000  # ms — time for a broadcast to propagate and re-render
_WAIT_NAV = 1_000   # ms — time for client-side navigation to complete


def _full_snapshot(**overrides: Any) -> dict:
    """Return a complete dashboard snapshot dict, optionally with field overrides."""
    base: dict = {
        "phase": "idle",
        "phase_label": "Idle",
        "active_card_title": None,
        "active_card_column": None,
        "pr_url": None,
        "agent_session_id": None,
        "agent_dispatch_at": None,
        "open_questions": [],
        "card_clarifications": [],
        "performer_events": [],
        "performer_metrics": None,
        "performer_stage": "",
        "lifecycle_sequence": [],
        "performer_backend": "",
        "performer_logs": [],
        "card_tokens_total": 0,
        "card_cost_estimate": 0.0,
        "active_session_count": 0,
        # 049: active_sessions is a list of summaries (not a dict)
        "active_sessions": [],
        "subsystems": [{"name": "github", "status": "healthy", "required": True, "checked_at": "2026-03-02T10:00:00+00:00"}],
        "project_name": "test-project",
        "project_board_url": "https://github.com/orgs/test/projects/1",
        "cycles_completed": 0,
        "last_cycle_duration_seconds": None,
        "consecutive_error_count": 0,
        "daemon_start_time": "2026-03-02T09:30:00+00:00",
        "cycle_history": [],
        "cycle_active": False,
        "daemon_running": True,
        # 046
        "blocked_by_dependencies": [],
        # 047
        "last_rebase_round": None,
        # 048
        "role_utilization": [],
        # 050
        "assignee_filter": None,
        # 160
        "include_unassigned": False,
        # 053
        "board_summary": {
            "TODO": 0,
            "IN_PROGRESS": 0,
            "IN_REVIEW": 0,
            "DONE": 0,
            "BLOCKED": 0,
            "BACKLOG": 0,
        },
        "last_poll_at": None,
        "backend_ui_url": None,
        "session_stats": None,
    }
    base.update(overrides)
    # 160: the browser renders the policy, it no longer derives it. Deriving the
    # hint here with the production function keeps this fixture from drifting
    # into a snapshot the server would never send -- which is exactly what an
    # explicit "ownership_hint": None in the base dict would have allowed.
    base.setdefault(
        "ownership_hint",
        ownership_hint(
            SimpleNamespace(
                assignee_filter=base["assignee_filter"],
                include_unassigned=base["include_unassigned"],
            )
        ),
    )
    return base


def _active_session(
    card_id: str = "PVTI_1",
    card_title: str = "Fix the bug",
    stage: str = "implementing",
    phase: str = "monitoring_performer",
    **telemetry: Any,
) -> dict:
    """Return a single active_sessions list entry.

    348: performer telemetry rides on the session, not on top-level state.
    build_snapshot always emits these keys per session, so the fixture has to
    as well -- a top-level-only shape is one the real payload never has.
    """
    session = {
        "card_id": card_id,
        "card_title": card_title,
        "phase": phase,
        "performer_stage": stage,
        "card_tokens_total": 0,
        "card_cost_estimate": 0.0,
        "performer_events": [],
        "performer_metrics": None,
        "session_stats": None,
        "performer_logs": [],
        "backend_ui_url": None,
        "performer_backend": None,
        "session_id": None,
    }
    session.update(telemetry)
    return session


# ---------------------------------------------------------------------------
# Page load and static structure
# ---------------------------------------------------------------------------


@pytest.mark.e2e
def test_page_loads_with_navbar(page: Page, live_server_url: str) -> None:
    """Dashboard page must load and show the navbar with 4 links."""
    page.goto(live_server_url)
    expect(page.locator("#navbar")).to_be_visible(timeout=_WAIT_SSE)
    for text in ("Dashboard", "Performers", "Personas", "History"):
        expect(page.locator(f"#navbar a:has-text('{text}')")).to_have_count(1)


@pytest.mark.e2e
def test_page_title_on_home(page: Page, live_server_url: str) -> None:
    """Title at / must contain 'Dashboard' (set by router() on DOMContentLoaded)."""
    page.goto(live_server_url)
    # Wait for router to fire and update title
    expect(page).to_have_title("Dashboard — Coordinare", timeout=_WAIT_SSE)


@pytest.mark.e2e
def test_all_section_ids_present(page: Page, live_server_url: str) -> None:
    """All required DOM element IDs must be present in the served HTML."""
    page.goto(live_server_url)
    for element_id in (
        "phase",
        "active-work-card",
        "questions-card",
        "cycles-completed",
        "last-duration",
        "error-count",
        "daemon-start-time",
        "subsystems-section",
        "disconnected-banner",
        "force-poll-btn",
        "force-poll-msg",
        # 049 new IDs
        "navbar",
        "active-performers",
        "active-performer-tiles",
        "dashboard-page",
        "performers-page",
        "personas-page",
        "history-page",
    ):
        expect(page.locator(f"#{element_id}")).to_have_count(1)


# ---------------------------------------------------------------------------
# Initial SSE render (Scenario 1 — idle)
# ---------------------------------------------------------------------------


@pytest.mark.e2e
def test_initial_phase_label_is_idle(page: Page, live_server_url: str) -> None:
    """First SSE event must render 'Idle' in the phase element."""
    page.goto(live_server_url)
    expect(page.locator("#phase")).to_have_text("Idle", timeout=_WAIT_SSE)


@pytest.mark.e2e
def test_idle_shows_no_active_card(page: Page, live_server_url: str) -> None:
    """Empty-state message must appear in the card section when no card is active."""
    page.goto(live_server_url)
    expect(page.locator("#phase")).to_have_text("Idle", timeout=_WAIT_SSE)
    expect(page.locator("#swimlane-section")).to_contain_text("No cards in the TODO column")


@pytest.mark.e2e
def test_disconnected_banner_hidden_on_load(page: Page, live_server_url: str) -> None:
    """Disconnected banner must be hidden while the SSE connection is healthy."""
    page.goto(live_server_url)
    expect(page.locator("#phase")).to_have_text("Idle", timeout=_WAIT_SSE)
    expect(page.locator("#disconnected-banner")).to_be_hidden()


@pytest.mark.e2e
def test_empty_history_shows_placeholder(page: Page, live_server_url: str) -> None:
    """Before any cycles run, the history section shows the empty-state message."""
    page.goto(live_server_url)
    expect(page.locator("#phase")).to_have_text("Idle", timeout=_WAIT_SSE)
    expect(page.locator("#history-section")).to_contain_text("No cycles completed yet")


@pytest.mark.e2e
def test_subsystem_health_table_renders(page: Page, live_server_url: str) -> None:
    """The subsystem health table must show the registered 'github' probe."""
    page.goto(live_server_url)
    expect(page.locator("#phase")).to_have_text("Idle", timeout=_WAIT_SSE)
    expect(page.locator("#subsystems-section")).to_contain_text("github")


# ---------------------------------------------------------------------------
# Live updates via SSE broadcast (Scenario 2, 3, 5, 7)
# ---------------------------------------------------------------------------


@pytest.mark.e2e
def test_phase_updates_on_broadcast(
    page: Page, live_server_url: str, store: DashboardStore
) -> None:
    """Scenario 2/7: A broadcast must update the phase label within 5 s."""
    page.goto(live_server_url)
    expect(page.locator("#phase")).to_have_text("Idle", timeout=_WAIT_SSE)

    store.broadcaster.broadcast(_full_snapshot(phase="dispatching", phase_label="Dispatching"))

    expect(page.locator("#phase")).to_have_text("Dispatching", timeout=_WAIT_LIVE)


@pytest.mark.e2e
def test_active_card_details_render(
    page: Page, live_server_url: str, store: DashboardStore
) -> None:
    """Scenario 2: Card title and issue link must appear in active-work panel after broadcast."""
    page.goto(live_server_url)
    expect(page.locator("#phase")).to_have_text("Idle", timeout=_WAIT_SSE)

    session = {**_active_session("PVTI_1", "Fix timeout bug", "implementing"), "issue_url": "https://github.com/org/repo/issues/42"}
    store.broadcaster.broadcast(
        _full_snapshot(
            phase="monitoring_agent",
            phase_label="Monitoring Agent",
            active_sessions=[session],
        )
    )

    expect(page.locator("#swimlane-section")).to_contain_text("Fix timeout bug", timeout=_WAIT_LIVE)
    expect(page.locator("#swimlane-section a")).to_have_attribute(
        "href", "https://github.com/org/repo/issues/42", timeout=_WAIT_LIVE
    )


@pytest.mark.e2e
def test_open_questions_visible_when_blocked(
    page: Page, live_server_url: str, store: DashboardStore
) -> None:
    """Scenario 3: Questions card must become visible and list questions when blocked."""
    page.goto(live_server_url)
    expect(page.locator("#phase")).to_have_text("Idle", timeout=_WAIT_SSE)

    store.broadcaster.broadcast(
        _full_snapshot(
            phase="blocked",
            phase_label="Blocked",
            active_card_title="Refactor auth",
            active_card_column="Blocked",
            open_questions=["Should we use OAuth2 or API keys?"],
        )
    )

    expect(page.locator("#questions-card")).to_be_visible(timeout=_WAIT_LIVE)
    expect(page.locator("#questions-list")).to_contain_text("OAuth2", timeout=_WAIT_LIVE)


@pytest.mark.e2e
def test_open_questions_hidden_when_not_blocked(
    page: Page, live_server_url: str, store: DashboardStore
) -> None:
    """Questions card must be hidden when there are no open questions."""
    page.goto(live_server_url)
    expect(page.locator("#phase")).to_have_text("Idle", timeout=_WAIT_SSE)

    store.broadcaster.broadcast(_full_snapshot(phase="idle", phase_label="Idle", open_questions=[]))

    expect(page.locator("#phase")).to_have_text("Idle", timeout=_WAIT_LIVE)
    expect(page.locator("#questions-card")).to_be_hidden()


@pytest.mark.e2e
def test_cycle_history_table_renders(
    page: Page, live_server_url: str, store: DashboardStore
) -> None:
    """Scenario 5: History table must show success and error entries after broadcast."""
    page.goto(live_server_url)
    expect(page.locator("#phase")).to_have_text("Idle", timeout=_WAIT_SSE)

    store.broadcaster.broadcast(
        _full_snapshot(
            cycles_completed=3,
            last_cycle_duration_seconds=0.45,
            cycle_history=[
                {"timestamp": "2026-03-02T10:05:00+00:00", "phase": "idle", "duration_seconds": 0.45, "outcome": "success"},
                {"timestamp": "2026-03-02T10:04:30+00:00", "phase": "idle", "duration_seconds": 0.0, "outcome": "error"},
            ],
        )
    )

    expect(page.locator("#history-section table")).to_be_visible(timeout=_WAIT_LIVE)
    expect(page.locator("#history-section")).to_contain_text("success", timeout=_WAIT_LIVE)
    expect(page.locator("#history-section")).to_contain_text("error", timeout=_WAIT_LIVE)


@pytest.mark.e2e
def test_metrics_bar_updates(
    page: Page, live_server_url: str, store: DashboardStore
) -> None:
    """Metrics bar values must update when a broadcast arrives."""
    page.goto(live_server_url)
    expect(page.locator("#phase")).to_have_text("Idle", timeout=_WAIT_SSE)

    store.broadcaster.broadcast(
        _full_snapshot(cycles_completed=42, last_cycle_duration_seconds=1.23, consecutive_error_count=2)
    )

    expect(page.locator("#cycles-completed")).to_have_text("42", timeout=_WAIT_LIVE)
    expect(page.locator("#error-count")).to_have_text("2", timeout=_WAIT_LIVE)


# ---------------------------------------------------------------------------
# Force-poll button
# ---------------------------------------------------------------------------


@pytest.mark.e2e
def test_force_poll_button_present(page: Page, live_server_url: str) -> None:
    """Button with id force-poll-btn must be visible in the rendered page."""
    page.goto(live_server_url)
    expect(page.locator("#phase")).to_have_text("Idle", timeout=_WAIT_SSE)
    expect(page.locator("#force-poll-btn")).to_be_visible()


@pytest.mark.e2e
def test_force_poll_button_enabled_when_idle(
    page: Page, live_server_url: str, store: DashboardStore
) -> None:
    """Button must be enabled after SSE delivers cycle_active=False, daemon_running=True."""
    page.goto(live_server_url)
    expect(page.locator("#phase")).to_have_text("Idle", timeout=_WAIT_SSE)
    store.broadcaster.broadcast(_full_snapshot(cycle_active=False, daemon_running=True))
    expect(page.locator("#force-poll-btn")).to_be_enabled(timeout=_WAIT_LIVE)


@pytest.mark.e2e
def test_force_poll_button_disabled_when_cycle_active(
    page: Page, live_server_url: str, store: DashboardStore
) -> None:
    """Button must be disabled after SSE delivers cycle_active=True."""
    page.goto(live_server_url)
    expect(page.locator("#phase")).to_have_text("Idle", timeout=_WAIT_SSE)
    store.broadcaster.broadcast(_full_snapshot(cycle_active=True, daemon_running=True))
    expect(page.locator("#force-poll-btn")).to_be_disabled(timeout=_WAIT_LIVE)


@pytest.mark.e2e
def test_force_poll_button_disabled_when_daemon_stopped(
    page: Page, live_server_url: str, store: DashboardStore
) -> None:
    """Button must be disabled when daemon_running=False even if cycle is not active."""
    page.goto(live_server_url)
    expect(page.locator("#phase")).to_have_text("Idle", timeout=_WAIT_SSE)
    store.broadcaster.broadcast(_full_snapshot(cycle_active=False, daemon_running=False))
    expect(page.locator("#force-poll-btn")).to_be_disabled(timeout=_WAIT_LIVE)


@pytest.mark.e2e
def test_force_poll_button_re_enables_after_cycle(
    page: Page, live_server_url: str, store: DashboardStore
) -> None:
    """Button must re-enable once cycle_active returns to False."""
    page.goto(live_server_url)
    expect(page.locator("#phase")).to_have_text("Idle", timeout=_WAIT_SSE)
    store.broadcaster.broadcast(_full_snapshot(cycle_active=True, daemon_running=True))
    expect(page.locator("#force-poll-btn")).to_be_disabled(timeout=_WAIT_LIVE)
    store.broadcaster.broadcast(_full_snapshot(cycle_active=False, daemon_running=True))
    expect(page.locator("#force-poll-btn")).to_be_enabled(timeout=_WAIT_LIVE)


@pytest.mark.e2e
def test_force_poll_click_fires_post_request(page: Page, live_server_url: str) -> None:
    """Clicking the button must fire a POST to /api/force-poll."""
    page.goto(live_server_url)
    expect(page.locator("#phase")).to_have_text("Idle", timeout=_WAIT_SSE)
    expect(page.locator("#force-poll-btn")).to_be_enabled(timeout=_WAIT_LIVE)
    with page.expect_request("**/api/force-poll") as req_info:
        page.locator("#force-poll-btn").click()
    assert req_info.value.method == "POST"


@pytest.mark.e2e
def test_force_poll_409_shows_cycle_already_running(page: Page, live_server_url: str) -> None:
    """A 409 from /api/force-poll must show 'Cycle already running' in the message span."""
    page.route(
        "**/api/force-poll",
        lambda route: route.fulfill(status=409, content_type="application/json", body='{"status":"cycle_in_progress"}'),
    )
    page.goto(live_server_url)
    expect(page.locator("#phase")).to_have_text("Idle", timeout=_WAIT_SSE)
    page.locator("#force-poll-btn").click()
    expect(page.locator("#force-poll-msg")).to_have_text("Cycle already running", timeout=2000)
    expect(page.locator("#force-poll-btn")).to_be_enabled(timeout=2000)


@pytest.mark.e2e
def test_force_poll_button_disabled_when_sse_disconnected(page: Page, live_server_url: str) -> None:
    """Button must be disabled when the SSE connection fails (onerror fires)."""
    page.route("**/events", lambda route: route.abort())
    page.goto(live_server_url)
    expect(page.locator("#force-poll-btn")).to_be_disabled(timeout=3000)
    expect(page.locator("#disconnected-banner")).to_be_visible(timeout=3000)


# ---------------------------------------------------------------------------
# 049: Navbar and multi-page routing
# ---------------------------------------------------------------------------


@pytest.mark.e2e
def test_navbar_dashboard_link_is_active_on_home(page: Page, live_server_url: str) -> None:
    """Dashboard nav link must have the active class on /."""
    page.goto(live_server_url)
    expect(page.locator("#phase")).to_have_text("Idle", timeout=_WAIT_SSE)
    expect(page.locator("#navbar a[href='/']")).to_have_class("nav-link nav-active")


@pytest.mark.e2e
def test_navigate_to_performers_updates_url_and_title(page: Page, live_server_url: str) -> None:
    """Clicking Performers nav link must update URL to /performers and title."""
    page.goto(live_server_url)
    expect(page.locator("#phase")).to_have_text("Idle", timeout=_WAIT_SSE)

    page.locator("#navbar a[href='/performers']").click()

    expect(page).to_have_url(f"{live_server_url}/performers", timeout=_WAIT_NAV)
    expect(page).to_have_title("Performers — Coordinare", timeout=_WAIT_NAV)
    expect(page.locator("#navbar a[href='/performers']")).to_have_class("nav-link nav-active")


@pytest.mark.e2e
def test_navigate_to_personas_shows_personas_page(page: Page, live_server_url: str) -> None:
    """Clicking Personas nav link must show /personas page content."""
    page.goto(live_server_url)
    expect(page.locator("#phase")).to_have_text("Idle", timeout=_WAIT_SSE)

    page.locator("#navbar a[href='/personas']").click()

    expect(page).to_have_url(f"{live_server_url}/personas", timeout=_WAIT_NAV)
    expect(page).to_have_title("Personas — Coordinare", timeout=_WAIT_NAV)
    expect(page.locator("#personas-page")).to_be_visible(timeout=_WAIT_NAV)
    expect(page.locator("#dashboard-page")).to_be_hidden()


@pytest.mark.e2e
def test_navigate_to_history_shows_live_empty_state(page: Page, live_server_url: str) -> None:
    """Clicking History nav link must show the real history container empty-state."""
    page.goto(live_server_url)
    expect(page.locator("#phase")).to_have_text("Idle", timeout=_WAIT_SSE)

    page.locator("#navbar a[href='/history']").click()

    expect(page).to_have_url(f"{live_server_url}/history", timeout=_WAIT_NAV)
    expect(page.locator("#history-page")).to_be_visible(timeout=_WAIT_NAV)
    expect(page.locator("#history-page")).to_contain_text("No cycles completed yet", timeout=_WAIT_NAV)


@pytest.mark.e2e
def test_history_page_renders_cycle_rows_from_sse(
    page: Page, live_server_url: str, store: DashboardStore
) -> None:
    """053: /history must render cycle rows and live-update from SSE."""
    page.goto(f"{live_server_url}/history")
    expect(page.locator("#history-page")).to_be_visible(timeout=_WAIT_NAV)
    expect(page.locator("#history-page-section")).to_contain_text("No cycles completed yet", timeout=_WAIT_NAV)

    store.broadcaster.broadcast(
        _full_snapshot(
            cycle_history=[
                {"timestamp": "2026-03-02T10:05:00+00:00", "phase": "monitoring_pr", "duration_seconds": 0.45, "outcome": "success"},
                {"timestamp": "2026-03-02T10:04:30+00:00", "phase": "recovery", "duration_seconds": 0.0, "outcome": "error"},
            ]
        )
    )

    expect(page.locator("#history-page-section table")).to_be_visible(timeout=_WAIT_LIVE)
    expect(page.locator("#history-page-section")).to_contain_text("Monitoring Pr", timeout=_WAIT_LIVE)
    expect(page.locator("#history-page-section")).to_contain_text("error", timeout=_WAIT_LIVE)


@pytest.mark.e2e
def test_browser_back_returns_to_home(page: Page, live_server_url: str) -> None:
    """Browser back button must return to / after navigating to /performers."""
    page.goto(live_server_url)
    expect(page.locator("#phase")).to_have_text("Idle", timeout=_WAIT_SSE)

    page.locator("#navbar a[href='/performers']").click()
    expect(page).to_have_url(f"{live_server_url}/performers", timeout=_WAIT_NAV)

    page.go_back()
    expect(page).to_have_url(live_server_url + "/", timeout=_WAIT_NAV)
    expect(page.locator("#dashboard-page")).to_be_visible(timeout=_WAIT_NAV)


@pytest.mark.e2e
def test_direct_navigate_to_performers_route(page: Page, live_server_url: str) -> None:
    """Navigating directly to /performers must render the performers page."""
    page.goto(f"{live_server_url}/performers")
    expect(page.locator("#navbar")).to_be_visible(timeout=_WAIT_SSE)
    expect(page.locator("#performers-page")).to_be_visible(timeout=_WAIT_NAV)
    expect(page.locator("#dashboard-page")).to_be_hidden()
    expect(page.locator("#navbar a[href='/performers']")).to_have_class("nav-link nav-active")


@pytest.mark.e2e
def test_direct_navigate_to_personas_route(page: Page, live_server_url: str) -> None:
    """Navigating directly to /personas must render the personas page."""
    page.goto(f"{live_server_url}/personas")
    expect(page.locator("#navbar")).to_be_visible(timeout=_WAIT_SSE)
    expect(page.locator("#personas-page")).to_be_visible(timeout=_WAIT_NAV)
    expect(page.locator("#dashboard-page")).to_be_hidden()


@pytest.mark.e2e
def test_sse_stays_connected_across_navigation(
    page: Page, live_server_url: str, store: DashboardStore
) -> None:
    """SSE EventSource must stay connected when navigating between pages."""
    page.goto(live_server_url)
    expect(page.locator("#phase")).to_have_text("Idle", timeout=_WAIT_SSE)

    # Navigate to performers
    page.locator("#navbar a[href='/performers']").click()
    expect(page).to_have_url(f"{live_server_url}/performers", timeout=_WAIT_NAV)

    # Navigate back to dashboard
    page.locator("#navbar a[href='/']").click()
    expect(page).to_have_url(live_server_url + "/", timeout=_WAIT_NAV)

    # SSE still works — broadcast should update phase
    store.broadcaster.broadcast(_full_snapshot(phase="dispatching", phase_label="Dispatching"))
    expect(page.locator("#phase")).to_have_text("Dispatching", timeout=_WAIT_LIVE)


# ---------------------------------------------------------------------------
# 049: Active-performer tiles
# ---------------------------------------------------------------------------


@pytest.mark.e2e
def test_active_performer_tiles_show_on_home(
    page: Page, live_server_url: str, store: DashboardStore
) -> None:
    """Active performer tiles must appear on the main dashboard when sessions are active."""
    page.goto(live_server_url)
    expect(page.locator("#phase")).to_have_text("Idle", timeout=_WAIT_SSE)

    store.broadcaster.broadcast(
        _full_snapshot(
            phase="monitoring_performer",
            phase_label="Monitoring Performer",
            agent_dispatch_at="2026-04-20T10:00:00+00:00",
            active_sessions=[
                _active_session("PVTI_1", "Fix the auth bug", "implementing"),
            ],
        )
    )

    expect(page.locator("#active-performers")).to_be_visible(timeout=_WAIT_LIVE)
    expect(page.locator("#active-performer-tiles")).to_contain_text("Implementing", timeout=_WAIT_LIVE)
    expect(page.locator("#active-performer-tiles")).to_contain_text("Fix the auth bug", timeout=_WAIT_LIVE)


@pytest.mark.e2e
def test_idle_state_shows_no_active_performers_message(
    page: Page, live_server_url: str, store: DashboardStore
) -> None:
    """When no sessions are active, the active-performers section shows an idle message."""
    page.goto(live_server_url)
    expect(page.locator("#phase")).to_have_text("Idle", timeout=_WAIT_SSE)

    store.broadcaster.broadcast(
        _full_snapshot(
            active_sessions=[],
            cycles_completed=5,
            board_summary={"TODO": 3, "IN_PROGRESS": 1, "IN_REVIEW": 2, "DONE": 4, "BLOCKED": 0, "BACKLOG": 0},
            last_poll_at="2026-04-22T22:40:00+00:00",
        )
    )

    expect(page.locator("#active-performer-tiles")).to_contain_text(
        "No active performers", timeout=_WAIT_LIVE
    )
    expect(page.locator("#active-performer-tiles")).to_contain_text("TODO 3", timeout=_WAIT_LIVE)
    expect(page.locator("#active-performer-tiles")).to_contain_text("IN_PROGRESS 1", timeout=_WAIT_LIVE)
    expect(page.locator("#active-performer-tiles")).to_contain_text("Last poll:", timeout=_WAIT_LIVE)


@pytest.mark.e2e
def test_idle_state_shows_assignee_filter_hint(
    page: Page, live_server_url: str, store: DashboardStore
) -> None:
    """050: when assignee_filter is set, idle tile shows 'Filter: <login>'."""
    page.goto(live_server_url)
    expect(page.locator("#phase")).to_have_text("Idle", timeout=_WAIT_SSE)

    store.broadcaster.broadcast(
        _full_snapshot(active_sessions=[], assignee_filter="coordinare-bot")
    )

    expect(page.locator("#active-performer-tiles")).to_contain_text(
        "Filter: coordinare-bot", timeout=_WAIT_LIVE
    )


@pytest.mark.e2e
def test_idle_state_no_filter_hint_when_unset(
    page: Page, live_server_url: str, store: DashboardStore
) -> None:
    """050: when assignee_filter is None, idle tile does not show a filter hint."""
    page.goto(live_server_url)
    expect(page.locator("#phase")).to_have_text("Idle", timeout=_WAIT_SSE)

    store.broadcaster.broadcast(
        _full_snapshot(active_sessions=[], assignee_filter=None)
    )

    tiles = page.locator("#active-performer-tiles")
    expect(tiles).to_contain_text("No active performers", timeout=_WAIT_LIVE)
    expect(tiles).not_to_contain_text("Filter:", timeout=_WAIT_LIVE)


@pytest.mark.e2e
def test_active_work_row_click_opens_detail_and_back_returns_list(
    page: Page, live_server_url: str, store: DashboardStore
) -> None:
    """059: clicking an Active Work table row opens the card detail view; back button restores the list."""
    page.goto(live_server_url)
    expect(page.locator("#phase")).to_have_text("Idle", timeout=_WAIT_SSE)

    store.broadcaster.broadcast(
        _full_snapshot(
            phase="monitoring_performer",
            phase_label="Monitoring Performer",
            agent_dispatch_at="2026-04-20T10:00:00+00:00",
            active_sessions=[_active_session("PVTI_1", "Fix the auth bug", "implementing")],
        )
    )

    row = page.locator("#swimlane-section .swimlane-card[data-card-id='PVTI_1']")
    expect(row).to_be_visible(timeout=_WAIT_LIVE)
    row.click()

    expect(page.locator("#card-detail-view")).to_be_visible(timeout=_WAIT_LIVE)
    expect(page.locator("#swimlane-section")).to_be_hidden(timeout=_WAIT_LIVE)
    expect(page.locator("#card-detail-content")).to_contain_text("Fix the auth bug", timeout=_WAIT_LIVE)

    page.locator("#card-detail-view .perf-back-btn").click()
    expect(page.locator("#swimlane-section")).to_be_visible(timeout=_WAIT_LIVE)
    expect(page.locator("#card-detail-view")).to_be_hidden(timeout=_WAIT_LIVE)


@pytest.mark.e2e
def test_active_work_row_keyboard_opens_detail(
    page: Page, live_server_url: str, store: DashboardStore
) -> None:
    """059: Enter key on a focused Active Work row must open the card detail view."""
    page.goto(live_server_url)
    expect(page.locator("#phase")).to_have_text("Idle", timeout=_WAIT_SSE)

    store.broadcaster.broadcast(
        _full_snapshot(
            phase="monitoring_performer",
            phase_label="Monitoring Performer",
            agent_dispatch_at="2026-04-20T10:00:00+00:00",
            active_sessions=[_active_session("PVTI_2", "Add keyboard nav", "implementing")],
        )
    )

    row = page.locator("#swimlane-section .swimlane-card[data-card-id='PVTI_2']")
    expect(row).to_be_visible(timeout=_WAIT_LIVE)
    row.focus()
    page.keyboard.press("Enter")

    expect(page.locator("#card-detail-view")).to_be_visible(timeout=_WAIT_LIVE)
    expect(page.locator("#card-detail-content")).to_contain_text("Add keyboard nav", timeout=_WAIT_LIVE)


@pytest.mark.e2e
def test_workflow_diagram_visible_without_expand_toggle(page: Page, live_server_url: str) -> None:
    """053: Workflow should be readable without expand/collapse controls."""
    page.goto(live_server_url)
    expect(page.locator("#phase")).to_have_text("Idle", timeout=_WAIT_SSE)
    fc = page.locator("#flow-chart")
    expect(fc).to_be_visible()
    expect(page.locator("#flow-chart-toggle")).to_have_count(0)


@pytest.mark.e2e
def test_desktop_layout_active_performers_is_single_column(
    page: Page, live_server_url: str, store: DashboardStore
) -> None:
    """053: Active Performers card should be a single-column grid item (not full-width)."""
    page.set_viewport_size({"width": 1200, "height": 900})
    page.goto(live_server_url)
    expect(page.locator("#phase")).to_have_text("Idle", timeout=_WAIT_SSE)

    store.broadcaster.broadcast(
        _full_snapshot(
            phase="monitoring_performer",
            phase_label="Monitoring Performer",
            active_sessions=[_active_session("PVTI_1", "Fix layout", "implementing")],
        )
    )

    expect(page.locator("#active-performers")).to_be_visible(timeout=_WAIT_LIVE)
    layout = page.evaluate(
        """() => {
          const apCard = document.getElementById('active-performers');
          const phaseCard = document.getElementById('phase').closest('.card');
          const ap = apCard.getBoundingClientRect();
          const ph = phaseCard.getBoundingClientRect();
          return {apWidth: ap.width, phWidth: ph.width, viewportWidth: window.innerWidth};
        }"""
    )
    assert layout["apWidth"] < layout["viewportWidth"] * 0.75
    assert abs(layout["apWidth"] - layout["phWidth"]) < 120


@pytest.mark.e2e
def test_desktop_layout_clarifications_and_recent_cycles_are_single_column(
    page: Page, live_server_url: str, store: DashboardStore
) -> None:
    """053: Clarification History and Recent Cycles should be single-column grid cards."""
    page.set_viewport_size({"width": 1200, "height": 900})
    page.goto(live_server_url)
    expect(page.locator("#phase")).to_have_text("Idle", timeout=_WAIT_SSE)

    store.broadcaster.broadcast(
        _full_snapshot(
            card_clarifications=[{"questions": ["Need API shape?"], "answer": "Use v2 payload."}],
            cycle_history=[
                {
                    "timestamp": "2026-04-22T22:40:00+00:00",
                    "phase": "monitoring_performer",
                    "duration_seconds": 12.3,
                    "outcome": "success",
                }
            ],
        )
    )

    expect(page.locator("#clarifications-card")).to_be_visible(timeout=_WAIT_LIVE)
    expect(page.locator("#history-section")).to_contain_text("success", timeout=_WAIT_LIVE)
    layout = page.evaluate(
        """() => {
          const clCard = document.getElementById('clarifications-card');
          const historyCard = document.getElementById('history-section').closest('.card');
          const metricsCard = document.getElementById('cycles-completed').closest('.card');
          const cl = clCard.getBoundingClientRect();
          const hs = historyCard.getBoundingClientRect();
          const mt = metricsCard.getBoundingClientRect();
          return {
            clWidth: cl.width,
            hsWidth: hs.width,
            mtWidth: mt.width,
            viewportWidth: window.innerWidth,
          };
        }"""
    )
    assert layout["clWidth"] < layout["viewportWidth"] * 0.75
    assert layout["hsWidth"] < layout["viewportWidth"] * 0.75
    assert abs(layout["clWidth"] - layout["mtWidth"]) < 120
    assert abs(layout["hsWidth"] - layout["mtWidth"]) < 120


# ---------------------------------------------------------------------------
# 049: /performers page
# ---------------------------------------------------------------------------


@pytest.mark.e2e
def test_performers_page_shows_role_table(
    page: Page, live_server_url: str, store: DashboardStore
) -> None:
    """The /performers page must show a table with role utilization data."""
    page.goto(live_server_url)
    expect(page.locator("#phase")).to_have_text("Idle", timeout=_WAIT_SSE)

    store.broadcaster.broadcast(
        _full_snapshot(
            role_utilization=[
                {"role": "implementing", "active": 1, "max": 2, "queued": 0},
                {"role": "reviewing", "active": 0, "max": 1, "queued": 0},
            ],
            active_sessions=[_active_session("PVTI_1", "Fix bug", "implementing")],
        )
    )

    page.locator("#navbar a[href='/performers']").click()
    expect(page.locator("#performers-page")).to_be_visible(timeout=_WAIT_NAV)
    expect(page.locator("#performers-page")).to_contain_text("implementing", timeout=_WAIT_LIVE)
    expect(page.locator("#performers-page")).to_contain_text("reviewing", timeout=_WAIT_LIVE)


@pytest.mark.e2e
def test_performers_page_shows_active_badge(
    page: Page, live_server_url: str, store: DashboardStore
) -> None:
    """An active role must show the 'active' badge on the performers page."""
    page.goto(live_server_url)
    expect(page.locator("#phase")).to_have_text("Idle", timeout=_WAIT_SSE)

    store.broadcaster.broadcast(
        _full_snapshot(
            role_utilization=[{"role": "implementing", "active": 1, "max": 1, "queued": 0}],
            active_sessions=[_active_session("PVTI_1", "Big feature", "implementing")],
        )
    )

    page.locator("#navbar a[href='/performers']").click()
    expect(page.locator("#performers-page .role-active")).to_be_visible(timeout=_WAIT_LIVE)


@pytest.mark.e2e
def test_performers_page_row_click_opens_detail_and_back_restores_list(
    page: Page, live_server_url: str, store: DashboardStore
) -> None:
    """053: clicking a performers row opens detail view; back returns to list."""
    page.goto(f"{live_server_url}/performers")
    expect(page.locator("#performers-page")).to_be_visible(timeout=_WAIT_NAV)

    store.broadcaster.broadcast(
        _full_snapshot(
            role_utilization=[
                {"role": "implementing", "active": 1, "max": 1, "queued": 0},
                {"role": "reviewing", "active": 0, "max": 1, "queued": 1},
            ],
            active_sessions=[_active_session("PVTI_1", "Big feature", "implementing")],
            performer_events=[{"type": "progress", "text": "Running tests"}],
            performer_logs=["line one", "line two"],
            performer_metrics={"pid": 111, "cpu_percent": 2.5, "memory_bytes": 2048},
            performer_backend="opencode",
            backend_ui_url="http://127.0.0.1:4040",
            agent_session_id="sess-xyz",
            agent_dispatch_at="2026-04-22T22:40:00+00:00",
            session_stats={"title": "Big feature", "files_changed": 2, "lines_added": 20, "lines_removed": 5},
        )
    )

    row = page.locator("#performers-page-tbody tr[data-role='implementing']")
    expect(row).to_be_visible(timeout=_WAIT_LIVE)
    row.click()
    expect(page.locator("#performers-page-detail-view")).to_be_visible(timeout=_WAIT_LIVE)
    expect(page.locator("#performers-page-detail")).to_contain_text("Card Context", timeout=_WAIT_LIVE)
    expect(page.locator("#performers-page-detail")).to_contain_text("Live Events", timeout=_WAIT_LIVE)
    page.locator("#performers-page-back").click()
    expect(page.locator("#performers-page-list-view")).to_be_visible(timeout=_WAIT_LIVE)
    expect(page.locator("#performers-page-detail-view")).to_be_hidden(timeout=_WAIT_LIVE)


@pytest.mark.e2e
def test_performers_page_keyboard_drilldown_stays_open_during_sse_updates(
    page: Page, live_server_url: str, store: DashboardStore
) -> None:
    """053: Enter/Space should open detail, and SSE updates should not collapse it."""
    page.goto(f"{live_server_url}/performers")
    expect(page.locator("#performers-page")).to_be_visible(timeout=_WAIT_NAV)

    store.broadcaster.broadcast(
        _full_snapshot(
            role_utilization=[{"role": "implementing", "active": 1, "max": 1, "queued": 0}],
            active_sessions=[
                _active_session(
                    "PVTI_1", "Keyboard flow", "implementing",
                    performer_events=[{"type": "progress", "text": "Initial"}],
                )
            ],
        )
    )
    row = page.locator("#performers-page-tbody tr[data-role='implementing']")
    expect(row).to_be_visible(timeout=_WAIT_LIVE)
    row.focus()
    page.keyboard.press("Enter")
    expect(page.locator("#performers-page-detail-view")).to_be_visible(timeout=_WAIT_LIVE)

    store.broadcaster.broadcast(
        _full_snapshot(
            role_utilization=[{"role": "implementing", "active": 1, "max": 1, "queued": 0}],
            active_sessions=[
                _active_session(
                    "PVTI_1", "Keyboard flow", "implementing",
                    performer_events=[{"type": "progress", "text": "Updated"}],
                )
            ],
        )
    )
    expect(page.locator("#performers-page-detail-view")).to_be_visible(timeout=_WAIT_LIVE)
    expect(page.locator("#performers-page-detail")).to_contain_text("Updated", timeout=_WAIT_LIVE)


# ---------------------------------------------------------------------------
# 348: telemetry belongs to the session, not to whoever rendered last
# ---------------------------------------------------------------------------


@pytest.mark.e2e
def test_performer_detail_shows_only_the_selected_roles_telemetry(
    page: Page, live_server_url: str, store: DashboardStore
) -> None:
    """348: two live roles must not show each other's events.

    Before the fix the panel read one global blob, so whichever card synced to
    top-level state last was rendered under every role.
    """
    page.goto(f"{live_server_url}/performers")
    expect(page.locator("#performers-page")).to_be_visible(timeout=_WAIT_NAV)

    store.broadcaster.broadcast(
        _full_snapshot(
            role_utilization=[
                {"role": "implementing", "active": 1, "max": 1, "queued": 0},
                {"role": "architecting", "active": 1, "max": 1, "queued": 0},
            ],
            active_sessions=[
                _active_session(
                    "PVTI_1", "Timesheet rules", "implementing",
                    performer_events=[{"type": "tool_use", "text": "IMPLEMENTER-EVENT"}],
                ),
                _active_session(
                    "PVTI_2", "Blueprint draft", "architecting",
                    performer_events=[{"type": "text", "text": "ARCHITECT-EVENT"}],
                ),
            ],
        )
    )

    impl_row = page.locator("#performers-page-tbody tr[data-role='implementing']")
    expect(impl_row).to_be_visible(timeout=_WAIT_LIVE)
    impl_row.click()
    detail = page.locator("#performers-page-detail")
    expect(detail).to_contain_text("IMPLEMENTER-EVENT", timeout=_WAIT_LIVE)
    expect(detail).not_to_contain_text("ARCHITECT-EVENT")


# ---------------------------------------------------------------------------
# 049: Personas hidden from main dashboard
# ---------------------------------------------------------------------------


@pytest.mark.e2e
def test_personas_not_on_main_dashboard(page: Page, live_server_url: str) -> None:
    """The personas editor must NOT appear on the main dashboard page (moved to /personas)."""
    page.goto(live_server_url)
    expect(page.locator("#phase")).to_have_text("Idle", timeout=_WAIT_SSE)
    # #personas-main-card is hidden on the main dashboard
    expect(page.locator("#personas-main-card")).to_be_hidden()


@pytest.mark.e2e
def test_personas_page_loads_editor(page: Page, live_server_url: str) -> None:
    """The /personas page must show the persona editor (loading from /api/personas)."""
    page.route(
        "**/api/personas",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body='[{"role":"implementer","instructions":"Write tests first.","is_default":false}]',
        ),
    )
    page.goto(f"{live_server_url}/personas")
    expect(page.locator("#personas-page")).to_be_visible(timeout=_WAIT_NAV)
    expect(page.locator("#personas-page-section")).to_contain_text("implementer", timeout=2000)
    expect(page.locator("#personas-page-section textarea")).to_contain_text(
        "Write tests first.", timeout=2000
    )


# ---------------------------------------------------------------------------
# Symphonies page tests
# ---------------------------------------------------------------------------


@pytest.mark.e2e
def test_navigate_to_symphonies_page(page: Page, live_server_url: str) -> None:
    """Navigate to /symphonies via navbar link."""
    page.goto(live_server_url)
    expect(page.locator("#navbar")).to_be_visible(timeout=_WAIT_SSE)
    page.click("a[href='/symphonies']")
    expect(page.locator("#symphonies-page")).to_be_visible(timeout=_WAIT_NAV)


@pytest.mark.e2e
def test_symphonies_page_empty_state(page: Page, live_server_url: str, store: DashboardStore) -> None:
    """Empty symphonies list shows placeholder."""
    store.broadcaster.broadcast(_full_snapshot(symphonies=[]))
    page.goto(f"{live_server_url}/symphonies")
    expect(page.locator("#symphonies-page")).to_be_visible(timeout=_WAIT_NAV)
    expect(page.locator("#symphonies-page-section")).to_contain_text(
        "No symphonies configured", timeout=_WAIT_LIVE
    )


@pytest.mark.e2e
def test_symphonies_page_shows_list(page: Page, live_server_url: str, store: DashboardStore) -> None:
    """Symphonies page displays list of symphonies."""
    # Navigate first so SSE connects, then broadcast — broadcaster only pushes
    # to currently-connected clients, so goto must precede broadcast.
    page.goto(f"{live_server_url}/symphonies")
    expect(page.locator("#symphonies-page")).to_be_visible(timeout=_WAIT_NAV)
    store.broadcaster.broadcast(
        _full_snapshot(
            symphonies=[
                {"name": "frontend", "source_project": 42, "enabled": True},
                {"name": "backend", "source_project": 43, "enabled": False},
            ]
        )
    )
    expect(page.locator("#symphonies-page-section")).to_contain_text("frontend", timeout=_WAIT_LIVE)
    expect(page.locator("#symphonies-page-section")).to_contain_text("backend", timeout=_WAIT_LIVE)


@pytest.mark.e2e
def test_symphonies_page_add_form_toggle(page: Page, live_server_url: str, store: DashboardStore) -> None:
    """Add symphony form can be toggled via button."""
    store.broadcaster.broadcast(_full_snapshot(symphonies=[]))
    page.goto(f"{live_server_url}/symphonies")
    expect(page.locator("#symphonies-page")).to_be_visible(timeout=_WAIT_NAV)
    # Click add button to show form
    page.click("button:has-text('Add symphony')")
    expect(page.locator("#sym-add-form")).to_be_visible(timeout=1000)


@pytest.mark.e2e
def test_symphonies_page_click_symphony_opens_detail(
    page: Page, live_server_url: str, store: DashboardStore
) -> None:
    """Clicking a symphony in the list opens its detail page."""
    page.goto(f"{live_server_url}/symphonies")
    expect(page.locator("#symphonies-page")).to_be_visible(timeout=_WAIT_NAV)
    store.broadcaster.broadcast(
        _full_snapshot(
            symphonies=[{"name": "test-symphony", "source_project": 42, "enabled": True}]
        )
    )
    expect(page.locator("#symphonies-page-section")).to_contain_text("test-symphony", timeout=_WAIT_LIVE)
    # Click symphony name link
    page.click('a:has-text("test-symphony")')
    expect(page).to_have_url(re.compile(r"/symphonies/test-symphony"))


@pytest.mark.e2e
def test_symphonies_page_shows_env_bootstrap_button(
    page: Page, live_server_url: str, store: DashboardStore
) -> None:
    """060: a symphony with env_bootstrap_performer_id renders a per-row Bootstrap
    button on the list page; one without renders an em-dash placeholder."""
    page.goto(f"{live_server_url}/symphonies")
    expect(page.locator("#symphonies-page")).to_be_visible(timeout=_WAIT_NAV)
    store.broadcaster.broadcast(
        _full_snapshot(
            symphonies=[
                {
                    "name": "with-bootstrap",
                    "source_project": 42,
                    "enabled": True,
                    "env_bootstrap_performer_id": "codex-ephemeral",
                },
                {
                    "name": "no-bootstrap",
                    "source_project": 43,
                    "enabled": True,
                    "env_bootstrap_performer_id": None,
                },
            ]
        )
    )
    expect(page.locator("#symphonies-page-section")).to_contain_text("with-bootstrap", timeout=_WAIT_LIVE)
    expect(page.locator('button.sym-row-bootstrap[data-sym="with-bootstrap"]')).to_be_visible()
    expect(page.locator('button.sym-row-bootstrap[data-sym="no-bootstrap"]')).to_have_count(0)


@pytest.mark.e2e
def test_symphonies_page_bootstrap_503_shows_performer_id_error(
    page: Page, live_server_url: str, store: DashboardStore
) -> None:
    """062 Fix 1: when the bootstrap performer isn't registered, the dashboard
    must surface the daemon's 503 error string (which names the missing
    performer id) in #sym-list-msg rather than swallowing it."""
    page.route(
        "**/api/symphonies/with-bootstrap/env-bootstrap",
        lambda route: route.fulfill(
            status=503,
            content_type="application/json",
            body=(
                '{"error":"Bootstrap performer \'codex-ephemeral\' is not '
                'registered with the daemon — check that it is defined in '
                'config.yaml and that coordinare loaded it at startup.",'
                '"performer_id":"codex-ephemeral"}'
            ),
        ),
    )
    page.goto(f"{live_server_url}/symphonies")
    expect(page.locator("#symphonies-page")).to_be_visible(timeout=_WAIT_NAV)
    store.broadcaster.broadcast(
        _full_snapshot(
            symphonies=[
                {
                    "name": "with-bootstrap",
                    "source_project": 42,
                    "enabled": True,
                    "env_bootstrap_performer_id": "codex-ephemeral",
                },
            ]
        )
    )
    expect(page.locator("#symphonies-page-section")).to_contain_text(
        "with-bootstrap", timeout=_WAIT_LIVE
    )
    page.locator('button.sym-row-bootstrap[data-sym="with-bootstrap"]').click()
    msg = page.locator("#sym-list-msg")
    expect(msg).to_contain_text("codex-ephemeral", timeout=2000)
    expect(msg).to_contain_text("not registered", timeout=2000)


# ---------------------------------------------------------------------------
# Admin Config page tests
# ---------------------------------------------------------------------------


@pytest.mark.e2e
def test_navigate_to_admin_config_page(page: Page, live_server_url: str) -> None:
    """Navigate to /admin/config via navbar."""
    page.goto(live_server_url)
    expect(page.locator("#navbar")).to_be_visible(timeout=_WAIT_SSE)
    # Look for admin/config link (may be under hamburger menu on mobile)
    page.click("a[href='/admin/config']")
    expect(page.locator("#admin-config-page")).to_be_visible(timeout=_WAIT_NAV)


@pytest.mark.e2e
def test_admin_config_page_loads(page: Page, live_server_url: str) -> None:
    """Admin config page loads and displays form."""
    page.goto(f"{live_server_url}/admin/config")
    expect(page.locator("#admin-config-page")).to_be_visible(timeout=_WAIT_NAV)
    expect(page.locator("#admin-config-page-section")).to_be_visible(timeout=_WAIT_LIVE)


# ---------------------------------------------------------------------------
# Personas editor interactions
# ---------------------------------------------------------------------------


@pytest.mark.e2e
def test_personas_page_save_button_exists(page: Page, live_server_url: str) -> None:
    """Personas editor has save buttons for each persona."""
    page.route(
        "**/api/personas",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body='[{"role":"implementer","instructions":"Original instructions","is_default":false}]',
        ),
    )
    page.goto(f"{live_server_url}/personas")
    expect(page.locator("#personas-page")).to_be_visible(timeout=_WAIT_NAV)
    # Save button uses class persona-save-btn (no id)
    expect(page.locator("button.persona-save-btn")).to_be_visible(timeout=_WAIT_LIVE)
    expect(page.locator("#personas-page-section")).to_contain_text("implementer", timeout=_WAIT_LIVE)


@pytest.mark.e2e
def test_personas_page_reset_button_exists(page: Page, live_server_url: str) -> None:
    """Personas editor has reset buttons for reverting changes."""
    page.route(
        "**/api/personas",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body='[{"role":"implementer","instructions":"Test instructions","is_default":false}]',
        ),
    )
    page.goto(f"{live_server_url}/personas")
    expect(page.locator("#personas-page")).to_be_visible(timeout=_WAIT_NAV)
    expect(page.locator("button.persona-reset-btn")).to_be_visible(timeout=_WAIT_LIVE)


# ---------------------------------------------------------------------------
# Awaiting Review section tests
# ---------------------------------------------------------------------------


@pytest.mark.e2e
def test_awaiting_review_card_hidden_when_phase_is_idle(
    page: Page, live_server_url: str, store: DashboardStore
) -> None:
    """Awaiting review card is hidden when phase is idle."""
    store.broadcaster.broadcast(_full_snapshot(phase="idle", phase_label="Idle"))
    page.goto(live_server_url)
    expect(page.locator("#phase")).to_have_text("Idle", timeout=_WAIT_SSE)
    expect(page.locator("#awaiting-review-card")).to_be_hidden()


@pytest.mark.e2e
def test_awaiting_review_card_appears_when_in_review_phase(
    page: Page, live_server_url: str, store: DashboardStore
) -> None:
    """Awaiting review card appears when cycle is in review phase."""
    page.goto(live_server_url)
    expect(page.locator("#phase")).to_have_text("Idle", timeout=_WAIT_SSE)
    store.broadcaster.broadcast(
        _full_snapshot(
            phase="awaiting_review",
            phase_label="Awaiting Review",
            active_card_title="Fix the bug",
            active_card_column="IN_REVIEW",
        )
    )
    expect(page.locator("#phase")).to_have_text("Awaiting Review", timeout=_WAIT_LIVE)


# ---------------------------------------------------------------------------
# Mobile navbar and hamburger menu tests
# ---------------------------------------------------------------------------


@pytest.mark.e2e
def test_hamburger_menu_visible_on_mobile(page: Page, live_server_url: str) -> None:
    """Hamburger menu button is visible on mobile (< 768px)."""
    page.set_viewport_size({"width": 375, "height": 667})
    page.goto(live_server_url)
    expect(page.locator("#navbar-hamburger")).to_be_visible()


@pytest.mark.e2e
def test_hamburger_menu_toggles_nav_links(page: Page, live_server_url: str) -> None:
    """Hamburger menu button toggles nav links visibility on mobile."""
    page.set_viewport_size({"width": 375, "height": 667})
    page.goto(live_server_url)
    expect(page.locator("#navbar-hamburger")).to_be_visible()
    # Initially menu should be hidden
    expect(page.locator("#navbar-menu")).not_to_have_class("open")
    # Click hamburger
    page.click("#navbar-hamburger")
    # Menu should now be visible
    expect(page.locator("#navbar-menu")).to_have_class("open")


@pytest.mark.e2e
def test_hamburger_menu_hides_on_navigation(page: Page, live_server_url: str) -> None:
    """Hamburger menu closes after navigating to a page."""
    page.set_viewport_size({"width": 375, "height": 667})
    page.goto(live_server_url)
    # Open menu
    page.click("#navbar-hamburger")
    expect(page.locator("#navbar-menu")).to_have_class("open")
    # Navigate
    page.click("a[href='/performers']")
    page.wait_for_timeout(_WAIT_NAV)
    # Menu should close on navigation
    expect(page.locator("#navbar-menu")).not_to_have_class("open")


# ---------------------------------------------------------------------------
# Idle panel tests
# ---------------------------------------------------------------------------


@pytest.mark.e2e
def test_idle_panel_shows_when_idle(page: Page, live_server_url: str, store: DashboardStore) -> None:
    """Idle panel appears when daemon is in idle phase."""
    page.goto(live_server_url)
    expect(page.locator("#phase")).to_have_text("Idle", timeout=_WAIT_SSE)
    expect(page.locator("#idle-panel")).to_be_visible()


@pytest.mark.e2e
def test_assignee_filter_hint_shows_when_set(
    page: Page, live_server_url: str, store: DashboardStore
) -> None:
    """Idle panel displays assignee filter hint when filter is set."""
    page.goto(live_server_url)
    expect(page.locator("#phase")).to_have_text("Idle", timeout=_WAIT_SSE)
    store.broadcaster.broadcast(
        _full_snapshot(phase="idle", phase_label="Idle", assignee_filter="@alice")
    )
    expect(page.locator("#idle-panel")).to_be_visible()
    expect(page.locator("#idle-panel-content")).to_contain_text(
        "filter", timeout=_WAIT_LIVE
    )


# ---------------------------------------------------------------------------
# Cycle history table interactions
# ---------------------------------------------------------------------------


@pytest.mark.e2e
def test_cycle_history_shows_cycles(
    page: Page, live_server_url: str, store: DashboardStore
) -> None:
    """Cycle history section displays completed cycles with durations."""
    page.goto(live_server_url)
    expect(page.locator("#phase")).to_have_text("Idle", timeout=_WAIT_SSE)

    store.broadcaster.broadcast(
        _full_snapshot(
            cycles_completed=2,
            cycle_history=[
                {
                    "phase": "merging",
                    "phase_label": "Merging",
                    "duration_seconds": 120,
                    "completed_at": "2026-03-02T10:30:00+00:00",
                },
                {
                    "phase": "idle",
                    "phase_label": "Idle",
                    "duration_seconds": 60,
                    "completed_at": "2026-03-02T10:29:00+00:00",
                },
            ],
        )
    )
    expect(page.locator("#cycles-completed")).to_contain_text("2", timeout=_WAIT_LIVE)
    expect(page.locator("#history-section")).to_contain_text("Merging", timeout=_WAIT_LIVE)
    expect(page.locator("#history-section")).to_contain_text("120", timeout=_WAIT_LIVE)


@pytest.mark.e2e
def test_history_page_shows_cycle_list(
    page: Page, live_server_url: str, store: DashboardStore
) -> None:
    """History page displays full list of cycles."""
    page.goto(f"{live_server_url}/history")
    expect(page.locator("#history-page")).to_be_visible(timeout=_WAIT_NAV)

    store.broadcaster.broadcast(
        _full_snapshot(
            cycles_completed=3,
            cycle_history=[
                {
                    "phase": "idle",
                    "phase_label": "Idle",
                    "duration_seconds": 30,
                    "completed_at": "2026-03-02T10:31:00+00:00",
                },
            ],
        )
    )
    expect(page.locator("#history-page-section")).to_be_visible(timeout=_WAIT_LIVE)
    expect(page.locator("#history-page-section")).to_contain_text("Idle", timeout=_WAIT_LIVE)
    expect(page.locator("#history-page-section")).to_contain_text("30", timeout=_WAIT_LIVE)


# ---------------------------------------------------------------------------
# Clarifications card interactions
# ---------------------------------------------------------------------------


@pytest.mark.e2e
def test_clarifications_card_appears_with_data(
    page: Page, live_server_url: str, store: DashboardStore
) -> None:
    """Clarifications card appears when clarifications exist."""
    page.goto(live_server_url)
    # "Idle" is also the static HTML default; it does not prove subscription.
    # Wait for the first received snapshot before broadcasting this update.
    page.wait_for_function("window._lastState !== null", timeout=_WAIT_SSE)

    store.broadcaster.broadcast(
        _full_snapshot(
            card_clarifications=[
                {"questions": ["What is the expected behavior?"], "answer": "Expected: returns list"},
                {"questions": ["Should we handle edge cases?"], "answer": "Yes, all cases"},
            ]
        )
    )
    expect(page.locator("#clarifications-card")).to_be_visible(timeout=_WAIT_LIVE)
    expect(page.locator("#clarifications-list")).to_contain_text(
        "What is the expected behavior?", timeout=_WAIT_LIVE
    )


@pytest.mark.e2e
def test_clarifications_card_hidden_when_empty(
    page: Page, live_server_url: str, store: DashboardStore
) -> None:
    """Clarifications card is hidden when list is empty."""
    page.goto(live_server_url)
    expect(page.locator("#phase")).to_have_text("Idle", timeout=_WAIT_SSE)
    expect(page.locator("#clarifications-card")).to_be_hidden()


# ---------------------------------------------------------------------------
# Questions card interactions
# ---------------------------------------------------------------------------


@pytest.mark.e2e
def test_questions_card_shows_questions(
    page: Page, live_server_url: str, store: DashboardStore
) -> None:
    """Questions card displays open questions when phase is blocked."""
    page.goto(live_server_url)
    expect(page.locator("#phase")).to_have_text("Idle", timeout=_WAIT_SSE)

    store.broadcaster.broadcast(
        _full_snapshot(
            phase="blocked",
            phase_label="Blocked",
            open_questions=["How should we handle this edge case?", "Do we need migration?"],
        )
    )
    expect(page.locator("#questions-card")).to_be_visible(timeout=_WAIT_LIVE)
    expect(page.locator("#questions-list")).to_contain_text(
        "How should we handle", timeout=_WAIT_LIVE
    )


@pytest.mark.e2e
def test_questions_card_hidden_when_not_blocked(
    page: Page, live_server_url: str, store: DashboardStore
) -> None:
    """Questions card is hidden when phase is not blocked."""
    page.goto(live_server_url)
    expect(page.locator("#phase")).to_have_text("Idle", timeout=_WAIT_SSE)
    expect(page.locator("#questions-card")).to_be_hidden()


# ---------------------------------------------------------------------------
# Workflow diagram visibility
# ---------------------------------------------------------------------------


@pytest.mark.e2e
def test_workflow_diagram_renders_mermaid_graph(
    page: Page, live_server_url: str, store: DashboardStore
) -> None:
    """Workflow diagram section renders when performer stage is set."""
    page.goto(live_server_url)
    # Workflow diagram is always rendered (it's part of the static layout)
    expect(page.locator("#flow-chart")).to_be_visible()


# ---------------------------------------------------------------------------
# Subsystems health status
# ---------------------------------------------------------------------------


@pytest.mark.e2e
def test_subsystems_table_shows_health_status(
    page: Page, live_server_url: str, store: DashboardStore
) -> None:
    """Subsystems table displays health status badges."""
    page.goto(live_server_url)
    expect(page.locator("#phase")).to_have_text("Idle", timeout=_WAIT_SSE)
    store.broadcaster.broadcast(
        _full_snapshot(
            subsystems=[
                {"name": "github", "status": "healthy", "required": True, "checked_at": "2026-03-02T10:00:00+00:00"},
                {"name": "slack", "status": "degraded", "required": False, "checked_at": "2026-03-02T10:00:00+00:00"},
            ]
        )
    )
    expect(page.locator("#subsystems-section")).to_be_visible(timeout=_WAIT_LIVE)
    # Check for health badges via section text (badges may be in collapsed rows)
    expect(page.locator("#subsystems-section")).to_contain_text("healthy", timeout=_WAIT_LIVE)
    expect(page.locator("#subsystems-section")).to_contain_text("degraded", timeout=_WAIT_LIVE)


@pytest.mark.e2e
def test_subsystem_unavailable_status_renders(
    page: Page, live_server_url: str, store: DashboardStore
) -> None:
    """Unavailable subsystem status displays correctly."""
    page.goto(live_server_url)
    expect(page.locator("#phase")).to_have_text("Idle", timeout=_WAIT_SSE)
    store.broadcaster.broadcast(
        _full_snapshot(
            subsystems=[
                {"name": "database", "status": "unavailable", "required": True, "checked_at": "2026-03-02T10:00:00+00:00"},
            ]
        )
    )
    expect(page.locator("#subsystems-section")).to_be_visible(timeout=_WAIT_LIVE)
    expect(page.locator("#subsystems-section")).to_contain_text("unavailable", timeout=_WAIT_LIVE)


# ---------------------------------------------------------------------------
# Active card details rendering
# ---------------------------------------------------------------------------


@pytest.mark.e2e
def test_active_card_title_and_link_visible(
    page: Page, live_server_url: str, store: DashboardStore
) -> None:
    """Active card section shows title and PR link."""
    page.goto(live_server_url)
    expect(page.locator("#phase")).to_have_text("Idle", timeout=_WAIT_SSE)
    store.broadcaster.broadcast(
        _full_snapshot(
            phase="monitoring_agent",
            phase_label="Monitoring Agent",
            active_sessions=[
                _active_session("PVTI_99", "Implement dark mode", "implementing"),
            ],
        )
    )
    expect(page.locator("#active-work-card")).to_be_visible(timeout=_WAIT_LIVE)
    expect(page.locator("#active-work-card")).to_contain_text("Implement dark mode", timeout=_WAIT_LIVE)


@pytest.mark.e2e
def test_card_tokens_and_cost_display(
    page: Page, live_server_url: str, store: DashboardStore
) -> None:
    """Card token count and cost estimate display correctly."""
    page.goto(live_server_url)
    expect(page.locator("#phase")).to_have_text("Idle", timeout=_WAIT_SSE)
    store.broadcaster.broadcast(
        _full_snapshot(
            card_tokens_total=42,
            card_cost_estimate=0.15,
        )
    )
    expect(page.locator("#card-tokens-total")).to_contain_text("42", timeout=_WAIT_LIVE)
    expect(page.locator("#card-cost-estimate")).to_contain_text("0.15", timeout=_WAIT_LIVE)


# ---------------------------------------------------------------------------
# Metrics display
# ---------------------------------------------------------------------------


@pytest.mark.e2e
def test_error_count_displays(
    page: Page, live_server_url: str, store: DashboardStore
) -> None:
    """Consecutive error count displays in metrics."""
    page.goto(live_server_url)
    expect(page.locator("#phase")).to_have_text("Idle", timeout=_WAIT_SSE)
    store.broadcaster.broadcast(_full_snapshot(consecutive_error_count=3))
    expect(page.locator("#error-count")).to_contain_text("3", timeout=_WAIT_LIVE)


@pytest.mark.e2e
def test_last_cycle_duration_displays(
    page: Page, live_server_url: str, store: DashboardStore
) -> None:
    """Last cycle duration displays in metrics."""
    page.goto(live_server_url)
    expect(page.locator("#phase")).to_have_text("Idle", timeout=_WAIT_SSE)
    store.broadcaster.broadcast(_full_snapshot(last_cycle_duration_seconds=180))
    expect(page.locator("#last-duration")).to_contain_text("180", timeout=_WAIT_LIVE)


# ---------------------------------------------------------------------------
# Phase-specific styling
# ---------------------------------------------------------------------------


@pytest.mark.e2e
def test_phase_label_color_idle(
    page: Page, live_server_url: str, store: DashboardStore
) -> None:
    """Idle phase has muted color."""
    page.goto(live_server_url)
    expect(page.locator("#phase")).to_have_text("Idle", timeout=_WAIT_SSE)
    # Initial SSE snapshot is idle; assert class membership (element has "phase phase-idle")
    expect(page.locator("#phase")).to_have_class(re.compile(r"phase-idle"))


@pytest.mark.e2e
def test_phase_label_color_dispatching(
    page: Page, live_server_url: str, store: DashboardStore
) -> None:
    """Dispatching phase has blue color."""
    page.goto(live_server_url)
    expect(page.locator("#phase")).to_have_text("Idle", timeout=_WAIT_SSE)
    store.broadcaster.broadcast(
        _full_snapshot(phase="dispatching_agent", phase_label="Dispatching Agent")
    )
    expect(page.locator("#phase")).to_have_text("Dispatching Agent", timeout=_WAIT_LIVE)
    expect(page.locator("#phase")).to_have_class(re.compile(r"phase-monitoring"))


# ---------------------------------------------------------------------------
# Daemon status and uptime
# ---------------------------------------------------------------------------


@pytest.mark.e2e
def test_daemon_start_time_displays(
    page: Page, live_server_url: str, store: DashboardStore
) -> None:
    """Daemon start time displays in footer."""
    store.broadcaster.broadcast(
        _full_snapshot(daemon_start_time="2026-03-02T09:30:00+00:00")
    )
    page.goto(live_server_url)
    start_time = page.locator("#daemon-start-time")
    expect(start_time).not_to_be_empty()


@pytest.mark.e2e
def test_project_name_and_url_display(
    page: Page, live_server_url: str, store: DashboardStore
) -> None:
    """Project name and board URL display in header."""
    store.broadcaster.broadcast(
        _full_snapshot(
            project_name="coordinare",
            project_board_url="https://github.com/orgs/test/projects/1",
        )
    )
    page.goto(live_server_url)
    # Project info should be visible
    expect(page.locator("#nav-sse-dot")).to_be_visible()


# ---------------------------------------------------------------------------
# Board summary stats
# ---------------------------------------------------------------------------


@pytest.mark.e2e
def test_board_summary_displays_column_counts(
    page: Page, live_server_url: str, store: DashboardStore
) -> None:
    """Board summary section shows column counts in the idle panel."""
    page.goto(live_server_url)
    expect(page.locator("#phase")).to_have_text("Idle", timeout=_WAIT_SSE)
    store.broadcaster.broadcast(
        _full_snapshot(
            cycles_completed=7,
            board_summary={
                "TODO": 5,
                "IN_PROGRESS": 3,
                "IN_REVIEW": 2,
                "DONE": 10,
                "BLOCKED": 1,
                "BACKLOG": 8,
            },
        )
    )
    # Idle panel shows board total (5+3+2+10=20), in-progress (3), and cycles today (7)
    expect(page.locator("#idle-panel-content")).to_contain_text("20", timeout=_WAIT_LIVE)
    expect(page.locator("#idle-panel-content")).to_contain_text("3", timeout=_WAIT_LIVE)
    expect(page.locator("#idle-panel-content")).to_contain_text("7", timeout=_WAIT_LIVE)


# ---------------------------------------------------------------------------
# Agent session display
# ---------------------------------------------------------------------------


@pytest.mark.e2e
def test_agent_session_info_hidden_when_none(
    page: Page, live_server_url: str, store: DashboardStore
) -> None:
    """Agent session info is hidden when session is idle."""
    store.broadcaster.broadcast(_full_snapshot(agent_session_id=None))
    page.goto(live_server_url)
    expect(page.locator("#agent-session")).to_be_hidden()


@pytest.mark.e2e
def test_agent_session_info_visible_when_active(
    page: Page, live_server_url: str, store: DashboardStore
) -> None:
    """Agent session info displays when agent is running."""
    page.goto(live_server_url)
    expect(page.locator("#phase")).to_have_text("Idle", timeout=_WAIT_SSE)

    store.broadcaster.broadcast(
        _full_snapshot(
            agent_session_id="sess-abc123",
            agent_dispatch_at="2026-03-02T10:00:00+00:00",
        )
    )
    expect(page.locator("#agent-session")).to_be_visible(timeout=_WAIT_LIVE)
    expect(page.locator("#agent-session-id")).to_contain_text("sess-abc123", timeout=_WAIT_LIVE)


# ---------------------------------------------------------------------------
# 059: Active Work panel — cost and stale indicators
# ---------------------------------------------------------------------------


@pytest.mark.e2e
def test_active_work_row_shows_cost(
    page: Page, live_server_url: str, store: DashboardStore
) -> None:
    """059: Active Work row renders cost when agent_dispatch_at is set, '—' when null."""
    from datetime import UTC, datetime, timedelta

    page.goto(live_server_url)
    expect(page.locator("#phase")).to_have_text("Idle", timeout=_WAIT_SSE)

    dispatch_ts = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    session = {
        **_active_session("PVTI_cost", "Cost feature", "implementing"),
        "agent_dispatch_at": dispatch_ts,
        "card_cost_estimate": 0.0123,
    }
    store.broadcaster.broadcast(
        _full_snapshot(
            phase="monitoring_performer",
            phase_label="Monitoring Performer",
            active_sessions=[session],
        )
    )

    row = page.locator("#swimlane-section .swimlane-card[data-card-id='PVTI_cost']")
    expect(row).to_be_visible(timeout=_WAIT_LIVE)
    expect(row).to_contain_text("0.0123", timeout=_WAIT_LIVE)

    # Without dispatch_at, cost column should show em-dash
    session_no_dispatch = {**_active_session("PVTI_nodispatch", "No dispatch yet", "initialising")}
    store.broadcaster.broadcast(
        _full_snapshot(
            phase="monitoring_performer",
            phase_label="Monitoring Performer",
            active_sessions=[session_no_dispatch],
        )
    )
    row2 = page.locator("#swimlane-section .swimlane-card[data-card-id='PVTI_nodispatch']")
    expect(row2).to_be_visible(timeout=_WAIT_LIVE)
    expect(row2).to_contain_text("—", timeout=_WAIT_LIVE)


@pytest.mark.e2e
def test_active_work_row_stale_session_shows_warning(
    page: Page, live_server_url: str, store: DashboardStore
) -> None:
    """059: Active Work row shows ⚠ warning when session elapsed > 30 minutes."""
    from datetime import UTC, datetime, timedelta

    page.goto(live_server_url)
    expect(page.locator("#phase")).to_have_text("Idle", timeout=_WAIT_SSE)

    stale_dispatch = (datetime.now(UTC) - timedelta(minutes=45)).isoformat()
    session = {
        **_active_session("PVTI_stale", "Long-running task", "implementing"),
        "agent_dispatch_at": stale_dispatch,
        "card_cost_estimate": 0.5,
    }
    store.broadcaster.broadcast(
        _full_snapshot(
            phase="monitoring_performer",
            phase_label="Monitoring Performer",
            active_sessions=[session],
        )
    )

    row = page.locator("#swimlane-section .swimlane-card[data-card-id='PVTI_stale']")
    expect(row).to_be_visible(timeout=_WAIT_LIVE)
    # Stale sessions prepend a ⚠ warning glyph
    expect(row).to_contain_text("⚠", timeout=_WAIT_LIVE)


@pytest.mark.e2e
def test_card_detail_auto_closes_when_session_ends(
    page: Page, live_server_url: str, store: DashboardStore
) -> None:
    """059: Card detail view auto-closes if the selected session disappears from active_sessions."""
    from datetime import UTC, datetime, timedelta

    page.goto(live_server_url)
    expect(page.locator("#phase")).to_have_text("Idle", timeout=_WAIT_SSE)

    dispatch_ts = (datetime.now(UTC) - timedelta(minutes=5)).isoformat()
    session = {
        **_active_session("PVTI_ending", "Finishing task", "implementing"),
        "agent_dispatch_at": dispatch_ts,
    }
    store.broadcaster.broadcast(
        _full_snapshot(
            phase="monitoring_performer",
            phase_label="Monitoring Performer",
            active_sessions=[session],
        )
    )

    row = page.locator("#swimlane-section .swimlane-card[data-card-id='PVTI_ending']")
    expect(row).to_be_visible(timeout=_WAIT_LIVE)
    row.click()
    expect(page.locator("#card-detail-view")).to_be_visible(timeout=_WAIT_LIVE)

    # Session disappears (task completed, coordinare goes idle)
    store.broadcaster.broadcast(_full_snapshot(phase="idle", phase_label="Idle", active_sessions=[]))

    # Detail view should auto-close; list view (now idle panel) should be visible
    expect(page.locator("#card-detail-view")).to_be_hidden(timeout=_WAIT_LIVE)


@pytest.mark.e2e
def test_idle_panel_shows_cycles_completed_label(
    page: Page, live_server_url: str, store: DashboardStore
) -> None:
    """Idle panel shows 'Cycles completed' (not 'Cycles today') and the correct count."""
    page.goto(live_server_url)
    expect(page.locator("#phase")).to_have_text("Idle", timeout=_WAIT_SSE)
    store.broadcaster.broadcast(_full_snapshot(cycles_completed=42))
    idle = page.locator("#idle-panel-content")
    expect(idle).to_contain_text("Cycles completed", timeout=_WAIT_LIVE)
    expect(idle).to_contain_text("42", timeout=_WAIT_LIVE)


@pytest.mark.e2e
def test_idle_panel_hides_when_session_becomes_active(
    page: Page, live_server_url: str, store: DashboardStore
) -> None:
    """Idle panel disappears once a session becomes active."""
    page.goto(live_server_url)
    expect(page.locator("#idle-panel")).to_be_visible(timeout=_WAIT_SSE)
    store.broadcaster.broadcast(
        _full_snapshot(
            phase="monitoring_performer",
            phase_label="Monitoring Performer",
            active_sessions=[_active_session("PVTI_1", "Fix the bug")],
        )
    )
    expect(page.locator("#idle-panel")).to_be_hidden(timeout=_WAIT_LIVE)


# ---------------------------------------------------------------------------
# Subsystem health collapse tests
# ---------------------------------------------------------------------------


@pytest.mark.e2e
def test_subsystem_details_closed_when_healthy(
    page: Page, live_server_url: str, store: DashboardStore
) -> None:
    """Healthy subsystem rows render as collapsed <details> (no open attribute)."""
    page.goto(live_server_url)
    expect(page.locator("#phase")).to_have_text("Idle", timeout=_WAIT_SSE)
    store.broadcaster.broadcast(
        _full_snapshot(
            subsystems=[
                {"name": "github", "status": "healthy", "required": True, "checked_at": "2026-03-02T10:00:00+00:00"},
            ]
        )
    )
    expect(page.locator("#subsystems-section")).to_be_visible(timeout=_WAIT_LIVE)
    # Healthy subsystem: <details> must not have the open attribute
    details = page.locator("#subsystems-section details").first
    expect(details).not_to_have_attribute("open", "")


@pytest.mark.e2e
def test_subsystem_details_open_when_degraded(
    page: Page, live_server_url: str, store: DashboardStore
) -> None:
    """Degraded subsystem rows auto-expand (<details open>) so the error is visible."""
    page.goto(live_server_url)
    expect(page.locator("#phase")).to_have_text("Idle", timeout=_WAIT_SSE)
    store.broadcaster.broadcast(
        _full_snapshot(
            subsystems=[
                {"name": "github", "status": "degraded", "required": True, "checked_at": "2026-03-02T10:00:00+00:00"},
            ]
        )
    )
    expect(page.locator("#subsystems-section")).to_be_visible(timeout=_WAIT_LIVE)
    details = page.locator("#subsystems-section details").first
    # Playwright's to_have_attribute checks attribute presence; "open" is a boolean attr
    expect(details).to_have_attribute("open", "")


# ---------------------------------------------------------------------------
# aria-current nav link tests
# ---------------------------------------------------------------------------


@pytest.mark.e2e
def test_aria_current_set_on_hard_load(page: Page, live_server_url: str) -> None:
    """Dashboard nav link has aria-current='page' on initial hard-load to /."""
    page.goto(live_server_url)
    expect(page.locator("a[href='/'][aria-current='page']")).to_have_count(1, timeout=_WAIT_SSE)


@pytest.mark.e2e
def test_aria_current_follows_spa_navigation(page: Page, live_server_url: str) -> None:
    """aria-current moves to the newly active link after SPA navigation."""
    page.goto(live_server_url)
    # Initially Dashboard link is active
    expect(page.locator("a[href='/'][aria-current='page']")).to_have_count(1, timeout=_WAIT_SSE)
    # SPA-navigate to /performers
    page.click("a[href='/performers']")
    page.wait_for_timeout(_WAIT_NAV)
    expect(page.locator("a[href='/performers'][aria-current='page']")).to_have_count(1)
    expect(page.locator("a[href='/'][aria-current='page']")).to_have_count(0)


# ---------------------------------------------------------------------------
# No horizontal scrollbar at 768px
# ---------------------------------------------------------------------------


@pytest.mark.e2e
def test_no_horizontal_scrollbar_at_768px_dashboard(page: Page, live_server_url: str) -> None:
    """Dashboard page must not overflow horizontally at 768px viewport width."""
    page.set_viewport_size({"width": 768, "height": 1024})
    page.goto(live_server_url)
    page.wait_for_timeout(_WAIT_SSE)
    overflow = page.evaluate("document.documentElement.scrollWidth > window.innerWidth")
    assert not overflow, "Horizontal scrollbar detected on Dashboard at 768px"


@pytest.mark.e2e
def test_no_horizontal_scrollbar_at_768px_performers(page: Page, live_server_url: str) -> None:
    """Performers page must not overflow horizontally at 768px viewport width."""
    page.set_viewport_size({"width": 768, "height": 1024})
    page.goto(f"{live_server_url}/performers")
    page.wait_for_timeout(_WAIT_SSE)
    overflow = page.evaluate("document.documentElement.scrollWidth > window.innerWidth")
    assert not overflow, "Horizontal scrollbar detected on Performers at 768px"


@pytest.mark.e2e
def test_no_horizontal_scrollbar_at_768px_history(page: Page, live_server_url: str) -> None:
    """History page must not overflow horizontally at 768px viewport width."""
    page.set_viewport_size({"width": 768, "height": 1024})
    page.goto(f"{live_server_url}/history")
    page.wait_for_timeout(_WAIT_SSE)
    overflow = page.evaluate("document.documentElement.scrollWidth > window.innerWidth")
    assert not overflow, "Horizontal scrollbar detected on History at 768px"


# ---------------------------------------------------------------------------
# 065 US1: Active Performers panel must reflect multi-card mutations
# ---------------------------------------------------------------------------


@pytest.mark.e2e
def test_active_performers_updates_when_one_card_kicked_back_to_todo(
    page: Page, live_server_url: str, store: DashboardStore
) -> None:
    """065 US1 repro: with two cards in flight, if one is kicked back to TODO
    (removed from active_sessions) and the other advances stage, the panel
    must show *only* the surviving session with its *new* stage. The bug is
    that the panel keeps rendering the removed card and/or the prior stage."""
    page.goto(live_server_url)
    expect(page.locator("#phase")).to_have_text("Idle", timeout=_WAIT_SSE)

    # Initial: both A and B are active and being implemented.
    store.broadcaster.broadcast(
        _full_snapshot(
            phase="monitoring_performer",
            phase_label="Monitoring Performer",
            active_session_count=2,
            active_sessions=[
                _active_session("PVTI_A", "Fix the auth bug", "implementing"),
                _active_session("PVTI_B", "Add keyboard nav", "architecting"),
            ],
        )
    )
    tiles = page.locator("#active-performer-tiles")
    expect(tiles).to_contain_text("Fix the auth bug", timeout=_WAIT_LIVE)
    expect(tiles).to_contain_text("Add keyboard nav", timeout=_WAIT_LIVE)

    # Card A is kicked back to TODO; card B advances to reviewing.
    store.broadcaster.broadcast(
        _full_snapshot(
            phase="monitoring_performer",
            phase_label="Monitoring Performer",
            active_session_count=1,
            active_sessions=[
                _active_session("PVTI_B", "Add keyboard nav", "reviewing"),
            ],
        )
    )

    # Surviving session must render with its new stage.
    expect(tiles).to_contain_text("Add keyboard nav", timeout=_WAIT_LIVE)
    expect(tiles).to_contain_text("Reviewing", timeout=_WAIT_LIVE)
    # Removed session must NOT linger.
    expect(tiles).not_to_contain_text("Fix the auth bug", timeout=_WAIT_LIVE)
    # Stale stage from the prior snapshot must not bleed through.
    expect(tiles).not_to_contain_text("Architecting", timeout=_WAIT_LIVE)


@pytest.mark.e2e
def test_active_performers_renders_two_concurrent_sessions(
    page: Page, live_server_url: str, store: DashboardStore
) -> None:
    """065 US2 surface: when the daemon reports two concurrent active sessions
    in a single snapshot, the panel must render two distinct tiles. This test
    pins the UI contract — the underlying dispatcher bug (only one card picked
    per cycle) is covered by a separate unit/integration test, but if the
    dashboard ever silently collapses N sessions into one tile, this catches
    it."""
    page.goto(live_server_url)
    expect(page.locator("#phase")).to_have_text("Idle", timeout=_WAIT_SSE)

    store.broadcaster.broadcast(
        _full_snapshot(
            phase="monitoring_performer",
            phase_label="Monitoring Performer",
            active_session_count=2,
            active_sessions=[
                _active_session("PVTI_A", "Fix the auth bug", "implementing"),
                _active_session("PVTI_B", "Add keyboard nav", "implementing"),
            ],
        )
    )

    tiles = page.locator("#active-performer-tiles")
    expect(tiles).to_contain_text("Fix the auth bug", timeout=_WAIT_LIVE)
    expect(tiles).to_contain_text("Add keyboard nav", timeout=_WAIT_LIVE)


@pytest.mark.e2e
def test_pipeline_queue_is_distinct_from_dispatching(page, live_server_url, store):
    page.goto(live_server_url)
    expect(page.locator("#phase")).to_have_text("Idle", timeout=_WAIT_SSE)
    session = _active_session("queued-card", "Queued work", "implementing")
    session["phase"] = "dispatching"
    store.broadcaster.broadcast(_full_snapshot(
        active_sessions=[session], active_session_count=1,
        session_skip_reasons={"queued-card": {"reason": "pipeline_capacity"}},
    ))
    expect(page.locator("#swimlane-section")).to_contain_text("Queued — issue limit reached")
