"""#134: the snapshot view of usage: unknown prices, the pricing date, windows, and bounds."""
from __future__ import annotations

from datetime import date
from typing import Any

from coordinare.config import CostTrackingConfig
from coordinare.services.token_usage import (
    TokenLedger,
    disabled_usage_block,
    price_table_from_config,
    session_usage,
    usage_block,
    usage_summary,
)

OPUS: dict[str, float] = {
    "input_per_million": 15.0,
    "output_per_million": 75.0,
    "cache_read_per_million": 1.5,
    "cache_write_per_million": 18.75,
}


def _config(**extra: Any) -> CostTrackingConfig:
    return CostTrackingConfig.model_validate({
        "pricing_as_of": "2026-10-01",
        "prices": [{"models": ["claude-opus-4-5"], **OPUS}],
        **extra,
    })


def _record(seq: int, *, model: str | None, input_tokens: int, ts: str = "2026-10-14T10:00:00+00:00") -> dict[str, Any]:
    return {
        "ts": ts, "seq": seq, "type": "usage", "scope": "message",
        "usage": {"input_tokens": input_tokens, "output_tokens": 0, "cache_read": 0, "cache_write": 0, "model": model},
    }


def _ledger() -> TokenLedger:
    ledger = TokenLedger()
    ledger.observe("run-1", symphony="acme", card="PVTI_1", stage="implementing", records=[
        _record(1, model="claude-opus-4-5", input_tokens=1_000_000),
        _record(2, model="gpt-unpriced", input_tokens=500_000),
    ])
    return ledger


def test_a_summary_prices_what_it_can_and_names_what_it_cannot() -> None:
    table = price_table_from_config(_config())
    summary = usage_summary(_ledger().totals(run_id="run-1"), table)
    assert summary["tokens"]["input"] == 1_500_000
    assert summary["tokens"]["total"] == 1_500_000
    assert summary["cost"]["usd"] is None
    assert summary["cost"]["partial_usd"] == 15.0
    assert "gpt-unpriced" in summary["cost"]["unknown_reason"]
    unpriced = next(m for m in summary["models"] if m["model"] == "gpt-unpriced")
    assert unpriced["priced"] is False and unpriced["cost_usd"] is None
    assert unpriced["reason"] == "no price configured"


def test_a_fully_priced_summary_has_a_cost_and_no_reason() -> None:
    table = price_table_from_config(_config())
    priced_only = {"claude-opus-4-5": _ledger().totals(run_id="run-1")["claude-opus-4-5"]}
    exact = usage_summary(priced_only, table)
    assert exact["cost"]["usd"] == 15.0
    assert exact["cost"]["unknown_reason"] is None
    assert exact["cost"]["kind_usd"]["input_usd"] == 15.0


def test_the_summary_carries_the_pricing_date_and_its_source() -> None:
    summary = usage_summary(_ledger().totals(run_id="run-1"), price_table_from_config(_config()))
    assert summary["pricing"] == {"as_of": "2026-10-01", "source": "table"}


def test_no_pricing_says_so_instead_of_showing_zero() -> None:
    summary = usage_summary(_ledger().totals(run_id="run-1"), price_table_from_config(CostTrackingConfig()))
    assert summary["cost"]["usd"] is None
    assert summary["pricing"] == {"as_of": None, "source": "none"}
    assert "no price" in summary["cost"]["unknown_reason"]


def test_an_empty_total_is_zero_tokens_and_no_invented_cost() -> None:
    summary = usage_summary({}, price_table_from_config(_config()))
    assert summary["tokens"]["total"] == 0
    assert summary["cost"]["usd"] == 0.0
    assert summary["models"] == []


def test_the_block_has_today_and_a_seven_day_week_in_utc() -> None:
    ledger = TokenLedger()
    ledger.observe("old", symphony="acme", card="PVTI_1", stage="implementing", records=[
        _record(1, model="claude-opus-4-5", input_tokens=100, ts="2026-10-07T10:00:00+00:00"),
    ])
    ledger.observe("today", symphony="acme", card="PVTI_1", stage="implementing", records=[
        _record(1, model="claude-opus-4-5", input_tokens=10, ts="2026-10-14T01:00:00+00:00"),
    ])
    block = usage_block(ledger, price_table_from_config(_config()), today=date(2026, 10, 14))
    assert block["tracked"] is True
    assert block["day"] == "2026-10-14"
    assert block["today"]["tokens"]["input"] == 10
    assert block["week"]["tokens"]["input"] == 10  # 2026-10-07 is outside the seven UTC days ending today
    assert block["week_start"] == "2026-10-08"


def test_disabled_transcripts_say_not_tracked_and_never_show_a_cost() -> None:
    block = disabled_usage_block(reason="transcripts disabled")
    assert block["tracked"] is False
    assert block["reason"] == "transcripts disabled"
    assert block["today"] is None and block["week"] is None


def test_a_session_gets_run_and_card_summaries() -> None:
    table = price_table_from_config(_config())
    view = session_usage(_ledger(), table, run_id="run-1", card="PVTI_1")
    assert view["run"]["tokens"]["input"] == 1_500_000
    assert view["card"]["tokens"]["input"] == 1_500_000
    other = session_usage(_ledger(), table, run_id="run-missing", card="PVTI_missing")
    assert other["run"]["tokens"]["total"] == 0
    assert other["card"]["tokens"]["total"] == 0


def test_the_per_model_rows_are_bounded_by_the_models_seen() -> None:
    ledger = TokenLedger()
    ledger.observe("run-1", symphony="acme", card="PVTI_1", stage="implementing", records=[
        _record(seq, model=f"model-{seq}", input_tokens=1) for seq in range(1, 4)
    ])
    summary = usage_summary(ledger.totals(run_id="run-1"), price_table_from_config(_config()))
    assert len(summary["models"]) == 3
