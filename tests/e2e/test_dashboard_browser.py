"""E2E browser tests (T036): verify _DASHBOARD_HTML renders in Chrome and Firefox.

Run with:
    pytest tests/e2e/ --override-ini="addopts=" --browser chromium --browser firefox

Skipped in the standard pytest run (requires Playwright browser binaries).
Each test is marked @pytest.mark.e2e; the default addopts excludes that marker.
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


def _full_snapshot(**overrides: Any) -> dict:
    """Return a complete dashboard snapshot dict, optionally with field overrides."""
    base: dict = {
        "phase": "idle",
        "phase_label": "Idle",
        "active_card_title": None,
        "active_card_column": None,
        "pr_url": None,
        "agent_session_id": None,
        "open_questions": [],
        "subsystems": [],
        "cycles_completed": 0,
        "last_cycle_duration_seconds": None,
        "consecutive_error_count": 0,
        "daemon_start_time": "2026-03-02T09:30:00+00:00",
        "cycle_history": [],
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Page load and static structure
# ---------------------------------------------------------------------------


@pytest.mark.e2e
def test_page_title_and_heading(page: Page, live_server_url: str) -> None:
    """Dashboard page must load with the correct title and h1."""
    page.goto(live_server_url)
    expect(page).to_have_title("Coordinare Dashboard")
    expect(page.locator("h1")).to_contain_text("Coordinare Dashboard")


@pytest.mark.e2e
def test_all_section_ids_present(page: Page, live_server_url: str) -> None:
    """All required DOM element IDs must be present in the served HTML."""
    page.goto(live_server_url)
    for element_id in (
        "phase",
        "card-section",
        "questions-card",
        "cycles-completed",
        "last-duration",
        "error-count",
        "daemon-start-time",
        "subsystems-section",
        "history-section",
        "disconnected-banner",
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
    expect(page.locator("#card-section")).to_contain_text("No active card")


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
    """Scenario 2: Card title, column, and PR link must appear after broadcast."""
    page.goto(live_server_url)
    expect(page.locator("#phase")).to_have_text("Idle", timeout=_WAIT_SSE)

    store.broadcaster.broadcast(
        _full_snapshot(
            phase="monitoring_agent",
            phase_label="Monitoring Agent",
            active_card_title="Fix timeout bug",
            active_card_column="In Progress",
            pr_url="https://github.com/org/repo/pull/42",
            agent_session_id="sess-abc",
        )
    )

    expect(page.locator("#card-section")).to_contain_text("Fix timeout bug", timeout=_WAIT_LIVE)
    expect(page.locator("#card-section")).to_contain_text("In Progress", timeout=_WAIT_LIVE)
    expect(page.locator("#card-section a")).to_have_attribute(
        "href", "https://github.com/org/repo/pull/42", timeout=_WAIT_LIVE
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
                {
                    "timestamp": "2026-03-02T10:05:00+00:00",
                    "phase": "idle",
                    "duration_seconds": 0.45,
                    "outcome": "success",
                },
                {
                    "timestamp": "2026-03-02T10:04:30+00:00",
                    "phase": "idle",
                    "duration_seconds": 0.0,
                    "outcome": "error",
                },
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
        _full_snapshot(
            cycles_completed=42,
            last_cycle_duration_seconds=1.23,
            consecutive_error_count=2,
        )
    )

    expect(page.locator("#cycles-completed")).to_have_text("42", timeout=_WAIT_LIVE)
    expect(page.locator("#error-count")).to_have_text("2", timeout=_WAIT_LIVE)
