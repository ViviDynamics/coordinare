"""E2E browser tests: verify _DASHBOARD_HTML renders in Chrome and Firefox.

Run with:
    pytest tests/e2e/ --override-ini="addopts=" --browser chromium --browser firefox

Skipped in the standard pytest run (requires Playwright browser binaries).
Each test is marked @pytest.mark.e2e; the default addopts excludes that marker.

049/053 additions: navbar, multi-page routing (pushState), active-performer tiles,
/performers drilldown, /personas page, /history live rendering.
"""
from __future__ import annotations

from typing import Any

import pytest
from playwright.sync_api import Page, expect

from coordinare.dashboard import DashboardStore

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
    return base


def _active_session(
    card_id: str = "PVTI_1",
    card_title: str = "Fix the bug",
    stage: str = "implementing",
    phase: str = "monitoring_performer",
) -> dict:
    """Return a single active_sessions list entry."""
    return {
        "card_id": card_id,
        "card_title": card_title,
        "phase": phase,
        "performer_stage": stage,
        "card_tokens_total": 0,
        "card_cost_estimate": 0.0,
    }


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
    expect(page.locator("#active-work-section")).to_contain_text("No cards in the TODO column")


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

    expect(page.locator("#active-work-section")).to_contain_text("Fix timeout bug", timeout=_WAIT_LIVE)
    expect(page.locator("#active-work-section a")).to_have_attribute(
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
    expect(page.locator("#active-performer-tiles")).to_contain_text("implementing", timeout=_WAIT_LIVE)
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
def test_workflow_diagram_visible_without_expand_toggle(page: Page, live_server_url: str) -> None:
    """053: Workflow should be readable without expand/collapse controls."""
    page.goto(live_server_url)
    expect(page.locator("#phase")).to_have_text("Idle", timeout=_WAIT_SSE)
    fc = page.locator("#flow-chart")
    expect(fc).to_be_visible()
    expect(page.locator("#flow-chart-toggle")).to_have_count(0)


@pytest.mark.e2e
def test_desktop_layout_workflow_and_performers_are_single_column_cards(
    page: Page, live_server_url: str, store: DashboardStore
) -> None:
    """053: Workflow and Performers cards should be single-column grid cards."""
    page.set_viewport_size({"width": 1200, "height": 900})
    page.goto(live_server_url)
    expect(page.locator("#phase")).to_have_text("Idle", timeout=_WAIT_SSE)

    store.broadcaster.broadcast(
        _full_snapshot(
            phase="monitoring_performer",
            phase_label="Monitoring Performer",
            active_sessions=[_active_session("PVTI_1", "Fix layout", "implementing")],
            performer_events=[{"type": "progress", "text": "Working"}],
            role_utilization=[{"role": "implementing", "active": 1, "max": 1, "queued": 0}],
        )
    )

    expect(page.locator("#performers-card")).to_be_visible(timeout=_WAIT_LIVE)
    layout = page.evaluate(
        """() => {
          const workflowCard = document.getElementById('flow-chart').closest('.card');
          const performersCard = document.getElementById('performers-card');
          const metricsCard = document.getElementById('cycles-completed').closest('.card');
          const wf = workflowCard.getBoundingClientRect();
          const pf = performersCard.getBoundingClientRect();
          const mt = metricsCard.getBoundingClientRect();
          return {
            wfWidth: wf.width,
            pfWidth: pf.width,
            mtWidth: mt.width,
            viewportWidth: window.innerWidth,
          };
        }"""
    )
    assert layout["wfWidth"] < layout["viewportWidth"] * 0.75
    assert layout["pfWidth"] < layout["viewportWidth"] * 0.75
    assert abs(layout["wfWidth"] - layout["mtWidth"]) < 120
    assert abs(layout["pfWidth"] - layout["mtWidth"]) < 120


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
            active_sessions=[_active_session("PVTI_1", "Keyboard flow", "implementing")],
            performer_events=[{"type": "progress", "text": "Initial"}],
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
            active_sessions=[_active_session("PVTI_1", "Keyboard flow", "implementing")],
            performer_events=[{"type": "progress", "text": "Updated"}],
        )
    )
    expect(page.locator("#performers-page-detail-view")).to_be_visible(timeout=_WAIT_LIVE)
    expect(page.locator("#performers-page-detail")).to_contain_text("Updated", timeout=_WAIT_LIVE)


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
