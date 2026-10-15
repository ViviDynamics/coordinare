"""#139: the symphony switcher filters the whole dashboard, and the header and the
Cards panel never disagree. Checked in a real browser.
"""
from __future__ import annotations

import re
from typing import Any

import pytest
from playwright.sync_api import Page, expect

from coordinare.dashboard import DashboardStore
from tests.e2e.test_dashboard_browser import _WAIT_LIVE, _WAIT_SSE, _active_session, _full_snapshot

_ZERO = {
    "tokens": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0, "total": 0},
    "cost": {"usd": 0.0, "partial_usd": 0.0, "kind_usd": {}, "unknown_reason": None},
    "models": [],
    "pricing": {"as_of": "2026-10-01", "source": "table"},
}
_SPENT = {
    "tokens": {"input": 1050, "output": 1000, "cache_read": 0, "cache_write": 0, "total": 2050},
    "cost": {"usd": 0.13, "partial_usd": 0.13, "kind_usd": {}, "unknown_reason": None},
    "models": [],
    "pricing": {"as_of": "2026-10-01", "source": "table"},
}


def _symphony(name: str, number: int, repo: str, *, todo: list[str] = (), blocked: list[str] = (),
              sessions: list[str] = (), titles: dict[str, str] | None = None, org: str = "acme") -> dict[str, Any]:
    return {
        "name": name, "priority": 0, "github_project_number": number,
        "github_org": org, "repo": repo,
        "state": {
            "board_snapshot": {"TODO": list(todo), "BLOCKED": list(blocked), "IN_PROGRESS": [], "IN_REVIEW": [],
                               "BACKLOG": [], "DONE": []},
            "board_titles": titles or {},
            "board_issue_numbers": {}, "board_issue_urls": {}, "board_pr_urls": {},
            "todo_waits": {}, "todo_capacity": None,
            "session_skip_reasons": {},
            "active_sessions": {sid: {} for sid in sessions},
        },
    }


def _two_symphonies() -> list[dict[str, Any]]:
    return [
        _symphony("alpha-sym", 16, "alpha-repo", todo=["PVTI_A1"], sessions=["PVTI_A1"],
                  titles={"PVTI_A1": "Alpha card"}),
        _symphony("beta-sym", 2, "beta-repo", blocked=["PVTI_B1"], sessions=["PVTI_B1"],
                  titles={"PVTI_B1": "Beta card"}),
    ]


def _snapshot(symphonies: list[dict[str, Any]], **overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "phase": "monitoring_performer", "phase_label": "Watching an agent", "symphonies": symphonies,
        "active_sessions": [
            _active_session("PVTI_A1", "Alpha card", "implementing", phase="monitoring_performer"),
            _active_session("PVTI_B1", "Beta card", "reviewing", phase="monitoring_performer"),
        ],
        "token_usage": {
            "tracked": True, "reason": None, "day": "2026-10-14", "week_start": "2026-10-08",
            "today": _SPENT, "week": _SPENT,
            "by_symphony": {"alpha-sym": {"today": _SPENT, "week": _SPENT},
                            "beta-sym": {"today": _ZERO, "week": _ZERO}},
        },
    }
    base.update(overrides)
    return _full_snapshot(**base)


def _connected(page: Page, url: str) -> None:
    """Load a page and wait for its stream to be live, so a broadcast is not sent before anyone listens."""
    # Firefox can keep load waiting on the dashboard's long lived SSE request.
    # The Live indicator below is the readiness condition we need.
    page.goto(url, wait_until="domcontentloaded", timeout=_WAIT_SSE)
    expect(page.locator("#connection-label")).to_have_text("Live", timeout=_WAIT_SSE)


def _open(page: Page, url: str, store: DashboardStore, snapshot: dict[str, Any]) -> None:
    _connected(page, url)
    store.broadcaster.broadcast(snapshot)


@pytest.mark.e2e
def test_the_header_offers_all_symphonies_and_each_one(page: Page, live_server_url: str, store: DashboardStore) -> None:
    _open(page, live_server_url, store, _snapshot(_two_symphonies()))
    select = page.locator("#symphony-select")
    expect(select).to_be_visible(timeout=_WAIT_LIVE)
    expect(select).to_have_value("")
    expect(select.locator("option")).to_have_text(["All symphonies", "alpha-sym", "beta-sym"])
    expect(page.locator("#symphony-identity")).to_have_text("")
    expect(page.locator("#swimlane-section")).to_contain_text("Alpha card")
    expect(page.locator("#swimlane-section")).to_contain_text("Beta card")


