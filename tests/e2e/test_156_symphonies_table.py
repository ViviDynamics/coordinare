"""#156: the Symphonies table explains every column or links to more detail. Checked in a real browser.

Each row carries its own status, env state, card counts and spend. A row opens its symphony's page,
the project and repository link to GitHub, and a chosen symphony hides the other rows' spend.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from playwright.sync_api import Page, Route, expect

from coordinare.dashboard import DashboardStore
from tests.e2e.test_dashboard_browser import _WAIT_LIVE, _WAIT_SSE, _full_snapshot

EM_DASH = "\u2014"
EN_DASH = "\u2013"
_TABLE = "#symphonies-page-section"


def _cost(usd: float) -> dict[str, Any]:
    return {
        "tokens": {"input": 1000, "output": 1000, "cache_read": 0, "cache_write": 0, "total": 2000},
        "cost": {"usd": usd, "partial_usd": usd, "kind_usd": {}, "unknown_reason": None},
        "models": [],
        "pricing": {"as_of": "2026-10-01", "source": "table"},
    }


def _row(name: str, **overrides: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "name": name,
        "priority": 0,
        "github_project_number": 16,
        "github_org": "acme",
        "repo": f"{name}-repo",
        "enabled": True,
        "poll_interval_seconds": 30,
        "daemon_cycles_completed": 3,
        "cycle_count": 3,
        "error_count": 0,
        "last_poll_at": "2026-10-14T11:59:00+00:00",
        "env_bootstrap_performer_id": None,
        "env_status": {
            "state": "not_configured",
            "reason": "No env bootstrap performer is configured for this symphony.",
        },
        "cards_active": 0,
        "cards_queued": 0,
        "cards_paused": 0,
        "spend_week": {"tracked": True, "reason": None, **_cost(0.0)},
        "state": {
            "board_snapshot": None,
            "board_titles": {},
            "board_issue_numbers": {},
            "board_issue_urls": {},
            "board_pr_urls": {},
            "session_skip_reasons": {},
            "todo_waits": {},
            "todo_capacity": None,
            "active_sessions": {},
            "last_error": None,
            "active_card": None,
        },
    }
    entry.update(overrides)
    return entry


def _symphonies() -> list[dict[str, Any]]:
    return [
        _row(
            "alpha-sym",
            github_project_number=16,
            github_org="acme",
            repo="alpha-repo",
            env_bootstrap_performer_id="perf",
            env_status={
                "state": "ready",
                "reason": "The env cache is ready for this symphony's performers.",
            },
            cards_active=2,
            cards_queued=1,
            spend_week={"tracked": True, "reason": None, **_cost(0.13)},
        ),
        _row(
            "beta-sym",
            github_project_number=2,
            github_org="acme",
            repo="beta-repo",
            cards_paused=1,
            spend_week={"tracked": True, "reason": None, **_cost(0.5)},
        ),
        _row(
            "gamma-sym",
            enabled=False,
            env_bootstrap_performer_id="perf",
            env_status={"state": "bootstrapping", "reason": "An env bootstrap is running."},
        ),
        _row(
            "delta-sym",
            error_count=2,
            env_bootstrap_performer_id="perf",
            env_status={"state": "failed", "reason": "image pull denied"},
            state={
                "last_error": "GitHub 401 Bad credentials",
                "board_snapshot": None,
                "active_sessions": {},
            },
        ),
        _row(
            "placeholder",
            cycle_count=0,
            last_poll_at=None,
            daemon_cycles_completed=0,
            env_bootstrap_performer_id="perf",
            env_status={
                "state": "not_bootstrapped",
                "reason": "No env bootstrap has finished yet.",
            },
        ),
    ]


def _open(page: Page, url: str, store: DashboardStore) -> None:
    # Headline figures now come from the statistics API, with the same fixture identity.
    entries = _symphonies()

    def stats(route: Route) -> None:
        route.fulfill(json={
            "group_by": "symphony", "window": "7d", "start": "2026-10-07T12:00:00Z",
            "end": "2026-10-14T12:00:00Z", "partial": False,
            "groups": [{"key": entry["name"], **entry["spend_week"]} for entry in entries],
            "symphony_health": {entry["name"]: entry for entry in entries},
            "card_flow": {entry["name"]: {"cards_completed": 0} for entry in entries},
        })

    page.route("**/api/stats?*", stats)
    page.goto(url)
    expect(page.locator("#connection-label")).to_have_text("Live", timeout=_WAIT_SSE)
    store.broadcaster.broadcast(
        _full_snapshot(phase="idle", phase_label="Idle", symphonies=entries),
    )
    expect(page.locator(_TABLE)).to_contain_text("alpha-sym", timeout=_WAIT_LIVE)


def _cell(page: Page, name: str, index: int) -> Any:
    return page.locator(f"tr.symphony-row[data-href='/symphonies/{name}'] td").nth(index)


# Column indexes, as the table lays them out: name, status, project, repository, env, active, queued,
# spend, checks run, failed checks, last checked.
_STATUS, _PROJECT, _REPO, _ENV, _ACTIVE, _QUEUED, _SPEND = 1, 2, 3, 4, 5, 6, 7


@pytest.mark.e2e
def test_a_row_opens_its_symphonys_page(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    _open(page, live_server_url + "/symphonies", store)

    _cell(page, "beta-sym", _STATUS).click()

    expect(page).to_have_url(re.compile(r"/symphonies/beta-sym$"), timeout=_WAIT_LIVE)


@pytest.mark.e2e
def test_the_project_and_the_repository_link_to_github(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    _open(page, live_server_url + "/symphonies", store)

    # Two symphonies share project 16 in the fixture, so the first link is the one checked.
    project = page.locator(f"{_TABLE} a[href='https://github.com/orgs/acme/projects/16']").first
    expect(project).to_have_text("#16")
    repo = page.locator(f"{_TABLE} a[href='https://github.com/acme/alpha-repo']")
    expect(repo).to_have_text("acme/alpha-repo")
    expect(
        page.locator(f"{_TABLE} a[href='https://github.com/orgs/acme/projects/2']"),
    ).to_have_text("#2")


@pytest.mark.e2e
def test_each_status_shows_its_reason(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    _open(page, live_server_url + "/symphonies", store)

    expect(_cell(page, "alpha-sym", _STATUS)).to_contain_text("Active")
    expect(_cell(page, "alpha-sym", _STATUS)).to_contain_text("Checked every 30 seconds.")
    expect(_cell(page, "beta-sym", _STATUS)).to_contain_text("Paused")
    expect(_cell(page, "beta-sym", _STATUS)).to_contain_text("1 card is paused on the board.")
    expect(_cell(page, "gamma-sym", _STATUS)).to_contain_text("Disabled")
    expect(_cell(page, "gamma-sym", _STATUS)).to_contain_text("enabled: false in the config")
    expect(_cell(page, "delta-sym", _STATUS)).to_contain_text("Error")
    expect(_cell(page, "delta-sym", _STATUS)).to_contain_text(
        "Last check failed: GitHub 401 Bad credentials",
    )
    expect(_cell(page, "placeholder", _STATUS)).to_contain_text("Not checked yet")


@pytest.mark.e2e
def test_each_env_state_is_named_with_its_reason(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    _open(page, live_server_url + "/symphonies", store)

    expect(_cell(page, "alpha-sym", _ENV)).to_contain_text("Ready")
    expect(_cell(page, "alpha-sym", _ENV)).to_contain_text("The env cache is ready")
    expect(_cell(page, "gamma-sym", _ENV)).to_contain_text("Bootstrapping")
    expect(_cell(page, "delta-sym", _ENV)).to_contain_text("Failed")
    expect(_cell(page, "delta-sym", _ENV)).to_contain_text("image pull denied")
    expect(_cell(page, "beta-sym", _ENV)).to_contain_text("Not configured")
    expect(_cell(page, "beta-sym", _ENV)).to_contain_text(
        "No env bootstrap performer is configured",
    )
    expect(_cell(page, "placeholder", _ENV)).to_contain_text("Not bootstrapped")


@pytest.mark.e2e
def test_the_active_queued_and_spend_columns_show_each_symphonys_own_figures(
    page: Page,
    live_server_url: str,
    store: DashboardStore,
) -> None:
    _open(page, live_server_url + "/symphonies", store)

    expect(_cell(page, "alpha-sym", _ACTIVE)).to_have_text("2")
    expect(_cell(page, "alpha-sym", _QUEUED)).to_have_text("1")
    expect(_cell(page, "alpha-sym", _SPEND)).to_have_text("$0.13")
    expect(_cell(page, "beta-sym", _SPEND)).to_have_text("$0.50")
    expect(_cell(page, "beta-sym", _ACTIVE)).to_have_text("0")


@pytest.mark.e2e
def test_the_table_has_no_dash_in_it(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    _open(page, live_server_url + "/symphonies", store)

    table = page.locator(_TABLE)
    expect(table.locator("tbody tr")).to_have_count(5, timeout=_WAIT_LIVE)
    text = table.inner_text()
    assert EM_DASH not in text
    assert EN_DASH not in text
    expect(table.locator("thead")).not_to_contain_text(EM_DASH)


@pytest.mark.e2e
def test_a_scoped_viewer_sees_only_their_symphonys_spend(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    _open(page, live_server_url + "/symphonies?symphony=beta-sym", store)

    expect(_cell(page, "beta-sym", _SPEND)).to_have_text("$0.50")
    alpha_spend = _cell(page, "alpha-sym", _SPEND)
    expect(alpha_spend).to_contain_text("Hidden: another symphony is shown")
    expect(page.locator(_TABLE)).not_to_contain_text("$0.13")
    expect(_cell(page, "alpha-sym", _ACTIVE)).not_to_have_text("2")


@pytest.mark.e2e
def test_a_scoped_viewer_never_sees_another_symphonys_error_or_env_reason(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    # delta-sym carries a recorded error and a failed env with a reason; scoped to alpha-sym, neither
    # text may appear anywhere in delta-sym's row. Its status word and env word stay.
    _open(page, live_server_url + "/symphonies?symphony=alpha-sym", store)

    row = page.locator("tr.symphony-row[data-href='/symphonies/delta-sym']")
    expect(row).to_contain_text("Hidden: another symphony is shown", timeout=_WAIT_LIVE)
    html = row.evaluate("el => el.outerHTML")
    assert "GitHub 401" not in html, html
    assert "image pull denied" not in html, html
    expect(row.locator("td").nth(_STATUS)).to_contain_text("Error")
    expect(row.locator("td").nth(_ENV)).to_contain_text("Failed")
