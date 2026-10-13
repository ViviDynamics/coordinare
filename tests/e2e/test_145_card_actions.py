"""#145: the card action menu in a real browser, with the control endpoints stubbed."""
from __future__ import annotations

import json

import pytest
from playwright.sync_api import Dialog, Page, Route, expect

from coordinare.dashboard import DashboardStore
from tests.e2e.test_dashboard_browser import _WAIT_LIVE, _WAIT_SSE, _active_session, _full_snapshot

_CARD = "PVTI_145"


def _broadcast(store: DashboardStore, **session_overrides) -> None:
    session = {
        **_active_session(_CARD, "Add login", "implementing"),
        "issue_number": 57, "agent_dispatch_at": "2026-10-13T12:00:00+00:00",
        **session_overrides,
    }
    store.broadcaster.broadcast(_full_snapshot(
        phase="monitoring_performer", phase_label="Watching an agent", active_sessions=[session],
        lifecycle_sequence=["assessing", "implementing", "reviewing", "qa"],
    ))


@pytest.mark.e2e
def test_skip_confirms_states_its_effect_and_calls_the_endpoint(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    page.goto(live_server_url)
    expect(page.locator("#phase")).not_to_be_empty(timeout=_WAIT_SSE)
    _broadcast(store)

    requests: list[str] = []

    def fulfil(route: Route) -> None:
        requests.append(route.request.method + " " + route.request.url.split("://", 1)[1].split("/", 1)[1])
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps({"status": "override_queued", "action": "skip"}))

    page.route("**/api/skip-role**", fulfil)
    dialogs: list[str] = []

    def accept(dialog: Dialog) -> None:
        dialogs.append(dialog.message)
        dialog.accept()

    page.on("dialog", accept)

    tile = page.locator("#active-performers .ap-tile")
    expect(tile.locator(".card-actions")).to_have_count(1, timeout=_WAIT_LIVE)
    tile.locator(".card-actions > summary").click()
    menu = tile.locator(".card-actions-menu")
    expect(menu.get_by_role("button", name="Cancel card")).to_be_visible()
    expect(menu.get_by_role("button", name="Restart from stage")).to_be_visible()
    expect(menu.locator("select option")).to_have_text(["Assess", "Implement", "Review", "QA"])

    menu.get_by_role("button", name="Skip current stage").click()
    expect(menu.locator(".card-action-msg")).to_have_text("Queued. It takes effect on the next check.", timeout=_WAIT_LIVE)
    assert dialogs == ["Skip the current stage? The card moves on to its next stage on the next check, "
                       "without this stage finishing."]
    assert requests == [f"POST api/skip-role?card_id={_CARD}"]


@pytest.mark.e2e
def test_declining_the_confirmation_sends_nothing(page: Page, live_server_url: str, store: DashboardStore) -> None:
    page.goto(live_server_url)
    expect(page.locator("#phase")).not_to_be_empty(timeout=_WAIT_SSE)
    _broadcast(store)
    called: list[str] = []
    page.route("**/api/cancel**", lambda route: (called.append("cancel"), route.abort()))
    page.on("dialog", lambda dialog: dialog.dismiss())

    tile = page.locator("#active-performers .ap-tile")
    tile.locator(".card-actions > summary").click(timeout=_WAIT_LIVE)
    tile.get_by_role("button", name="Cancel card").click()
    expect(tile.locator(".card-action-msg")).to_have_text("")
    assert called == []


@pytest.mark.e2e
def test_a_check_in_progress_offers_a_retry(page: Page, live_server_url: str, store: DashboardStore) -> None:
    page.goto(live_server_url)
    expect(page.locator("#phase")).not_to_be_empty(timeout=_WAIT_SSE)
    _broadcast(store)
    page.route("**/api/cancel**", lambda route: route.fulfill(
        status=409, content_type="application/json",
        body=json.dumps({"error": "A cycle is in progress", "status": "cycle_in_progress"})))
    page.on("dialog", lambda dialog: dialog.accept())

    tile = page.locator("#active-performers .ap-tile")
    tile.locator(".card-actions > summary").click(timeout=_WAIT_LIVE)
    tile.get_by_role("button", name="Cancel card").click()
    message = tile.locator(".card-action-msg")
    expect(message).to_contain_text("A check is in progress. Try again in a moment.", timeout=_WAIT_LIVE)
    expect(message.get_by_role("button", name="Retry")).to_be_visible()


@pytest.mark.e2e
def test_a_queued_control_shows_as_a_pending_chip(page: Page, live_server_url: str, store: DashboardStore) -> None:
    page.goto(live_server_url)
    expect(page.locator("#phase")).not_to_be_empty(timeout=_WAIT_SSE)
    _broadcast(store, pending_control={"action": "restart", "target_stage": "reviewing"})
    expect(page.locator("#active-performers .card-action-pending")).to_have_text(
        "Restart from Review pending", timeout=_WAIT_LIVE,
    )