@pytest.mark.e2e
def test_choosing_a_symphony_filters_every_panel(page: Page, live_server_url: str, store: DashboardStore) -> None:
    question = {"card_id": "PVTI_B1", "card_number": 7, "card_title": "Beta card", "stage": "reviewing",
                "text": "Which branch?", "asked_at": "2026-10-14T09:00:00+00:00", "issue_url": None}
    _open(page, live_server_url, store, _snapshot(_two_symphonies(), open_questions=[question]))
    expect(page.locator("#swimlane-section")).to_contain_text("Beta card", timeout=_WAIT_LIVE)

    page.locator("#symphony-select").select_option("beta-sym")

    expect(page.locator("#swimlane-section")).not_to_contain_text("Alpha card")
    expect(page.locator("#swimlane-section")).to_contain_text("Beta card")
    expect(page.locator("#active-performer-tiles .ap-pill-card[title='Card PVTI_A1']")).to_have_count(0)
    expect(page.locator("#active-performer-tiles .ap-pill-card[title='Card PVTI_B1']")).to_have_count(1)
    expect(page.locator("#symphony-identity")).to_have_text("beta-sym · GitHub Project #2 · acme/beta-repo")
    expect(page.locator("#spend-strip")).to_have_text("$0.00 today · $0.00 week")
    expect(page.locator("#attention-list")).to_contain_text("Which branch?")

    page.locator("#symphony-select").select_option("alpha-sym")

    expect(page.locator("#swimlane-section")).not_to_contain_text("Beta card")
    expect(page.locator("#attention-list")).not_to_contain_text("Which branch?")
    expect(page.locator("#spend-strip")).to_have_text("$0.13 today · $0.13 week")


@pytest.mark.e2e
def test_all_symphonies_names_the_symphony_of_each_card(page: Page, live_server_url: str, store: DashboardStore) -> None:
    _open(page, live_server_url, store, _snapshot(_two_symphonies()))
    expect(page.locator(".swimlane-card-symphony")).to_have_text(["alpha-sym", "beta-sym"], timeout=_WAIT_LIVE)


@pytest.mark.e2e
def test_the_choice_survives_a_reload_and_navigation(page: Page, live_server_url: str, store: DashboardStore) -> None:
    _open(page, live_server_url, store, _snapshot(_two_symphonies()))
    page.locator("#symphony-select").select_option("beta-sym")
    expect(page.locator("#swimlane-section")).not_to_contain_text("Alpha card")

    page.reload(wait_until="domcontentloaded", timeout=_WAIT_SSE)
    expect(page.locator("#connection-label")).to_have_text("Live", timeout=_WAIT_SSE)
    store.broadcaster.broadcast(_snapshot(_two_symphonies()))
    expect(page.locator("#symphony-select")).to_have_value("beta-sym", timeout=_WAIT_LIVE)
    expect(page.locator("#swimlane-section")).not_to_contain_text("Alpha card")

    page.locator("#navbar a.nav-link[href='/performers']").click()
    # 139 review, item 4: navigating keeps the chosen symphony in the address.
    expect(page).to_have_url(re.compile(r"/performers\?symphony=beta-sym$"))
    expect(page.locator("#symphony-select")).to_have_value("beta-sym")
    expect(page.locator("#symphony-identity")).to_have_text("beta-sym · GitHub Project #2 · acme/beta-repo")


@pytest.mark.e2e
def test_a_deep_link_selects_its_symphony(page: Page, live_server_url: str, store: DashboardStore) -> None:
    _open(page, f"{live_server_url}/?symphony=beta-sym", store, _snapshot(_two_symphonies()))
    expect(page.locator("#symphony-select")).to_have_value("beta-sym", timeout=_WAIT_LIVE)
    expect(page.locator("#swimlane-section")).not_to_contain_text("Alpha card")
    expect(page.locator("#swimlane-section")).to_contain_text("Beta card")


