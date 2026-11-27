"""#365 (d), (e) and the Settings "Saved" message, in a real browser with the config API stubbed.

The approver field names its rule, the logins field shows the list it reads as and flags an
entry that is not a GitHub login before any save, and a save's "Saved" survives the refresh
that follows it. Runs in Chromium and Firefox.
"""
from __future__ import annotations

from typing import Any

import pytest
from playwright.sync_api import Page, expect

from tests.e2e.test_143_settings import _open_settings, _setting, _snapshot, _stub_config

_WAIT = 5000
_HELP = ("Only these GitHub logins can approve a merge, in every symphony that does not set its own "
         "list. An approval from any other login shows on the work item and does not merge.")


def _with_reviewers(humans: list[str]) -> dict[str, Any]:
    snapshot = _snapshot()
    general = snapshot["sections"][0]
    general["settings"].append(_setting(
        "global.human_reviewers", "Who can approve merges (human_reviewers)", "list", humans, help=_HELP,
    ))
    return snapshot


def _field(page: Page) -> Any:
    return page.locator('#config-page-section [data-field="human_reviewers"]')


@pytest.mark.e2e
def test_the_approver_field_says_who_can_approve(page: Page, live_server_url: str) -> None:
    _stub_config(page, _with_reviewers(["alice"]), [])
    _open_settings(page, live_server_url)
    row = page.locator("#config-page-section [data-fieldrow]", has=page.locator('[data-field="human_reviewers"]'))
    expect(row).to_contain_text("Who can approve merges (human_reviewers)")
    expect(row).to_contain_text(_HELP)


@pytest.mark.e2e
def test_the_logins_field_shows_the_list_it_reads_as_while_typing(page: Page, live_server_url: str) -> None:
    _stub_config(page, _with_reviewers(["alice"]), [])
    _open_settings(page, live_server_url)
    preview = page.locator("#config-page-section [data-logins-preview]")
    expect(preview).to_contain_text("Reads as 1 login: alice")
    _field(page).fill("alice,  bob carol")
    expect(preview).to_contain_text("Reads as 3 logins:")
    expect(preview.locator(".cfg-login-chip")).to_have_text(["alice", "bob", "carol"])
    _field(page).fill("alice @bob")
    expect(preview).to_contain_text("Not a GitHub login: @bob")


@pytest.mark.e2e
def test_a_flagged_login_is_refused_before_any_save(page: Page, live_server_url: str) -> None:
    saves: list[dict[str, Any]] = []
    _stub_config(page, _with_reviewers(["alice"]), saves)
    _open_settings(page, live_server_url)
    _field(page).fill("alice, al.ice")
    page.locator('#config-page-section [data-cfg-action="save-section"]').first.click()
    expect(page.locator("#config-page-section [data-review]")).to_have_count(0)
    row = page.locator("#config-page-section [data-fieldrow]", has=page.locator('[data-field="human_reviewers"]'))
    expect(row.locator("[data-err]")).to_contain_text("Not a GitHub login: al.ice")
    assert saves == []


@pytest.mark.e2e
def test_spaces_save_as_a_list_and_saved_survives_the_refresh(page: Page, live_server_url: str) -> None:
    saves: list[dict[str, Any]] = []
    _stub_config(page, _with_reviewers(["alice"]), saves)
    _open_settings(page, live_server_url)
    _field(page).fill("alice bob")
    page.locator('#config-page-section [data-cfg-action="save-section"]').first.click()
    page.locator('#config-page-section [data-review] button', has_text="Save these changes").click()
    status = page.locator('#config-page-section [data-section="global"] [data-status]')
    expect(status).to_have_text("Saved · applied live", timeout=_WAIT)
    assert saves[-1]["body"]["changes"]["human_reviewers"] == ["alice", "bob"]
    # The page is drawn again a second after a save. Mark the old section so the wait ends
    # exactly when a new one is in the page, then check the message came with it.
    page.locator('#config-page-section [data-section="global"]').evaluate("el => el.setAttribute('data-old', '')")
    expect(page.locator('#config-page-section [data-section="global"]:not([data-old])')).to_have_count(1, timeout=_WAIT)
    expect(status).to_have_text("Saved · applied live")


# --- (d) the symphony page says who can approve ------------------------------------------------


@pytest.mark.e2e
@pytest.mark.parametrize("own", [None, ["carol", "dan"]])
@pytest.mark.parametrize("where", ["settings form", "symphony page"])
def test_the_symphony_settings_say_who_can_approve_and_link_to_it(
    page: Page, live_server_url: str, store: Any, own: list[str] | None, where: str,
) -> None:
    from tests.e2e.test_221_symphony_editor import _NAME, _open, _snapshot, _stub

    _stub(page, {"human_reviewers": own} if own else None, [])
    if where == "settings form":
        _open(page, f"{live_server_url}/config#symphonies/{_NAME}", store)
        note = page.locator("#sym-config-form [data-approver-note]")
    else:
        page.goto(f"{live_server_url}/symphonies/{_NAME}")
        _snapshot(store)
        note = page.locator("[data-sym-summary-view] [data-approver-note]")
    expect(note).to_be_visible(timeout=_WAIT)
    if not own:
        expect(note).to_contain_text("Only the logins in Who can approve merges can approve a merge.")
    expect(note).to_contain_text("An approval from any other login shows on the work item and does not merge.")
    if own:
        expect(note).to_contain_text("This symphony sets its own list of who can approve merges: carol, dan.")
        expect(note).not_to_contain_text("Only the logins in Who can approve merges")
    else:
        expect(note).not_to_contain_text("its own list")
    note.locator("a").click()
    expect(page).to_have_url(f"{live_server_url}/config#global", timeout=_WAIT)
    expect(page.locator("#config-page")).to_be_visible(timeout=_WAIT)
