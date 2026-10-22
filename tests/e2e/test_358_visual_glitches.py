"""358: Dashboard visual glitches, checked in a real browser.

The status strip stays inside the page column with a long env bootstrap message, a
closed Metrics card is only as tall as its summary, and a board column's "+N more"
line never sits on top of a card.
"""

from __future__ import annotations

from typing import Any

import pytest
from playwright.sync_api import Page, expect

from coordinare.dashboard import DashboardStore
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
