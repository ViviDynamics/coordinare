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


@pytest.mark.e2e
def test_result_survives_a_state_update_that_arrives_before_the_response(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    """Review of #145: the tiles are re-rendered on every state update, which
    used to detach the message node, so the operator never saw the outcome."""
    page.goto(live_server_url)
    expect(page.locator("#phase")).not_to_be_empty(timeout=_WAIT_SSE)
    _broadcast(store)
    held: list[Route] = []
    page.route("**/api/skip-role**", lambda route: held.append(route))
    page.on("dialog", lambda dialog: dialog.accept())

    tile = page.locator("#active-performers .ap-tile")
    tile.locator(".card-actions > summary").click(timeout=_WAIT_LIVE)
    tile.get_by_role("button", name="Skip current stage").click()
    expect(tile.locator(".card-action-msg")).to_have_text("Working…", timeout=_WAIT_LIVE)

    # A normal state update lands while the request is still in flight.
    _broadcast(store, card_tokens_total=1234)
    expect(page.locator("#card-tokens-total")).to_have_text("1,234", timeout=_WAIT_LIVE)
    assert held, "the request was never sent"
    held[0].fulfill(status=200, content_type="application/json",
                    body=json.dumps({"status": "override_queued", "action": "skip"}))

    message = page.locator("#active-performers .ap-tile .card-action-msg")
    expect(message).to_have_text("Queued. It takes effect on the next check.", timeout=_WAIT_LIVE)
    expect(page.locator("#active-performers .ap-tile .card-actions")).to_have_attribute("open", "")

    # And it is still there after another re-render.
    _broadcast(store, card_tokens_total=5678)
    expect(page.locator("#card-tokens-total")).to_have_text("5,678", timeout=_WAIT_LIVE)
    expect(page.locator("#active-performers .ap-tile .card-action-msg")).to_have_text(
        "Queued. It takes effect on the next check.",
    )


@pytest.mark.e2e
def test_retry_after_a_rerender_repeats_the_same_restart(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    page.goto(live_server_url)
    expect(page.locator("#phase")).not_to_be_empty(timeout=_WAIT_SSE)
    _broadcast(store)
    urls: list[str] = []
    answers = [
        (409, {"error": "A cycle is in progress", "status": "cycle_in_progress"}),
        (200, {"status": "override_queued", "action": "restart", "target_stage": "reviewing"}),
    ]

    def fulfil(route: Route) -> None:
        urls.append(route.request.url.split("/api/", 1)[1])
        status, body = answers[len(urls) - 1]
        route.fulfill(status=status, content_type="application/json", body=json.dumps(body))

    page.route("**/api/restart-from/**", fulfil)
    dialogs: list[str] = []
    page.on("dialog", lambda dialog: (dialogs.append(dialog.message), dialog.accept()))

    tile = page.locator("#active-performers .ap-tile")
    tile.locator(".card-actions > summary").click(timeout=_WAIT_LIVE)
    tile.locator(".card-action-restart select").select_option("reviewing")
    tile.get_by_role("button", name="Restart from stage").click()
    expect(tile.locator(".card-action-msg")).to_contain_text("A check is in progress", timeout=_WAIT_LIVE)

    _broadcast(store, card_tokens_total=42)          # re-render resets the select
    expect(page.locator("#card-tokens-total")).to_have_text("42", timeout=_WAIT_LIVE)
    page.locator("#active-performers .ap-tile .card-action-msg").get_by_role("button", name="Retry").click()
    expect(page.locator("#active-performers .ap-tile .card-action-msg")).to_have_text(
        "Queued. It takes effect on the next check.", timeout=_WAIT_LIVE,
    )
    assert urls == [f"restart-from/reviewing?card_id={_CARD}"] * 2
    assert len(dialogs) == 1  # the retry does not ask to confirm again
