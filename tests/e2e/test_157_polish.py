"""#157: the small polish, checked in a real browser. One test per item of the issue.

The header chip shows the login name with the full name and provider in its title. Absolute times
show their age with the exact time and zone on hover. A run's id is shortened and copyable. The
activity feed is as tall as its entries, up to the board. A field with no default has no "default:"
line. The whole Workflow row opens the section. Each persona says when and by whom it was last edited.
Runs in Chromium and Firefox (--browser firefox).
"""
from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from playwright.sync_api import Page, Route, expect

from coordinare.dashboard import DashboardStore
from tests.e2e.test_143_settings import _open_settings, _setting, _snapshot, _stub_config
from tests.e2e.test_154_layout import _activity, _layout_snapshot
from tests.e2e.test_dashboard_browser import _WAIT_LIVE, _WAIT_SSE, _full_snapshot

_RUN = "299a8349-6f0e-4b1e-9d2a-0c1f2e3d4a5b"
_SHORT = "run 299a83"
_EXACT = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} \S+$")
_AGE = re.compile(r"^\d+(s|m|h|d) ago$")


def _ago(seconds: float) -> str:
    return (datetime.now(UTC) - timedelta(seconds=seconds)).isoformat()


@pytest.mark.e2e
def test_the_user_chip_shows_the_login_name_with_the_full_name_and_provider_in_its_title(
    page: Page, live_server_url: str,
) -> None:
    account = {
        "authentication": "oidc", "name": "Authentik Default Admin", "email": "admin@example.test",
        "subject": "sub-1", "username": "akadmin", "provider": "auth.example.test",
    }
    page.route("**/api/account", lambda route: route.fulfill(
        status=200, content_type="application/json", body=json.dumps(account),
    ))
    page.goto(live_server_url)
    chip = page.locator("#account-menu summary")
    expect(chip.locator(".account-name")).to_have_text("akadmin", timeout=_WAIT_SSE)
    expect(chip).to_have_attribute("title", "Authentik Default Admin (auth.example.test)")
    assert "Authentik Default Admin" not in chip.inner_text()


