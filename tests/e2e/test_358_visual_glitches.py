"""358: Dashboard visual glitches, checked in a real browser.

The status strip stays inside the page column with a long env bootstrap message, a
closed Metrics card is only as tall as its summary, and a board column's "+N more"
line never sits on top of a card.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from playwright.sync_api import Page, expect

from coordinare.dashboard import DashboardStore
from tests.e2e.test_139_symphony_switcher import _connected, _snapshot, _two_symphonies
from tests.e2e.test_154_layout import _board
from tests.e2e.test_266_bootstrap_visibility import _BOOT_BOARD, _boot, _symphony
from tests.e2e.test_dashboard_browser import _WAIT_LIVE, _WAIT_SSE, _full_snapshot

_LONG_REASON = (
    "verify.sh non-zero: bundle install failed while resolving the Gemfile.lock for the "
    "platform x86_64-linux because the lockfile was generated on arm64-darwin and the "
    "native extension for nokogiri could not be compiled against the system libxml2"
)
# A pixel of slack for sub-pixel rounding at the column edge.
_EDGE_SLACK_PX = 1


def _strip_geometry(page: Page) -> dict[str, Any]:
    return page.evaluate(
        """() => {
          const strip = document.getElementById('status-strip');
          const column = strip.parentElement.getBoundingClientRect();
          const box = strip.getBoundingClientRect();
          const button = document.getElementById('force-poll-btn').getBoundingClientRect();
          const boot = document.getElementById('status-bootstrap');
          return {columnRight: column.right, stripRight: box.right, stripScroll: strip.scrollWidth,
                  stripClient: strip.clientWidth, buttonRight: button.right,
                  bootWidth: boot.getBoundingClientRect().width, bootTitle: boot.title};
        }"""
    )


@pytest.mark.e2e
@pytest.mark.parametrize("width", [1280, 1600, 2000])
def test_the_status_strip_stays_in_the_column_with_a_long_bootstrap_message(
    page: Page, live_server_url: str, store: DashboardStore, width: int,
) -> None:
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(live_server_url)
    expect(page.locator("#phase")).not_to_be_empty(timeout=_WAIT_SSE)
    boot = _boot(state="failed", label="Failed, run 3 of 3", attempt=3, reason=_LONG_REASON)
    store.broadcaster.broadcast(_full_snapshot(phase="idle", phase_label="Idle", symphonies=[_symphony(_BOOT_BOARD, boot)]))
    expect(page.locator("#status-bootstrap")).to_contain_text("Env bootstrap: Failed", timeout=_WAIT_LIVE)
    expect(page.locator("#status-bootstrap")).to_be_visible()

    geometry = _strip_geometry(page)
    assert geometry["stripRight"] <= geometry["columnRight"] + _EDGE_SLACK_PX, geometry
    assert geometry["buttonRight"] <= geometry["stripRight"] + _EDGE_SLACK_PX, geometry
    assert geometry["stripScroll"] <= geometry["stripClient"] + _EDGE_SLACK_PX, geometry
    # The cut message keeps its full text on hover.
    assert _LONG_REASON in geometry["bootTitle"]


@pytest.mark.e2e
def test_a_closed_metrics_card_is_only_as_tall_as_its_summary(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    page.set_viewport_size({"width": 1600, "height": 900})
    page.goto(live_server_url)
    expect(page.locator("#phase")).not_to_be_empty(timeout=_WAIT_SSE)
    store.broadcaster.broadcast(_full_snapshot(phase="idle", phase_label="Idle"))
    expect(page.locator("#metrics-card")).to_be_visible(timeout=_WAIT_LIVE)

    heights = page.evaluate(
        """() => {
          const card = document.getElementById('metrics-card');
          card.open = false;
          const style = getComputedStyle(card);
          const chrome = parseFloat(style.paddingTop) + parseFloat(style.paddingBottom)
            + parseFloat(style.borderTopWidth) + parseFloat(style.borderBottomWidth);
          return {card: card.getBoundingClientRect().height,
                  summary: card.querySelector('summary').getBoundingClientRect().height + chrome,
                  health: document.getElementById('health-card').getBoundingClientRect().height};
        }"""
    )
    assert heights["card"] <= heights["summary"] + _EDGE_SLACK_PX, heights


def _crowded_board() -> dict[str, Any]:
    """A To Do column with 42 cards of mixed heights: one-line and two-line titles."""
    board = _board()
    ids = [f"PVTI_Q{i}" for i in range(42)]
    board["state"]["board_snapshot"]["TODO"] = ids
    board["state"]["board_titles"].update(
        {card: ("Tidy" if n % 3 else "Rework the poller so a long title wraps onto a second line") for n, card in enumerate(ids)},
    )
    board["state"]["board_issue_numbers"].update({card: 100 + n for n, card in enumerate(ids)})
    return board


@pytest.mark.e2e
@pytest.mark.parametrize("width", [1280, 1600, 2000])
def test_the_more_line_sits_below_whole_cards(
    page: Page, live_server_url: str, store: DashboardStore, width: int,
) -> None:
    page.set_viewport_size({"width": width, "height": 900})
    page.goto(live_server_url)
    expect(page.locator("#phase")).not_to_be_empty(timeout=_WAIT_SSE)
    store.broadcaster.broadcast(_full_snapshot(phase="idle", phase_label="Idle", symphonies=[_crowded_board()]))
    todo = page.locator("#swimlane-section .swimlane-col").filter(has_text="To Do")
    expect(todo.locator(".swimlane-more")).to_have_text(re.compile(r"^\+\d+ more$"), timeout=_WAIT_LIVE)

    geometry = todo.evaluate(
        """col => {
          const body = col.querySelector('.swimlane-col-body').getBoundingClientRect();
          const more = col.querySelector('.swimlane-more').getBoundingClientRect();
          const cards = [...col.querySelectorAll('.swimlane-col-body > .swimlane-card')]
            .map(c => c.getBoundingClientRect())
            .filter(r => r.bottom > body.top && r.top < body.bottom)
            .map(r => ({top: r.top, bottom: r.bottom}));
          return {body: {top: body.top, bottom: body.bottom}, moreTop: more.top, visible: cards,
                  text: col.querySelector('.swimlane-more').textContent,
                  total: col.querySelectorAll('.swimlane-col-body > .swimlane-card').length};
        }"""
    )
    assert geometry["moreTop"] >= geometry["body"]["bottom"] - _EDGE_SLACK_PX, geometry
    assert geometry["visible"], geometry
    for card in geometry["visible"]:
        assert card["bottom"] <= geometry["body"]["bottom"] + _EDGE_SLACK_PX, f"a card is cut at the foot: {geometry}"
    # The line counts every card not shown.
    assert geometry["text"] == f"+{geometry['total'] - len(geometry['visible'])} more", geometry


@pytest.mark.e2e
def test_the_scoped_dependency_note_is_one_compact_line(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    _connected(page, live_server_url + "/?symphony=beta-sym")
    store.broadcaster.broadcast(_snapshot(_two_symphonies(), blocked_by_dependencies=[
        {"issue_number": 5, "title": "Blocking issue", "column": "IN_PROGRESS", "issue_url": None},
    ]))
    card = page.locator("#dependency-blockers-card")
    expect(card).to_contain_text("Dependency blockers are only available in the all-symphonies view.", timeout=_WAIT_LIVE)
    expect(card).to_have_class(re.compile(r"\bscope-note-only\b"))
    heading, note = card.locator("h3").bounding_box(), card.locator("li").bounding_box()
    assert heading is not None and note is not None
    # The heading and the note share one line.
    assert abs(heading["y"] - note["y"]) < heading["height"], (heading, note)

    # Back to every symphony: the full list, and the panel is no longer compact.
    _connected(page, live_server_url + "/?symphony=")
    store.broadcaster.broadcast(_snapshot(_two_symphonies(), blocked_by_dependencies=[
        {"issue_number": 5, "title": "Blocking issue", "column": "IN_PROGRESS", "issue_url": None},
    ]))
    expect(card).to_contain_text("Blocking issue", timeout=_WAIT_LIVE)
    expect(card).not_to_have_class(re.compile(r"\bscope-note-only\b"))


@pytest.mark.e2e
def test_a_scoped_view_with_no_blockers_anywhere_has_no_dependency_panel(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    _connected(page, live_server_url + "/?symphony=beta-sym")
    store.broadcaster.broadcast(_snapshot(_two_symphonies(), blocked_by_dependencies=[]))
    expect(page.locator("#phase")).not_to_be_empty(timeout=_WAIT_LIVE)
    expect(page.locator("#dependency-blockers-card")).to_be_hidden(timeout=_WAIT_LIVE)
