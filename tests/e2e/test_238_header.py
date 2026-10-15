"""#238: the page header does not overlap at desktop widths.

Checked in a real browser at 1280, 1440 and 1920 px, with and without the symphony switcher, with the
sidebar open and collapsed, on the Dashboard and on the four other pages. The spend strip lives in the
Dashboard status strip (#154), so the header check includes it there. The spend wording is checked
here too: "$1.20 today · $8.40 week", and "Spend not priced" with the reason on hover when a cost is null.
Runs in Chromium and Firefox (--browser firefox).
"""
from __future__ import annotations

from typing import Any

import pytest
from playwright.sync_api import Page, expect

from coordinare.dashboard import DashboardStore
from tests.e2e.test_139_symphony_switcher import _open, _snapshot, _symphony
from tests.e2e.test_dashboard_browser import _WAIT_LIVE

_WIDTHS = (1280, 1440, 1920)
_PAGES = ("/", "/performers", "/history", "/symphonies", "/config")
_NAME = "conductor-e2e-sample"
_OTHER = "placeholder"
_VIEWPORT_HEIGHT = 900
_SIDEBAR_KEY = "coordinare.navigationCollapsed"
_OVERLAP_EPSILON_PX = 0.5
_SELECT_MIN_PX = 192
_TITLE_COLUMN_MIN_PX = 240
_ONE_LINE_RATIO = 1.6
_SPEND_OK = "$1.20 today · $8.40 week"
_REASON = "no price for gpt-x (no price configured)"
# The spend states the header matrix runs on the Dashboard only: the default figures, and neither period priced.
_SPENDS = ("default", "unpriced")
_MATRIX = [(path, spend) for path in _PAGES for spend in _SPENDS if path == "/" or spend == "default"]

# The header's visible boxes, and the Dashboard strip's spend and connection text, which sit just below it.
# Parents are left out so that a box is only ever compared with its siblings.
_HEADER_JS = """
() => {
  const selectors = ['h1#workspace-title', '#project-link label', '#project-link select', '#symphony-identity',
    '.appearance-control', '#account-menu', '#spend-strip', '#connection-status', '#phase', '#health-dot',
    '#force-poll-btn', '#navbar'];
  const items = [];
  for (const s of selectors) {
    for (const el of document.querySelectorAll(s)) {
      const cs = getComputedStyle(el);
      const r = el.getBoundingClientRect();
      if (cs.display === 'none' || cs.visibility === 'hidden' || r.width === 0 || r.height === 0) continue;
      items.push({name: s, text: (el.innerText || el.value || '').slice(0, 40),
        x0: r.left, y0: r.top, x1: r.right, y1: r.bottom});
    }
  }
  const h1 = document.querySelector('h1#workspace-title');
  const strip = document.getElementById('status-strip');
  return {
    items: items,
    titleFits: h1 ? h1.scrollWidth <= h1.clientWidth : null,
    stripFits: strip ? strip.scrollWidth <= strip.clientWidth + 1 : null,
    titleColumnMinWidth: parseFloat(getComputedStyle(document.querySelector('.workspace-header > div:first-child')).minWidth),
  };
}
"""

# The symphony select: its value, the selected name, and its computed minimum width.
_SELECT_JS = """
() => {
  const sel = document.getElementById('symphony-select');
  if (!sel) return null;
  const cs = getComputedStyle(sel);
  return {value: sel.value, text: sel.options[sel.selectedIndex].text, minWidth: parseFloat(cs.minWidth)};
}
"""


def _overlapping_pairs(items: list[dict[str, Any]]) -> list[str]:
    found = []
    for i, a in enumerate(items):
        for b in items[i + 1:]:
            across = min(a["x1"], b["x1"]) - max(a["x0"], b["x0"])
            down = min(a["y1"], b["y1"]) - max(a["y0"], b["y0"])
            if across > _OVERLAP_EPSILON_PX and down > _OVERLAP_EPSILON_PX:
                found.append(f"{a['name']} [{a['text']!r}] overlaps {b['name']} [{b['text']!r}]")
    return found


def _two_symphonies() -> list[dict[str, Any]]:
    return [
        _symphony(_NAME, 16, _NAME, todo=["PVTI_A1"], sessions=["PVTI_A1"], titles={"PVTI_A1": "Alpha card"}),
        _symphony(_OTHER, 2, _OTHER, blocked=["PVTI_B1"], sessions=["PVTI_B1"], titles={"PVTI_B1": "Beta card"}),
    ]


def _one_symphony() -> list[dict[str, Any]]:
    return [_symphony(_NAME, 16, _NAME, todo=["PVTI_S1"], titles={"PVTI_S1": "Solo card"})]


def _summary(usd: float | None, reason: str) -> dict[str, Any]:
    return {
        "tokens": {"input": 1000, "output": 1000, "cache_read": 0, "cache_write": 0, "total": 2000},
        "cost": {"usd": usd, "partial_usd": usd or 0.0, "kind_usd": {},
                 "unknown_reason": None if usd is not None else reason},
        "models": [], "pricing": {"as_of": "2026-10-01", "source": "table"},
    }


