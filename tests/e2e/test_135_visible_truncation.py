"""135: every clipped or dropped activity item says how much is hidden and links to its full transcript.

A clipped entry shows its line, and "Full transcript of" links the run's short name, which opens the viewer at the entry's run. A
group over the update cap says "Showing N of M". An uncut entry shows nothing. A lifecycle event
with no run says what was cut and has no link. Hostile text in a clipped entry stays text.
"""
from __future__ import annotations

import re
from typing import Any

import pytest
from playwright.sync_api import Page, expect

from coordinare.dashboard import DashboardStore
from tests.e2e.test_dashboard_browser import _WAIT_LIVE, _WAIT_SSE

pytestmark = [pytest.mark.e2e, pytest.mark.timeout(90)]

_CARD = "PVTI_135"
_NOTES = "#activity-feed .af-notes"


def _entry(seq: int, **extra: Any) -> dict[str, Any]:
    base = {
        "seq": seq, "timestamp": "2026-10-13T09:00:00+00:00", "card_id": _CARD, "card_number": 135,
        "card_title": "Visible truncation", "stage": "implementing", "activity_type": "progress",
        "text": f"entry {seq}", "summary": "", "status": "", "truncated": False,
        "text_length": None, "summary_length": None, "hidden_updates": None, "log_dropped": None,
        "session_id": "", "performer_id": "", "is_delta": False, "stream_id": "",
        "event_at": "",
    }
    base.update(extra)
    return base


def _open(page: Page, live_server_url: str) -> None:
    page.goto(live_server_url)
    expect(page.locator("#phase")).not_to_be_empty(timeout=_WAIT_SSE)


def _append(page: Page, entries: list[dict[str, Any]]) -> None:
    page.evaluate("entries => afAppend(entries)", entries)


def _row_with(page: Page, marker: str) -> Any:
    return page.locator("#activity-feed .af-row").filter(has_text=marker)


def test_a_clipped_entry_says_what_was_cut_and_opens_its_run(page: Page, live_server_url: str) -> None:
    _open(page, live_server_url)
    _append(page, [_entry(9501, text="a" * 200, truncated=True, text_length=1240,
                         session_id="run-135-a", performer_id="perf-135",
                         event_at="2026-10-13T09:00:05+00:00")])
    row = _row_with(page, "a" * 20)
    row.locator("summary").click()
    notes = row.locator(".af-notes")
    expect(notes).to_contain_text("text clipped at 200 characters (1,040 characters hidden)")
    expect(notes).to_contain_text("Full transcript of")
    link = notes.locator("a.run-entry-link")
    expect(link).to_have_attribute("href", re.compile(r"^/runs/run-135-a\?at="))
    expect(link).to_have_attribute("title", "run-135-a")
    link.click()
    # The viewer took the address: the run is in the URL, and the viewer's page is on screen.
    expect(page).to_have_url(re.compile(r"/runs/run-135-a\?at=2026-10-13T09%3A00%3A05"))
    expect(page.locator("#runs-notice")).to_be_attached()


