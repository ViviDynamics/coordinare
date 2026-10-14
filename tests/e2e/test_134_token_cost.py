"""#134: token and cost figures, their tooltips, and the spend strip, in a real browser."""
from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from coordinare.dashboard import DashboardStore
from tests.e2e.test_dashboard_browser import _WAIT_LIVE, _WAIT_SSE, _active_session, _full_snapshot

_CARD = "PVTI_134"
_OPUS = "claude-opus-4-5"

_SUMMARY_TODAY = {
    "tokens": {"input": 1050, "output": 1000, "cache_read": 17000, "cache_write": 300, "total": 19350},
    "cost": {
        "usd": 0.125625,
        "partial_usd": 0.125625,
        "kind_usd": {"input_usd": 0.01775, "output_usd": 0.0755, "cache_read_usd": 0.0255, "cache_write_usd": 0.006875},
        "unknown_reason": None,
    },
    "models": [
        {
            "model": _OPUS,
            "tokens": {"input": 1050, "output": 1000, "cache_read": 17000, "cache_write": 300, "total": 19350},
            "priced": True,
            "price": {"input_per_million": 15.0, "output_per_million": 75.0,
                      "cache_read_per_million": 1.5, "cache_write_per_million": 18.75},
            "cost_usd": 0.121875,
            "reason": None,
        },
    ],
    "pricing": {"as_of": "2026-10-01", "source": "table"},
}

_SUMMARY_UNKNOWN = {
    "tokens": {"input": 500000, "output": 0, "cache_read": 0, "cache_write": 0, "total": 500000},
    "cost": {"usd": None, "partial_usd": 0.0, "kind_usd": {}, "unknown_reason": "no price for gpt-unpriced (no price configured)"},
    "models": [
        {
            "model": "gpt-unpriced",
            "tokens": {"input": 500000, "output": 0, "cache_read": 0, "cache_write": 0, "total": 500000},
            "priced": False, "price": None, "cost_usd": None, "reason": "no price configured",
        },
    ],
    "pricing": {"as_of": "2026-10-01", "source": "table"},
}


def _block(*, today: dict | None, week: dict | None) -> dict:
    return {
        "tracked": True, "reason": None, "day": "2026-10-14", "week_start": "2026-10-08",
        "today": today, "week": week, "pricing": {"as_of": "2026-10-01", "source": "table"},
    }


def _usage(card: dict, run: dict | None = None) -> dict:
    return {"tracked": True, "run": run or card, "card": card}


_ROLES = [{"role": "implementing", "active": 1, "max": 2, "queued": 0}]


def _running(session: dict) -> dict:
    """A session that has been dispatched, so its cost cell is priced rather than a placeholder."""
    return {**session, "agent_dispatch_at": "2026-10-14T09:00:00+00:00"}


def _broadcast(store: DashboardStore, *, token_usage: dict, sessions: list[dict], stage: str = "implementing") -> None:
    store.broadcaster.broadcast(_full_snapshot(
        phase="monitoring_performer",
        phase_label="Watching an agent",
        performer_stage=stage,
        token_usage=token_usage,
        active_sessions=sessions,
        role_utilization=_ROLES,
        lifecycle_sequence=["assessing", "implementing", "reviewing", "qa"],
    ))


def _open(page: Page, live_server_url: str, store: DashboardStore, *, token_usage: dict, sessions: list[dict]) -> None:
    page.goto(live_server_url)
    expect(page.locator("#phase")).not_to_be_empty(timeout=_WAIT_SSE)
    _broadcast(store, token_usage=token_usage, sessions=sessions)