def _spend(*, today: float | None, week: float | None, reason: str = _REASON) -> dict[str, Any]:
    """The spend block. The chosen symphony's own figures are what the strip shows under the switcher."""
    today_figure = _summary(today, reason)
    week_figure = _summary(week, reason)
    return {
        "tracked": True, "reason": None, "day": "2026-10-14", "week_start": "2026-10-08",
        "today": today_figure, "week": week_figure,
        "by_symphony": {_NAME: {"today": today_figure, "week": week_figure}},
    }


def _sidebar(page: Page, collapsed: bool) -> None:
    value = "true" if collapsed else "false"
    page.add_init_script(f"window.localStorage.setItem('{_SIDEBAR_KEY}', '{value}')")


def _layout(page: Page, live_server_url: str, store: DashboardStore, *, width: int, switcher: bool,
            collapsed: bool, path: str, token_usage: dict[str, Any] | None = None) -> None:
    page.set_viewport_size({"width": width, "height": _VIEWPORT_HEIGHT})
    _sidebar(page, collapsed)
    url = live_server_url + path + (f"?symphony={_NAME}" if switcher else "")
    overrides: dict[str, Any] = {"token_usage": token_usage} if token_usage is not None else {}
    snapshot = _snapshot(_two_symphonies() if switcher else _one_symphony(), **overrides)
    if not switcher:
        snapshot["active_sessions"] = []
    _open(page, url, store, snapshot)
    expect(page.locator("#workspace-title")).not_to_be_empty(timeout=_WAIT_LIVE)


@pytest.mark.e2e
@pytest.mark.parametrize("width", _WIDTHS)
@pytest.mark.parametrize("switcher", [True, False], ids=["switcher", "no-switcher"])
@pytest.mark.parametrize("collapsed", [False, True], ids=["sidebar-open", "sidebar-collapsed"])
@pytest.mark.parametrize("path, spend", _MATRIX)
def test_the_header_does_not_overlap(
    page: Page, live_server_url: str, store: DashboardStore,
    width: int, switcher: bool, collapsed: bool, path: str, spend: str,
) -> None:
    token_usage = None if spend == "default" else _spend(today=None, week=None)
    _layout(page, live_server_url, store, width=width, switcher=switcher, collapsed=collapsed, path=path,
            token_usage=token_usage)
    if path == "/" and spend == "unpriced":
        # The worst case: the strip is at its widest only once the broadcast has landed, so wait for it first.
        expect(page.locator("#spend-strip")).to_have_text("Spend not priced", timeout=_WAIT_LIVE)

    state = page.evaluate(_HEADER_JS)
    assert _overlapping_pairs(state["items"]) == [], "header elements overlap"
    assert state["titleFits"] is True, "the page title is cut off"
    if path == "/":
        assert state["stripFits"] is True, "the Dashboard status strip overflows its row"
    if switcher:
        selected = page.evaluate(_SELECT_JS)
        assert selected is not None, "the symphony switcher is missing"
        assert selected["value"] == _NAME, "the select does not show the chosen symphony"
        assert selected["text"] == _NAME


@pytest.mark.e2e
def test_the_title_column_and_the_select_have_real_minimum_widths(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    _layout(page, live_server_url, store, width=1280, switcher=True, collapsed=False, path="/performers")

    state = page.evaluate(_HEADER_JS)
    assert state["titleColumnMinWidth"] >= _TITLE_COLUMN_MIN_PX, "the title column can shrink to nothing"
    selected = page.evaluate(_SELECT_JS)
    assert selected["minWidth"] >= _SELECT_MIN_PX, "the symphony select has no minimum width"


@pytest.mark.e2e
def test_the_spend_strip_stays_on_one_line_and_is_shortened(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    _layout(page, live_server_url, store, width=1280, switcher=True, collapsed=False, path="/",
            token_usage=_spend(today=1.2, week=8.4))

    strip = page.locator("#spend-strip")
    expect(strip).to_have_text(_SPEND_OK, timeout=_WAIT_LIVE)
    assert strip.evaluate("el => getComputedStyle(el).whiteSpace") == "nowrap"
    font_px = float(strip.evaluate("el => parseFloat(getComputedStyle(el).fontSize)"))
    height = float(strip.evaluate("el => el.getBoundingClientRect().height"))
    assert height <= font_px * _ONE_LINE_RATIO, "the spend strip wrapped onto a second line"
    title = strip.get_attribute("title") or ""
    assert "Days are UTC" in title
    assert "Pricing as of 2026-10-01." in title


@pytest.mark.e2e
def test_an_unpriced_spend_says_spend_not_priced_with_the_reason_on_hover(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    _layout(page, live_server_url, store, width=1440, switcher=True, collapsed=False, path="/",
            token_usage=_spend(today=None, week=None))

    strip = page.locator("#spend-strip")
    expect(strip).to_have_text("Spend not priced", timeout=_WAIT_LIVE)
    expect(strip).not_to_contain_text("unknown")
    title = strip.get_attribute("title") or ""
    assert _REASON in title, "the reason is not on hover"


@pytest.mark.e2e
def test_a_half_priced_spend_names_only_the_unpriced_day(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    _layout(page, live_server_url, store, width=1440, switcher=True, collapsed=False, path="/",
            token_usage=_spend(today=1.2, week=None))

    strip = page.locator("#spend-strip")
    expect(strip).to_have_text("$1.20 today · week not priced", timeout=_WAIT_LIVE)
    assert _REASON in (strip.get_attribute("title") or "")
