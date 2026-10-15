"""#101: the Symphonies table says when a branch needs a review coordinare cannot give. Checked in a browser.

The condition is a symphony-level reason in the Status column, so an operator sees it on the page
where the symphony is listed. A symphony whose branch needs no such review says nothing about it.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from coordinare.dashboard import DashboardStore
from tests.e2e.test_156_symphonies_table import _STATUS, _TABLE, _cell, _row
from tests.e2e.test_dashboard_browser import _WAIT_LIVE, _full_snapshot

_EXTERNAL = (
    "Merges need an approving review from someone other than vivi-coordinare. "
    "Add a human approver to the loop or relax the rule."
)
# This file sorts first in the e2e session, so its first page load pays the live server's and the
# browser's cold start; the shared SSE wait (3 s) is sized for a warm session.
_WAIT_FIRST_CONNECT = 15_000  # ms

_UNREADABLE = (
    "could not read branch protection on main, so coordinare cannot tell whether merges need "
    "a review it cannot give. Check the token's repository permissions."
)


def _open_with(page: Page, live_server_url: str, store: DashboardStore, rows: list[dict]) -> None:
    page.goto(live_server_url + "/symphonies")
    expect(page.locator("#connection-label")).to_have_text("Live", timeout=_WAIT_FIRST_CONNECT)
    store.broadcaster.broadcast(_full_snapshot(phase="idle", phase_label="Idle", symphonies=rows))
    expect(page.locator(_TABLE)).to_contain_text(rows[0]["name"], timeout=_WAIT_LIVE)


@pytest.mark.e2e
def test_a_branch_that_needs_an_outside_approver_is_named_in_the_status(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    rows = [
        _row("garden", merge_review={"state": "external_review_required", "message": _EXTERNAL}),
        _row("clear-sym", merge_review={"state": "clear", "message": "main needs no approving review."}),
    ]
    _open_with(page, live_server_url, store, rows)

    expect(_cell(page, "garden", _STATUS)).to_contain_text(_EXTERNAL)
    expect(_cell(page, "clear-sym", _STATUS)).not_to_contain_text("someone other than")


@pytest.mark.e2e
def test_an_unreadable_branch_protection_is_said_in_plain_words(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    rows = [_row("garden", merge_review={"state": "unreadable", "message": _UNREADABLE})]
    _open_with(page, live_server_url, store, rows)

    expect(_cell(page, "garden", _STATUS)).to_contain_text("could not read branch protection on main")


@pytest.mark.e2e
def test_the_condition_is_still_shown_on_a_paused_and_on_a_failed_symphony(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    rows = [
        _row("paused-sym", cards_paused=1, merge_review={"state": "external_review_required", "message": _EXTERNAL}),
        _row(
            "failed-sym",
            error_count=2,
            state={"last_error": "GitHub 401 Bad credentials", "board_snapshot": None, "active_sessions": {}},
            merge_review={"state": "external_review_required", "message": _EXTERNAL},
        ),
    ]
    _open_with(page, live_server_url, store, rows)

    expect(_cell(page, "paused-sym", _STATUS)).to_contain_text("1 card is paused on the board.")
    expect(_cell(page, "paused-sym", _STATUS)).to_contain_text(_EXTERNAL)
    expect(_cell(page, "failed-sym", _STATUS)).to_contain_text("Last check failed: GitHub 401 Bad credentials")
    expect(_cell(page, "failed-sym", _STATUS)).to_contain_text(_EXTERNAL)
