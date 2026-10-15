"""#221: the symphony Configuration form has the Settings editors' safeguards.

The form is reviewed before a save (old and new per field, and only the reviewed snapshot is sent),
locked while the review is open, guarded against leaving with unsaved edits, and reapplies its edits
after a conflict. A 422 says why, and a secret override is typed into and never shown.

Most tests stub the symphony API so the page's requests are visible. The conflict, reapply and
secret tests run against the REAL dashboard server over a real config.yaml, as #143's review3 does.
Runs in Chromium and Firefox (--browser firefox).
"""
from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Generator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
import uvicorn
import yaml
from playwright.sync_api import Dialog, Page, Route, expect

from coordinare.config import CoordinareConfiguration
from coordinare.config_validation import coerce_multi_symphony_raw
from coordinare.dashboard import DashboardStore, create_dashboard_app
from tests.e2e.test_dashboard_browser import _full_snapshot

_WAIT = 5_000
_NAME = "demo"
_SECRET = "ghp_symphonysecret0000000000000000000"
_OVERRIDE = '[data-override-key="max_concurrent_cards"]'
_CONFIRM = "#sym-config-form [data-review] button"
_CONFIRM_TEXT = "Save these changes"


def _symphony(overrides: dict[str, Any] | None) -> dict[str, Any]:
    return {
        "name": _NAME, "github_project_number": 42, "enabled": True, "overrides": overrides or {},
        "secret_overrides": [], "personas": {}, "env_spec_files": ["README.md"],
        "env_bootstrap_performer_id": None, "env_cache": None, "state": None,
    }


def _stub(page: Page, overrides: dict[str, Any] | None, saved: list[Any], *, put_status: int = 200,
          put_body: dict[str, Any] | None = None) -> None:
    """Serve the symphony and record every save body. A refused save answers put_status with put_body."""

    def handle(route: Route) -> None:
        if route.request.method == "PUT":
            saved.append(json.loads(route.request.post_data or "{}"))
            if put_status != 200:
                route.fulfill(status=put_status, content_type="application/json", body=json.dumps(put_body or {}))
                return
            route.fulfill(status=200, content_type="application/json", headers={"ETag": '"v2"'},
                          body=json.dumps(_symphony(saved[-1].get("overrides"))))
            return
        route.fulfill(status=200, content_type="application/json", headers={"ETag": '"v1"'},
                      body=json.dumps(_symphony(overrides)))

    page.route(f"**/api/symphonies/{_NAME}", handle)


def _snapshot(store: DashboardStore) -> None:
    store.broadcaster.broadcast(_full_snapshot(symphonies=[{"name": _NAME, "source_project": 42, "enabled": True}]))


def _open(page: Page, url: str, store: DashboardStore) -> None:
    page.goto(url)
    _snapshot(store)
    expect(page.locator("#sym-save-btn")).to_be_visible(timeout=_WAIT)


def _review_and_confirm(page: Page) -> None:
    page.click("#sym-save-btn")
    page.locator(_CONFIRM, has_text=_CONFIRM_TEXT).click()


# --- review, then save sends the snapshot ----------------------------------------------------


@pytest.mark.e2e
def test_review_shows_old_and_new_then_save_sends_the_reviewed_snapshot(page: Page, live_server_url: str,
                                                                       store: DashboardStore) -> None:
    saved: list[Any] = []
    _stub(page, {"max_concurrent_cards": 2}, saved)
    _open(page, f"{live_server_url}/symphonies/{_NAME}", store)

    page.locator(_OVERRIDE).fill("3")
    page.click("#sym-save-btn")

    review = page.locator("#sym-config-form [data-review]")
    expect(review).to_be_visible(timeout=_WAIT)
    expect(review).to_contain_text("Max concurrent cards")
    expect(review).to_contain_text("Was: 2")
    expect(review).to_contain_text("Now: 3")
    assert saved == [], "nothing is sent before the reader confirms the review"
    page.locator(_CONFIRM, has_text=_CONFIRM_TEXT).click()

    expect(page.locator("#sym-save-msg")).to_have_text("Saved", timeout=_WAIT)
    assert saved[-1]["overrides"] == {"max_concurrent_cards": 3}


@pytest.mark.e2e
def test_editing_after_the_review_opens_is_not_sent(page: Page, live_server_url: str, store: DashboardStore) -> None:
    saved: list[Any] = []
    _stub(page, {"max_concurrent_cards": 2}, saved)
    _open(page, f"{live_server_url}/symphonies/{_NAME}", store)

    page.locator(_OVERRIDE).fill("3")
    page.click("#sym-save-btn")
    expect(page.locator("#sym-config-form [data-review]")).to_be_visible(timeout=_WAIT)
    # The form is locked while the review is open, so the field cannot be typed into.
    expect(page.locator(_OVERRIDE)).to_be_disabled()
    # A change made underneath the lock must not reach the request either.
    page.evaluate("() => { document.querySelector('[data-override-key=\"max_concurrent_cards\"]').value = '7'; }")
    page.locator(_CONFIRM, has_text=_CONFIRM_TEXT).click()

    expect(page.locator("#sym-save-msg")).to_have_text("Saved", timeout=_WAIT)
    assert saved[-1]["overrides"] == {"max_concurrent_cards": 3}