@pytest.mark.e2e
def test_an_absolute_time_reads_as_an_age_with_the_exact_time_and_zone_on_hover(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    page.goto(live_server_url)
    expect(page.locator("#phase")).not_to_be_empty(timeout=_WAIT_SSE)
    stamp = _ago(35)
    store.broadcaster.broadcast({"_event": "activity_event", "entries": [
        {**_activity(1)[0], "seq": 7157, "card_id": "PVTI_157", "text": "timed entry 157", "timestamp": stamp},
    ]})
    row = page.locator("#activity-feed .af-row", has_text="timed entry 157")
    expect(row).to_have_count(1, timeout=_WAIT_LIVE)
    shown = row.locator(".af-time time")
    expect(shown).to_have_text(_AGE)
    title = shown.get_attribute("title") or ""
    assert _EXACT.match(title), title


@pytest.mark.e2e
def test_the_agent_run_is_shortened_with_a_copy_button_and_the_full_id_on_hover(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    page.goto(live_server_url)
    expect(page.locator("#phase")).not_to_be_empty(timeout=_WAIT_SSE)
    store.broadcaster.broadcast(_full_snapshot(phase="monitoring_performer", agent_session_id=_RUN))
    # The current run sits in the Metrics panel, which #154 collapses by default.
    page.locator("#metrics-card > summary").click()
    current = page.locator("#agent-session")
    expect(current).to_be_visible(timeout=_WAIT_LIVE)
    expect(current.locator(".run-id")).to_have_text(_SHORT)
    expect(current.locator(".run-id")).to_have_attribute("title", _RUN)
    expect(current.locator(".run-id-copy")).to_have_count(1)
    assert _RUN not in current.inner_text()


@pytest.mark.e2e
def test_the_activity_panel_is_only_as_tall_as_its_entries_and_never_taller_than_the_board(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    page.set_viewport_size({"width": 1280, "height": 800})
    page.goto(live_server_url)
    expect(page.locator("#phase")).not_to_be_empty(timeout=_WAIT_SSE)
    store.broadcaster.broadcast(_layout_snapshot())
    store.broadcaster.broadcast({"_event": "activity_event", "entries": _activity(2)})
    expect(page.locator("#activity-feed .af-row").first).to_be_visible(timeout=_WAIT_LIVE)
    board = page.locator("#active-work-card").bounding_box()
    feed = page.locator("#activity-feed-card").bounding_box()
    assert board is not None and feed is not None
    assert feed["height"] < board["height"] - 40, (feed, board)
    store.broadcaster.broadcast({"_event": "activity_event", "entries": _activity(30)})
    expect(page.locator("#activity-feed .af-row")).to_have_count(30, timeout=_WAIT_LIVE)
    board = page.locator("#active-work-card").bounding_box()
    feed = page.locator("#activity-feed-card").bounding_box()
    assert board is not None and feed is not None
    assert feed["height"] <= board["height"] + 1, (feed, board)


@pytest.mark.e2e
def test_a_setting_with_no_default_has_no_default_line(page: Page, live_server_url: str) -> None:
    snapshot = _snapshot()
    general = next(section for section in snapshot["sections"] if section["id"] == "global")
    general["settings"].append(_setting("global.project_name", "Project name", "str", "", default=""))
    general["settings"].append(_setting("global.labelled", "Labelled setting", "int", 5, default=30))
    _stub_config(page, snapshot, [])
    _open_settings(page, live_server_url)
    no_default = page.locator("[data-fieldrow]").filter(has=page.locator('[data-field="project_name"]'))
    expect(no_default).to_have_count(1)
    assert "default:" not in no_default.inner_text()
    with_default = page.locator("[data-fieldrow]").filter(has=page.locator('[data-field="labelled"]'))
    expect(with_default).to_contain_text("default: 30")


@pytest.mark.e2e
def test_one_click_anywhere_on_the_workflow_row_opens_it_and_turns_the_chevron(
    page: Page, live_server_url: str,
) -> None:
    page.goto(live_server_url)
    expect(page.locator("#phase")).not_to_be_empty(timeout=_WAIT_SSE)
    summary = page.locator("#workflow-card > details > summary")
    flow = page.locator("#flow-chart")
    expect(flow).to_be_hidden()
    box = summary.bounding_box()
    assert box is not None
    # The far edge of the row, away from the word "Workflow": the whole row is the control.
    summary.click(position={"x": box["width"] - 12, "y": box["height"] / 2})
    expect(flow).to_be_visible()
    expect(page.locator("#workflow-card > details")).to_have_attribute("open", "")
    assert summary.evaluate("el => getComputedStyle(el, '::before').transform") != "none"


@pytest.mark.e2e
def test_each_persona_says_when_and_by_whom_it_was_last_edited(page: Page, live_server_url: str) -> None:
    personas = [
        {"role": "implementer", "instructions": "Implement the change.", "is_default": True},
        {"role": "reviewer", "instructions": "Review carefully.", "is_default": False},
    ]
    histories: dict[str, dict[str, Any]] = {
        "implementer": {"role": "implementer", "available": True, "versions": [
            {"version": 1, "changed_at": _ago(600), "changed_by": "Grace Hopper", "source": "dashboard"},
            {"version": 2, "changed_at": _ago(120), "changed_by": "Ada Lovelace", "source": "dashboard"},
        ]},
        "reviewer": {"role": "reviewer", "available": True, "versions": [
            {"version": 1, "changed_at": _ago(300), "changed_by": "unknown", "source": "outside_dashboard"},
        ]},
    }

    def route_personas(route: Route) -> None:
        url = route.request.url
        if url.endswith("/history"):
            role = url.split("/api/personas/", 1)[1].split("/", 1)[0]
            body: Any = histories.get(role, {"role": role, "available": True, "versions": []})
        else:
            body = personas
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

    page.route("**/api/personas**", route_personas)
    page.goto(f"{live_server_url}/personas")
    implementer = page.locator('[data-persona-role="implementer"]')
    expect(implementer).to_contain_text("Last edited 2m ago by Ada Lovelace", timeout=_WAIT_LIVE)
    expect(implementer.locator("time")).to_have_attribute("title", _EXACT)
    expect(page.locator('[data-persona-role="reviewer"]')).to_contain_text("by unknown author")


def _feed_entries(count: int, start: int = 0) -> list[dict[str, Any]]:
    return [
        {"seq": 15700 + start + n, "timestamp": _ago(30 + n), "card_id": "PVTI_157F", "card_number": 157,
         "card_title": "Feed floor", "stage": "implementing", "activity_type": "progress",
         "text": f"floor entry {start + n}", "summary": "", "status": "", "truncated": False,
         "session_id": "", "performer_id": "", "is_delta": False, "stream_id": ""}
        for n in range(count)
    ]


@pytest.mark.e2e
def test_the_activity_feed_keeps_at_least_120_pixels_beside_a_short_board(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    page.set_viewport_size({"width": 1280, "height": 800})
    page.goto(live_server_url)
    expect(page.locator("#phase")).not_to_be_empty(timeout=_WAIT_SSE)
    store.broadcaster.broadcast(_full_snapshot(phase="idle", phase_label="Idle"))
    store.broadcaster.broadcast({"_event": "activity_event", "entries": _feed_entries(20)})
    expect(page.locator("#activity-feed .af-row").first).to_be_attached(timeout=_WAIT_LIVE)
    log = page.locator("#activity-feed")
    # The cap follows the board, but never drops below the floor, so the feed stays clickable.
    expect(log).to_have_attribute("style", re.compile(r"max-height: (1[2-9]\d|[2-9]\d\d)px"), timeout=_WAIT_LIVE)
    box = log.bounding_box()
    assert box is not None and box["height"] >= 119, box


@pytest.mark.e2e
def test_a_long_feed_scrolls_inside_its_own_box_and_not_the_page(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    page.set_viewport_size({"width": 1280, "height": 800})
    page.goto(live_server_url)
    expect(page.locator("#phase")).not_to_be_empty(timeout=_WAIT_SSE)
    store.broadcaster.broadcast(_full_snapshot(phase="monitoring_performer", phase_label="Monitoring Performer"))
    store.broadcaster.broadcast({"_event": "activity_event", "entries": _feed_entries(60, start=100)})
    expect(page.locator("#activity-feed .af-row").first).to_be_attached(timeout=_WAIT_LIVE)
    log = page.locator("#activity-feed")
    assert log.evaluate("el => getComputedStyle(el).overflowY") == "auto"
    assert log.evaluate("el => el.scrollHeight > el.clientHeight") is True
    before_page = page.evaluate("() => window.scrollY")
    log.evaluate("el => { el.scrollTop = el.scrollHeight; }")
    assert log.evaluate("el => el.scrollTop") > 0
    assert page.evaluate("() => window.scrollY") == before_page