@pytest.mark.e2e
def test_a_deep_link_to_an_unknown_symphony_shows_all(page: Page, live_server_url: str, store: DashboardStore) -> None:
    _open(page, f"{live_server_url}/?symphony=no-such-symphony", store, _snapshot(_two_symphonies()))
    expect(page.locator("#symphony-select")).to_have_value("", timeout=_WAIT_LIVE)
    expect(page.locator("#swimlane-section")).to_contain_text("Alpha card")
    expect(page.locator("#swimlane-section")).to_contain_text("Beta card")


@pytest.mark.e2e
def test_one_symphony_shows_its_identity_and_no_switcher(page: Page, live_server_url: str, store: DashboardStore) -> None:
    solo = [_symphony("solo", 5, "solo-repo", todo=["PVTI_S1"], titles={"PVTI_S1": "Solo card"})]
    _open(page, live_server_url, store, _snapshot(solo, active_sessions=[]))
    expect(page.locator("#symphony-identity")).to_have_text("solo · GitHub Project #5 · acme/solo-repo", timeout=_WAIT_LIVE)
    expect(page.locator("#symphony-select")).to_have_count(0)
    expect(page.locator("#swimlane-section")).to_contain_text("Solo card")
    expect(page.locator(".swimlane-card-symphony")).to_have_count(0)


@pytest.mark.e2e
def test_the_identity_links_go_to_github(page: Page, live_server_url: str, store: DashboardStore) -> None:
    _open(page, live_server_url, store, _snapshot(_two_symphonies()))
    page.locator("#symphony-select").select_option("alpha-sym")
    project = page.locator("#symphony-identity a", has_text="GitHub Project #16")
    expect(project).to_have_attribute("href", "https://github.com/orgs/acme/projects/16")
    expect(page.locator("#symphony-identity a", has_text="acme/alpha-repo")).to_have_attribute(
        "href", "https://github.com/acme/alpha-repo",
    )


@pytest.mark.e2e
def test_hostile_symphony_names_are_escaped(page: Page, live_server_url: str, store: DashboardStore) -> None:
    hostile = '<img src=x onerror="window.__coordinare139=1">'
    symphonies = [
        _symphony(hostile, 9, "repo-x", todo=["PVTI_H1"], titles={"PVTI_H1": "Hostile card"}, org="javascript:alert(1)"),
        _symphony("plain", 1, "plain-repo", blocked=["PVTI_P1"], titles={"PVTI_P1": "Plain card"}),
    ]
    _open(page, live_server_url, store, _snapshot(symphonies, active_sessions=[]))
    expect(page.locator("#symphony-select option").nth(1)).to_have_text(hostile, timeout=_WAIT_LIVE)
    expect(page.locator("#project-link img")).to_have_count(0)
    expect(page.locator("#symphony-identity a[href^='javascript']")).to_have_count(0)
    assert page.evaluate("window.__coordinare139 === undefined") is True

    page.locator("#symphony-select").select_option(hostile)
    expect(page.locator("#swimlane-section")).to_contain_text("Hostile card")
    expect(page.locator("#symphony-identity")).to_contain_text(hostile)
    assert page.evaluate("window.__coordinare139 === undefined") is True


@pytest.mark.e2e
def test_no_user_facing_board_or_swimlane_text_remains(page: Page, live_server_url: str, store: DashboardStore) -> None:
    _open(page, live_server_url, store, _snapshot(_two_symphonies()))
    # 154: the Cards heading carries the board's counts beside its name.
    expect(page.locator("#active-work-card h2")).to_contain_text("Cards", timeout=_WAIT_LIVE)
    text = page.locator("body").inner_text()
    assert not re.search(r"\bBoard\b", text), "a Board label is still visible"
    assert "swimlane" not in text.lower(), "a swimlane label is still visible"

    _connected(page, f"{live_server_url}/symphonies")
    store.broadcaster.broadcast(_snapshot(_two_symphonies()))
    expect(page.locator("#symphonies-page-section th").nth(2)).to_have_text("GitHub Project", timeout=_WAIT_LIVE)
    expect(page.locator("#symphonies-page-section th").nth(3)).to_have_text("Repository")
    expect(page.locator("#symphonies-page-section td").nth(2)).to_have_text("#16")
    board_text = page.locator("body").inner_text()
    assert not re.search(r"\bBoard\b", board_text), "a Board label is still visible on Symphonies"