@pytest.mark.e2e
def test_keep_editing_unlocks_the_form_and_sends_nothing(page: Page, live_server_url: str, store: DashboardStore) -> None:
    saved: list[Any] = []
    _stub(page, {"max_concurrent_cards": 2}, saved)
    _open(page, f"{live_server_url}/symphonies/{_NAME}", store)

    page.locator(_OVERRIDE).fill("3")
    page.click("#sym-save-btn")
    page.locator("#sym-config-form [data-review] button", has_text="Keep editing").click()

    expect(page.locator("#sym-config-form [data-review]")).to_have_count(0)
    expect(page.locator(_OVERRIDE)).to_be_enabled()
    assert saved == []


@pytest.mark.e2e
def test_a_refused_save_shows_its_reason_in_plain_words(page: Page, live_server_url: str, store: DashboardStore) -> None:
    saved: list[Any] = []
    refusal = {"error": "Validation failed: Non-loopback dashboard_host requires dashboard_auth_token"}
    _stub(page, {"max_concurrent_cards": 2}, saved, put_status=422, put_body=refusal)
    _open(page, f"{live_server_url}/symphonies/{_NAME}", store)

    page.locator(_OVERRIDE).fill("3")
    _review_and_confirm(page)

    expect(page.locator("#sym-save-msg")).to_have_text(
        "Not saved: Non-loopback dashboard_host requires dashboard_auth_token. Nothing was changed.",
        timeout=_WAIT,
    )


# --- the unsaved-changes guard ---------------------------------------------------------------


@pytest.mark.e2e
def test_no_prompt_on_an_untouched_form_or_after_a_save(page: Page, live_server_url: str, store: DashboardStore) -> None:
    dialogs: list[str] = []

    def on_dialog(dialog: Dialog) -> None:
        dialogs.append(dialog.type)
        dialog.dismiss()

    page.on("dialog", on_dialog)
    saved: list[Any] = []
    _stub(page, {"max_concurrent_cards": 2}, saved)
    _open(page, f"{live_server_url}/symphonies/{_NAME}", store)

    page.locator("a", has_text="All symphonies").click()
    expect(page).to_have_url(f"{live_server_url}/symphonies", timeout=_WAIT)
    page.evaluate("() => navigate(null, '/symphonies/demo')")
    expect(page.locator("#sym-save-btn")).to_be_visible(timeout=_WAIT)
    assert dialogs == [], "an untouched form asks nothing"

    page.locator(_OVERRIDE).fill("3")
    _review_and_confirm(page)
    expect(page.locator("#sym-save-msg")).to_have_text("Saved", timeout=_WAIT)
    page.locator("a", has_text="All symphonies").click()
    expect(page).to_have_url(f"{live_server_url}/symphonies", timeout=_WAIT)
    assert dialogs == [], "a saved form asks nothing when it is left"


@pytest.mark.e2e
def test_an_in_app_link_asks_before_leaving_and_stays_when_declined(page: Page, live_server_url: str,
                                                                   store: DashboardStore) -> None:
    asked: list[str] = []

    def decline(dialog: Dialog) -> None:
        asked.append(dialog.type)
        dialog.dismiss()

    page.on("dialog", decline)
    _stub(page, {"max_concurrent_cards": 2}, [])
    _open(page, f"{live_server_url}/symphonies/{_NAME}", store)

    page.locator(_OVERRIDE).fill("4")
    page.locator("a", has_text="All symphonies").click()

    expect(page).to_have_url(f"{live_server_url}/symphonies/{_NAME}", timeout=_WAIT)
    expect(page.locator(_OVERRIDE)).to_have_value("4")
    assert asked == ["confirm"]


@pytest.mark.e2e
def test_leaving_with_the_edits_discarded_comes_back_to_the_saved_values(page: Page, live_server_url: str,
                                                                        store: DashboardStore) -> None:
    page.on("dialog", lambda dialog: dialog.accept())
    _stub(page, {"max_concurrent_cards": 2}, [])
    _open(page, f"{live_server_url}/symphonies/{_NAME}", store)

    page.locator(_OVERRIDE).fill("4")
    page.locator("a", has_text="All symphonies").click()
    expect(page).to_have_url(f"{live_server_url}/symphonies", timeout=_WAIT)
    page.evaluate("() => navigate(null, '/symphonies/demo')")

    expect(page.locator("#sym-save-btn")).to_be_visible(timeout=_WAIT)
    expect(page.locator(_OVERRIDE)).to_have_value("2")