@pytest.mark.e2e
def test_the_header_strip_shows_spend_today_and_this_week(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    block = _block(today=_SUMMARY_TODAY, week=_SUMMARY_TODAY)
    _open(page, live_server_url, store, token_usage=block, sessions=[])
    strip = page.locator("#spend-strip")
    expect(strip).to_have_text("Spend today $0.13 · this week $0.13", timeout=_WAIT_LIVE)
    title = strip.get_attribute("title") or ""
    assert "Days are UTC" in title
    assert "Pricing as of 2026-10-01." in title


@pytest.mark.e2e
def test_an_unknown_price_says_unknown_not_a_number(page: Page, live_server_url: str, store: DashboardStore) -> None:
    block = _block(today=_SUMMARY_UNKNOWN, week=_SUMMARY_UNKNOWN)
    _open(page, live_server_url, store, token_usage=block, sessions=[])
    expect(page.locator("#spend-strip")).to_have_text("Spend today unknown · this week unknown", timeout=_WAIT_LIVE)
    expect(page.locator("#spend-today")).to_have_text("unknown", timeout=_WAIT_LIVE)


@pytest.mark.e2e
def test_transcripts_off_says_not_tracked_and_never_zero(page: Page, live_server_url: str, store: DashboardStore) -> None:
    block = {"tracked": False, "reason": "transcripts disabled", "day": None, "week_start": None, "today": None, "week": None}
    _open(page, live_server_url, store, token_usage=block, sessions=[])
    expect(page.locator("#spend-strip")).to_have_text("Spend not tracked", timeout=_WAIT_LIVE)
    expect(page.locator("#spend-week")).to_have_text("not tracked", timeout=_WAIT_LIVE)
    expect(page.locator("#spend-strip")).not_to_contain_text("$0.00")


@pytest.mark.e2e
def test_the_run_header_shows_tokens_with_the_breakdown_on_hover(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    session = _running(_active_session(_CARD, "Add metering", "implementing", issue_number=134, usage=_usage(_SUMMARY_TODAY)))
    page.goto(live_server_url + "/performers/implementing")
    expect(page.locator("#phase")).not_to_be_empty(timeout=_WAIT_SSE)
    _broadcast(store, token_usage=_block(today=_SUMMARY_TODAY, week=_SUMMARY_TODAY), sessions=[session])
    detail = page.locator("#performers-page-detail")
    expect(detail).to_contain_text("19,350 tokens", timeout=_WAIT_LIVE)
    tokens = detail.locator("span[title*='Tokens: input']").first
    expect(tokens).to_contain_text("19,350 tokens")
    title = tokens.get_attribute("title") or ""
    assert "input 1,050" in title
    assert "cache read 17,000" in title
    assert "cache write 300" in title
    assert "Pricing as of 2026-10-01." in title


@pytest.mark.e2e
def test_the_board_card_cost_carries_its_breakdown(page: Page, live_server_url: str, store: DashboardStore) -> None:
    session = _running(_active_session(_CARD, "Add metering", "implementing", issue_number=134, usage=_usage(_SUMMARY_TODAY)))
    _open(page, live_server_url, store, token_usage=_block(today=_SUMMARY_TODAY, week=_SUMMARY_TODAY), sessions=[session])
    card = page.locator("#swimlane-section .swimlane-card").filter(has_text="Add metering")
    expect(card).to_contain_text("$0.13", timeout=_WAIT_LIVE)
    cost = card.locator("span[title*='Cost:']").first
    expect(cost).to_have_text("$0.13")
    assert "Pricing as of 2026-10-01." in (cost.get_attribute("title") or "")


@pytest.mark.e2e
def test_a_model_name_is_shown_as_text_never_as_markup(page: Page, live_server_url: str, store: DashboardStore) -> None:
    hostile = {
        **_SUMMARY_UNKNOWN,
        "models": [{**_SUMMARY_UNKNOWN["models"][0], "model": "<img src=x onerror=window.__pwned=1>"}],
    }
    session = _running(_active_session(_CARD, "Add metering", "implementing", issue_number=134, usage=_usage(hostile)))
    _open(page, live_server_url, store, token_usage=_block(today=_SUMMARY_UNKNOWN, week=_SUMMARY_UNKNOWN), sessions=[session])
    assert page.locator("img[src='x']").count() == 0
    assert page.evaluate("window.__pwned === undefined") is True
