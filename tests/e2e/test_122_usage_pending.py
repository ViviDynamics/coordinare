"""#122: token and cost pending state in a real browser, with priced usage from #134."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from playwright.sync_api import Page, expect

from coordinare.config import CostTrackingConfig
from coordinare.dashboard import DashboardStore
from coordinare.services.token_usage import TokenLedger, price_table_from_config, session_usage
from tests.e2e.test_dashboard_browser import _WAIT_LIVE, _WAIT_SSE, _active_session, _full_snapshot

_RUN = "run-122"
_CARD = "PVTI_122"


def _priced_usage(tokens: int, *, model: str = "claude-opus-4-5") -> dict:
    """The session's usage summary as the daemon builds it: a real ledger, priced by a real table.

    Opus input at $3.00 per million, so ``tokens`` of input cost ``tokens / 1e6 * 3``.
    """
    ledger = TokenLedger()
    ledger.observe(_RUN, symphony="acme", card=_CARD, stage="implementing", records=[{
        "ts": datetime.now(UTC).isoformat(),
        "seq": 1,
        "type": "usage",
        "scope": "message",
        "usage": {"input_tokens": tokens, "output_tokens": 0, "cache_read": 0, "cache_write": 0, "model": model},
    }])
    table = price_table_from_config(CostTrackingConfig.model_validate({
        "pricing_as_of": "2026-10-01",
        "prices": [{
            "models": ["claude-opus-4-5"],
            "input_per_million": 3.0,
            "output_per_million": 15.0,
            "cache_read_per_million": 0.3,
            "cache_write_per_million": 3.75,
        }],
    }))
    return session_usage(ledger, table, run_id=_RUN, card=_CARD)


@pytest.mark.e2e
def test_running_stage_with_no_usage_shows_pending_then_the_priced_cost(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    page.goto(live_server_url)
    expect(page.locator("#phase")).not_to_be_empty(timeout=_WAIT_SSE)

    running = {**_active_session(_CARD, "Running card", "implementing"),
               "agent_dispatch_at": "2026-10-13T12:00:00+00:00"}
    store.broadcaster.broadcast(_full_snapshot(
        phase="monitoring_performer", phase_label="Monitoring Performer",
        active_sessions=[running],
    ))
    # 154: the tokens and cost are on the card's active-run tile.
    tokens = page.locator("#active-performer-tiles .ap-tile-tokens")
    cost = page.locator("#active-performer-tiles .ap-tile-cost")
    expect(tokens).to_have_text("—", timeout=_WAIT_LIVE)
    expect(tokens).to_have_attribute("title", "Reported when the stage ends")
    # 134: the pending placeholder is a plain hyphen; the value itself is unchanged.
    expect(cost).to_have_text("-")

    # A priced summary from the ledger: 2,000,000 input tokens at $3.00 per million.
    reported = {**running, "card_tokens_total": 2000000, "usage": _priced_usage(2_000_000)}
    store.broadcaster.broadcast(_full_snapshot(
        phase="monitoring_performer", phase_label="Monitoring Performer",
        active_sessions=[reported],
    ))
    expect(tokens).to_have_text((2000000).__format__(","), timeout=_WAIT_LIVE)
    expect(cost).to_have_text("$6.00")
    title = cost.locator("[title]").first.get_attribute("title") or ""
    assert "Pricing as of 2026-10-01." in title
    assert "claude-opus-4-5" in title

    # The run has ended: its tile goes with it.
    store.broadcaster.broadcast(_full_snapshot())
    expect(page.locator("#active-performer-tiles .ap-tile")).to_have_count(0, timeout=_WAIT_LIVE)


@pytest.mark.e2e
def test_a_model_with_no_price_shows_unknown_with_its_reason(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    page.goto(live_server_url)
    expect(page.locator("#phase")).not_to_be_empty(timeout=_WAIT_SSE)
    unpriced = {
        **_active_session(_CARD, "Running card", "implementing"),
        "agent_dispatch_at": "2026-10-13T12:00:00+00:00",
        "usage": _priced_usage(500_000, model="gpt-unpriced"),
    }
    store.broadcaster.broadcast(_full_snapshot(
        phase="monitoring_performer", phase_label="Monitoring Performer",
        active_sessions=[unpriced],
    ))
    cost = page.locator("#active-performer-tiles .ap-tile-cost")
    expect(cost).to_have_text("unknown", timeout=_WAIT_LIVE)
    assert "no price for gpt-unpriced" in (cost.locator("[title]").first.get_attribute("title") or "")


@pytest.mark.e2e
def test_transcripts_off_says_not_tracked_and_does_not_invent_a_cost(
    page: Page, live_server_url: str, store: DashboardStore,
) -> None:
    page.goto(live_server_url)
    expect(page.locator("#phase")).not_to_be_empty(timeout=_WAIT_SSE)
    untracked = {
        **_active_session(_CARD, "Running card", "implementing"),
        "agent_dispatch_at": "2026-10-13T12:00:00+00:00",
        "card_tokens_total": 2000000,
        "usage": {"tracked": False, "reason": "transcripts disabled", "run": None, "card": None},
    }
    store.broadcaster.broadcast(_full_snapshot(
        phase="monitoring_performer", phase_label="Monitoring Performer",
        active_sessions=[untracked],
    ))
    expect(page.locator("#active-performer-tiles .ap-tile-tokens")).to_have_text("2,000,000", timeout=_WAIT_LIVE)
    expect(page.locator("#active-performer-tiles .ap-tile-cost")).to_have_text("not tracked")