@pytest.mark.e2e
@pytest.mark.parametrize("accept", [True, False], ids=["leave", "stay"])
def test_back_asks_before_leaving_and_keeps_the_edits_when_declined(page: Page, live_server_url: str,
                                                                   store: DashboardStore, accept: bool) -> None:
    def answer(dialog: Dialog) -> None:
        dialog.accept() if accept else dialog.dismiss()

    page.on("dialog", answer)
    _stub(page, {"max_concurrent_cards": 2}, [])
    page.goto(f"{live_server_url}/symphonies")
    _snapshot(store)
    page.evaluate("() => navigate(null, '/symphonies/demo')")
    expect(page.locator("#sym-save-btn")).to_be_visible(timeout=_WAIT)
    page.locator(_OVERRIDE).fill("4")

    page.go_back()

    if accept:
        expect(page).to_have_url(f"{live_server_url}/symphonies", timeout=_WAIT)
    else:
        expect(page).to_have_url(f"{live_server_url}/symphonies/{_NAME}", timeout=_WAIT)
        expect(page.locator(_OVERRIDE)).to_have_value("4")


@pytest.mark.e2e
def test_closing_the_page_with_unsaved_edits_asks_first(page: Page, live_server_url: str, store: DashboardStore) -> None:
    _stub(page, {"max_concurrent_cards": 2}, [])
    _open(page, f"{live_server_url}/symphonies/{_NAME}", store)

    untouched = page.evaluate("() => !window.dispatchEvent(new Event('beforeunload', {cancelable: true}))")
    page.locator(_OVERRIDE).fill("4")
    edited = page.evaluate("() => !window.dispatchEvent(new Event('beforeunload', {cancelable: true}))")

    assert untouched is False, "an untouched form lets the page close"
    assert edited is True, "unsaved edits cancel the close, so the browser asks"


# --- against the real server --------------------------------------------------------------


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def real_server(tmp_path: Path) -> Generator[dict[str, Any], None, None]:
    """The real dashboard over a real config.yaml, with one symphony that has a secret override."""
    raw = {
        "github_org": "acme",
        "github_token": "ghp_fixturetoken",
        "human_reviewers": ["alice"],
        "symphonies": [{"name": _NAME, "github_project_number": 100,
                        "overrides": {"max_concurrent_cards": 2, "github_token": _SECRET}}],
    }
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(raw, sort_keys=False))
    validated = CoordinareConfiguration(**coerce_multi_symphony_raw(raw))
    daemon = MagicMock()
    daemon.state = {
        "coordinare_config": validated,
        "config_version": 1,
        "symphony_configs": {s.name: s for s in validated.symphonies},
        "symphony_states": {},
    }
    daemon.state_store = MagicMock()
    daemon.state_store.last_snapshot = None
    daemon._cycle_active = False
    daemon.running = True
    store = DashboardStore()
    app = create_dashboard_app(store, daemon, MagicMock(), MagicMock(), config_path=config_path)
    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.05)
    yield {"url": f"http://127.0.0.1:{port}", "config": config_path, "store": store}
    server.should_exit = True
    thread.join(timeout=5)


def _open_real(page: Page, server: dict[str, Any]) -> None:
    page.goto(f"{server['url']}/symphonies/{_NAME}")
    _snapshot(server["store"])
    expect(page.locator("#sym-save-btn")).to_be_visible(timeout=_WAIT)


@pytest.mark.e2e
def test_a_stale_save_offers_reload_and_reapply_and_keeps_the_edit(page: Page, real_server: dict[str, Any]) -> None:
    _open_real(page, real_server)
    page.locator(_OVERRIDE).fill("5")
    # Someone else changes the file after this page loaded.
    config = real_server["config"]
    config.write_text(config.read_text() + "\n# edited elsewhere\n")

    _review_and_confirm(page)

    expect(page.locator("#sym-save-msg")).to_contain_text("Someone else changed", timeout=_WAIT)
    page.locator("#sym-save-msg button", has_text="Reload and reapply").click()
    expect(page.locator(_OVERRIDE)).to_have_value("5", timeout=_WAIT)
    expect(page.locator("#sym-save-msg")).to_contain_text("Your edits are back in the form", timeout=_WAIT)

    _review_and_confirm(page)
    expect(page.locator("#sym-save-msg")).to_have_text("Saved", timeout=_WAIT)
    stored = yaml.safe_load(config.read_text())["symphonies"][0]["overrides"]
    assert stored["max_concurrent_cards"] == 5


@pytest.mark.e2e
def test_a_secret_override_is_typed_into_and_never_shown(page: Page, real_server: dict[str, Any]) -> None:
    _open_real(page, real_server)
    secret = page.locator('[data-field="overrides.github_token"]')
    expect(secret).to_have_attribute("type", "password")
    assert secret.input_value() == ""
    assert _SECRET not in page.content()

    # A save of another setting keeps the stored secret, and still never shows it.
    page.locator(_OVERRIDE).fill("3")
    _review_and_confirm(page)
    expect(page.locator("#sym-save-msg")).to_have_text("Saved", timeout=_WAIT)

    assert _SECRET not in page.content()
    stored = yaml.safe_load(real_server["config"].read_text())["symphonies"][0]["overrides"]
    assert stored == {"max_concurrent_cards": 3, "github_token": _SECRET}