def test_a_group_over_the_update_cap_says_showing_n_of_m(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    # Recorded before the page opens, so the page gets the whole stream in one backfill. A burst
    # sent while the page is open can be dropped by the broadcaster for a slow client (by design).
    for index in range(205):
        store.activity_log.record(
            activity_type="progress", card_id=_CARD, card_number=135, card_title="Visible truncation",
            stage="implementing", text=f"w{index} ", session_id="run-135-b", performer_id="perf-135",
            is_delta=True, stream_id="st-135-cap",
        )
    _open(page, live_server_url)
    # The newest group is this stream's, so it is the first row on the page.
    row = page.locator("#activity-feed .af-row").first
    row.locator("summary").click()
    expect(row.locator(".af-notes")).to_contain_text("Showing 200 of 205 updates")
    expect(row.locator(".af-notes")).to_contain_text("Full transcript of")
    expect(row.locator(".af-truncated")).to_have_count(1)


def test_a_marker_that_grows_redraws_its_count_in_place(page: Page, live_server_url: str) -> None:
    """A stream's marker is re-sent with a larger hidden count. The feed redraws the same row."""
    _open(page, live_server_url)
    deltas = [_entry(10000 + i, text=f"live {i} ", is_delta=True, stream_id="st-135-live",
                     session_id="run-135-f", performer_id="perf-135") for i in range(200)]
    marker = _entry(10300, activity_type="stream_truncated", text="stream st-135-live exceeded 200 updates",
                    is_delta=False, stream_id="st-135-live", session_id="run-135-f",
                    performer_id="perf-135", hidden_updates=1)
    _append(page, [*deltas, marker])
    # The module's store also holds the earlier cap stream, so the row is found by its own text.
    row = _row_with(page, "live ")
    row.locator("summary").click()
    expect(row.locator(".af-notes")).to_contain_text("Showing 200 of 201 updates")
    _append(page, [dict(marker, hidden_updates=5)])
    expect(row.locator(".af-notes")).to_contain_text("Showing 200 of 205 updates")
    expect(_row_with(page, "live ")).to_have_count(1)


def test_an_uncut_entry_shows_no_truncation_line(page: Page, live_server_url: str) -> None:
    _open(page, live_server_url)
    _append(page, [_entry(9601, text="uncut 135 line", text_length=14, session_id="run-135-c",
                         performer_id="perf-135")])
    row = _row_with(page, "uncut 135 line")
    row.locator("summary").click()
    expect(row.locator(".af-raw")).to_contain_text("uncut 135 line")
    expect(row.locator(".af-notes")).to_be_hidden()
    expect(row.locator(".af-notes")).to_be_empty()


def test_a_clipped_lifecycle_event_says_what_was_cut_with_no_link(page: Page, live_server_url: str) -> None:
    _open(page, live_server_url)
    _append(page, [_entry(9701, activity_type="stall", text=("lifecycle 135 " + "s" * 200)[:200],
                         truncated=True, text_length=240)])
    row = _row_with(page, "lifecycle 135")
    expect(row.locator(".af-notes")).to_contain_text(
        "text clipped at 200 characters (40 characters hidden)")
    # A lifecycle event has no run, so there is nothing to open, and the line says so by omission.
    expect(row.locator("a.run-entry-link")).to_have_count(0)


def test_hostile_clipped_text_stays_text(page: Page, live_server_url: str) -> None:
    _open(page, live_server_url)
    hostile = '<img src=x onerror="window.__pwned = 1"><b>bold</b> '
    full = hostile + "z" * 300
    _append(page, [_entry(9801, text=full[:200], truncated=True, text_length=len(full),
                         session_id="run-135-d", performer_id="perf-135")])
    row = _row_with(page, "onerror")
    row.locator("summary").click()
    expect(row.locator(".af-narration")).to_contain_text('<img src=x onerror="window.__pwned = 1">')
    expect(row.locator(".af-notes")).to_contain_text("characters hidden")
    assert page.locator("#activity-feed img").count() == 0
    assert page.locator("#activity-feed b").count() == 0
    assert page.evaluate("typeof window.__pwned") == "undefined"


def test_the_feed_says_how_many_older_entries_it_dropped(page: Page, live_server_url: str) -> None:
    _open(page, live_server_url)
    expect(page.locator("#af-dropped")).to_be_hidden()
    _append(page, [_entry(9901, text="dropped 135", session_id="run-135-e", performer_id="perf-135",
                         log_dropped=312)])
    expect(page.locator("#af-dropped")).to_contain_text("312 entries were dropped")
    expect(page.locator("#af-dropped")).to_be_visible()


def test_a_re_send_through_the_broadcaster_redraws_the_count_live(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    """Review 135, item 8: the re-sends travel the real broadcaster to an open page.

    The stream is recorded before the page opens, so the page's backfill starts at 1 hidden.
    Each later record goes out through broadcast_activity while the page is open. A count that
    is a power of two is sent at once, so the bursts below are deterministic.
    """
    session = "run-135-g"
    for index in range(201):
        store.activity_log.record(
            activity_type="progress", card_id=_CARD, card_number=135, card_title="Visible truncation",
            stage="implementing", text=f"live135-{index} ", session_id=session, performer_id="perf-135",
            is_delta=True, stream_id="st-135-live-broadcast",
        )
    _open(page, live_server_url)
    row = _row_with(page, "live135-")
    row.locator("summary").click()
    expect(row.locator(".af-notes")).to_contain_text("Showing 200 of 201 updates")
    store.activity_log.record(
        activity_type="progress", card_id=_CARD, card_number=135, card_title="Visible truncation",
        stage="implementing", text="live135-201 ", session_id=session, performer_id="perf-135",
        is_delta=True, stream_id="st-135-live-broadcast",
    )
    expect(row.locator(".af-notes")).to_contain_text("Showing 200 of 202 updates", timeout=_WAIT_LIVE)
    for index in (202, 203):
        store.activity_log.record(
            activity_type="progress", card_id=_CARD, card_number=135, card_title="Visible truncation",
            stage="implementing", text=f"live135-{index} ", session_id=session, performer_id="perf-135",
            is_delta=True, stream_id="st-135-live-broadcast",
        )
    # Hidden 3 waits for the schedule; hidden 4 is a power of two and is sent, carrying the count.
    expect(row.locator(".af-notes")).to_contain_text("Showing 200 of 204 updates", timeout=_WAIT_LIVE)
    # The re-sent count redrew in place: the same open group, not a second row.
    expect(row.locator("summary")).to_be_visible()
    expect(_row_with(page, "live135-")).to_have_count(1)


def test_a_re_send_for_an_unseen_marker_lands_in_seq_order(page: Page, live_server_url: str) -> None:
    """Review 135, item 5: a marker whose creation was lost is inserted where its seq belongs."""
    _open(page, live_server_url)
    _append(page, [_entry(9730, activity_type="stage_change", text="later 135")])
    _append(page, [_entry(9725, activity_type="stream_truncated", text="stream st-135-late exceeded 200 updates",
                         hidden_updates=4)])
    seqs = page.evaluate("_afEntries.map(function(e) { return e.seq; })")
    assert [s for s in seqs if s >= 9700] == [9725, 9730]
    expect(page.locator("#activity-feed")).to_contain_text("4 updates not shown")


def test_a_re_send_redraws_in_the_ungrouped_view(page: Page, live_server_url: str) -> None:
    """Review 135, item 5: with grouping off, a re-sent count still shows on screen."""
    _open(page, live_server_url)
    page.evaluate("_afGroupingActive = false; afRender();")
    _append(page, [_entry(9740, activity_type="stream_truncated", text="stream st-135-flat exceeded 200 updates",
                         hidden_updates=1)])
    expect(page.locator("#activity-feed")).to_contain_text("1 update not shown")
    _append(page, [_entry(9740, activity_type="stream_truncated", text="stream st-135-flat exceeded 200 updates",
                         hidden_updates=3)])
    expect(page.locator("#activity-feed")).to_contain_text("3 updates not shown")
    expect(page.locator("#activity-feed")).not_to_contain_text("1 update not shown")
