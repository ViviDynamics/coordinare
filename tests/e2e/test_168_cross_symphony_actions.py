"""#168: card actions on a card another symphony holds, in a real browser.

Cancel is offered only for the card the conductor is working on right now, and
the menu says why it is missing for any other card. Skip and restart are offered
for every card and send the card id, so the daemon finds the holding symphony.
Runs in Chromium and Firefox (--browser firefox).
"""

from __future__ import annotations

import json

import pytest
from playwright.sync_api import Page, Route, expect

from coordinare.dashboard import DashboardStore
from tests.e2e.test_dashboard_browser import _WAIT_LIVE, _WAIT_SSE, _active_session, _full_snapshot

_ACTIVE = "PVTI_168_ACTIVE"
_HELD = "PVTI_168_HELD"
_NOTE = "Cancel is offered only for the card the conductor is working on right now"
_LIFECYCLE = ["assessing", "implementing", "reviewing", "qa"]


def _broadcast(store: DashboardStore) -> None:
    active = {
        **_active_session(_ACTIVE, "Own symphony card", "implementing"),
        "issue_number": 67,
        "agent_dispatch_at": "2026-10-13T12:00:00+00:00",
        "cancel_available": True,
    }
    held = {
        **_active_session(_HELD, "Held by another symphony", "implementing"),
        "issue_number": 68,
        "agent_dispatch_at": "2026-10-13T12:00:00+00:00",
        "cancel_available": False,
    }
    store.broadcaster.broadcast(
        _full_snapshot(
            phase="monitoring_performer",
            phase_label="Watching an agent",
            active_sessions=[active, held],
            lifecycle_sequence=_LIFECYCLE,
        ),
    )


def _tile(page: Page, card_id: str):
    return page.locator(f'#active-performers .ap-tile[data-card-id="{card_id}"]')


def _open_menu(page: Page, card_id: str):
    tile = _tile(page, card_id)
    expect(tile.locator(".card-actions")).to_have_count(1, timeout=_WAIT_LIVE)
    tile.locator(".card-actions > summary").click()
    return tile


@pytest.mark.e2e
def test_a_card_another_symphony_holds_has_no_cancel_and_says_why(
    page: Page,
    live_server_url: str,
    store: DashboardStore,
) -> None:
    page.goto(live_server_url)
    expect(page.locator("#phase")).not_to_be_empty(timeout=_WAIT_SSE)
    _broadcast(store)

    tile = _open_menu(page, _HELD)
    menu = tile.locator(".card-actions-menu")
    expect(menu.get_by_role("button", name="Cancel card")).to_have_count(0)
    expect(menu).to_contain_text(_NOTE, timeout=_WAIT_LIVE)
    expect(menu.get_by_role("button", name="Skip current stage")).to_be_visible()
    expect(menu.get_by_role("button", name="Restart from stage")).to_be_visible()


@pytest.mark.e2e
def test_the_card_this_symphony_works_on_still_offers_cancel(
    page: Page,
    live_server_url: str,
    store: DashboardStore,
) -> None:
    page.goto(live_server_url)
    expect(page.locator("#phase")).not_to_be_empty(timeout=_WAIT_SSE)
    _broadcast(store)

    tile = _open_menu(page, _ACTIVE)
    menu = tile.locator(".card-actions-menu")
    expect(menu.get_by_role("button", name="Cancel card")).to_be_visible()
    expect(menu).not_to_contain_text(_NOTE)


@pytest.mark.e2e
def test_skip_on_a_held_card_sends_its_card_id(
    page: Page,
    live_server_url: str,
    store: DashboardStore,
) -> None:
    page.goto(live_server_url)
    expect(page.locator("#phase")).not_to_be_empty(timeout=_WAIT_SSE)
    _broadcast(store)

    requests: list[str] = []

    def fulfil(route: Route) -> None:
        requests.append(
            route.request.method + " " + route.request.url.split("://", 1)[1].split("/", 1)[1],
        )
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"status": "override_queued", "action": "skip"}),
        )

    page.route("**/api/skip-role**", fulfil)
    page.on("dialog", lambda dialog: dialog.accept())

    tile = _open_menu(page, _HELD)
    tile.locator(".card-actions-menu").get_by_role("button", name="Skip current stage").click()
    expect(tile.locator(".card-action-msg")).to_have_text(
        "Queued. It takes effect on the next check.",
        timeout=_WAIT_LIVE,
    )
    assert requests == [f"POST api/skip-role?card_id={_HELD}"]


@pytest.mark.e2e
def test_restart_on_a_held_card_sends_its_card_id(
    page: Page,
    live_server_url: str,
    store: DashboardStore,
) -> None:
    page.goto(live_server_url)
    expect(page.locator("#phase")).not_to_be_empty(timeout=_WAIT_SSE)
    _broadcast(store)

    requests: list[str] = []

    def fulfil(route: Route) -> None:
        requests.append(
            route.request.method + " " + route.request.url.split("://", 1)[1].split("/", 1)[1],
        )
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {"status": "override_queued", "action": "restart", "target_stage": "reviewing"},
            ),
        )

    page.route("**/api/restart-from/**", fulfil)
    page.on("dialog", lambda dialog: dialog.accept())

    tile = _open_menu(page, _HELD)
    menu = tile.locator(".card-actions-menu")
    menu.locator("select").select_option("reviewing")
    menu.get_by_role("button", name="Restart from stage").click()
    expect(tile.locator(".card-action-msg")).to_have_text(
        "Queued. It takes effect on the next check.",
        timeout=_WAIT_LIVE,
    )
    assert requests == [f"POST api/restart-from/reviewing?card_id={_HELD}"]


@pytest.mark.e2e
def test_an_unknown_card_refusal_is_shown_as_a_plain_message(
    page: Page,
    live_server_url: str,
    store: DashboardStore,
) -> None:
    page.goto(live_server_url)
    expect(page.locator("#phase")).not_to_be_empty(timeout=_WAIT_SSE)
    _broadcast(store)
    page.route(
        "**/api/skip-role**",
        lambda route: route.fulfill(
            status=400,
            content_type="application/json",
            body=json.dumps({"error": "No eligible active card for this target"}),
        ),
    )
    page.on("dialog", lambda dialog: dialog.accept())

    tile = _open_menu(page, _HELD)
    tile.locator(".card-actions-menu").get_by_role("button", name="Skip current stage").click()
    expect(tile.locator(".card-action-msg")).to_have_text(
        "No eligible active card for this target",
        timeout=_WAIT_LIVE,
    )


@pytest.mark.e2e
def test_declining_cancel_on_the_own_card_sends_nothing(
    page: Page,
    live_server_url: str,
    store: DashboardStore,
) -> None:
    page.goto(live_server_url)
    expect(page.locator("#phase")).not_to_be_empty(timeout=_WAIT_SSE)
    _broadcast(store)
    called: list[str] = []
    page.route("**/api/cancel**", lambda route: (called.append("cancel"), route.abort()))
    page.on("dialog", lambda dialog: dialog.dismiss())

    tile = _open_menu(page, _ACTIVE)
    tile.get_by_role("button", name="Cancel card").click()
    expect(tile.locator(".card-action-msg")).to_have_text("")
    assert called == []
