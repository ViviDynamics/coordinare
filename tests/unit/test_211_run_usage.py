"""#211: the usage block of one run, taken from the #134 ledger (the store helper, not the route)."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

from coordinare.config import CostTrackingConfig
from coordinare.dashboard.store import run_usage_block
from coordinare.services.run_transcript import TranscriptCollector
from coordinare.services.token_usage import price_table_from_config, usage_summary

SYMPHONY = "acme"
CARD = "PVTI_211"
RUN = "run-211"
OTHER_RUN = "run-212"
OPUS = "claude-opus-4-5"
UNPRICED = "gpt-unpriced"
AS_OF = "2026-10-01"
ONE_MILLION = 1_000_000
OPUS_INPUT_USD = 15.0
TS = "2026-10-14T10:00:00+00:00"
NO_USAGE_REASON = "no usage records in this run"


def _cost_tracking() -> CostTrackingConfig:
    return CostTrackingConfig.model_validate({
        "pricing_as_of": AS_OF,
        "prices": [{"models": [OPUS], "input_per_million": OPUS_INPUT_USD, "output_per_million": 75.0,
                    "cache_read_per_million": 1.5, "cache_write_per_million": 18.75}],
    })


def _usage(seq: int, *, model: str, input_tokens: int) -> dict[str, Any]:
    return {
        "ts": TS, "seq": seq, "type": "usage", "scope": "message",
        "usage": {"input_tokens": input_tokens, "output_tokens": 0, "cache_read": 0, "cache_write": 0,
                  "model": model},
    }


def _daemon(root: Path | None, *, enabled: bool = True, priced: bool = True) -> MagicMock:
    daemon = MagicMock()
    state: dict[str, Any] = {"phase": "idle"}
    if root is not None:
        state["transcript_collector"] = TranscriptCollector(
            root, enabled=enabled, max_bytes_per_run=10**9, retention_days=0,
        )
    if priced:
        state["config"] = SimpleNamespace(cost_tracking=_cost_tracking())
    daemon.state = state
    return daemon


def _observe(daemon: MagicMock, run_id: str, records: list[dict[str, Any]]) -> None:
    collector = daemon.state["transcript_collector"]
    collector.usage.observe(run_id, symphony=SYMPHONY, card=CARD, stage="implementing", records=records)


def test_a_run_the_ledger_holds_gets_its_figures_and_price(tmp_path: Path) -> None:
    daemon = _daemon(tmp_path)
    _observe(daemon, RUN, [_usage(1, model=OPUS, input_tokens=ONE_MILLION)])
    block = run_usage_block(daemon, run_id=RUN)
    assert block["tracked"] is True
    assert block["reason"] is None
    assert block["tokens"]["input"] == ONE_MILLION
    assert block["cost"]["usd"] == OPUS_INPUT_USD
    assert block["cost"]["unknown_reason"] is None
    model_row = block["models"][0]
    assert model_row["model"] == OPUS
    assert model_row["price"]["input_per_million"] == OPUS_INPUT_USD
    assert model_row["cost_usd"] == OPUS_INPUT_USD
    assert block["pricing"] == {"as_of": AS_OF, "source": "table"}


def test_the_block_is_the_ledger_summary_for_the_run_and_nothing_else(tmp_path: Path) -> None:
    daemon = _daemon(tmp_path)
    _observe(daemon, RUN, [_usage(1, model=OPUS, input_tokens=100)])
    _observe(daemon, OTHER_RUN, [_usage(1, model=OPUS, input_tokens=ONE_MILLION)])
    collector = daemon.state["transcript_collector"]
    expected = usage_summary(collector.usage.totals(run_id=RUN), price_table_from_config(_cost_tracking()))
    block = run_usage_block(daemon, run_id=RUN)
    assert {key: block[key] for key in expected} == expected
    assert block["tokens"]["input"] == 100


def test_the_block_has_exactly_the_summary_keys_plus_tracked_and_reason(tmp_path: Path) -> None:
    daemon = _daemon(tmp_path)
    _observe(daemon, RUN, [_usage(1, model=OPUS, input_tokens=1)])
    assert set(run_usage_block(daemon, run_id=RUN)) == {"tracked", "reason", "tokens", "cost", "models", "pricing"}


def test_an_unpriced_model_says_unknown_and_names_the_model(tmp_path: Path) -> None:
    daemon = _daemon(tmp_path)
    _observe(daemon, RUN, [_usage(1, model=UNPRICED, input_tokens=500)])
    block = run_usage_block(daemon, run_id=RUN)
    assert block["tracked"] is True
    assert block["cost"]["usd"] is None
    assert UNPRICED in block["cost"]["unknown_reason"]
    assert block["models"][0]["priced"] is False
    assert block["models"][0]["price"] is None


def test_a_run_the_ledger_does_not_know_is_not_tracked_with_a_reason(tmp_path: Path) -> None:
    daemon = _daemon(tmp_path)
    block = run_usage_block(daemon, run_id="run-unknown")
    assert block == {"tracked": False, "reason": NO_USAGE_REASON}


def test_a_run_with_no_usage_records_reads_the_same_live_and_after_a_rebuild(tmp_path: Path) -> None:
    # Live: no usage record was ever observed, so the ledger has no entry for the run.
    live = _daemon(tmp_path)
    # After a restart: _rebuild observes the file with no usage records, leaving an empty entry.
    rebuilt = _daemon(tmp_path)
    _observe(rebuilt, RUN, [])
    assert run_usage_block(live, run_id=RUN) == {"tracked": False, "reason": NO_USAGE_REASON}
    assert run_usage_block(rebuilt, run_id=RUN) == {"tracked": False, "reason": NO_USAGE_REASON}


def test_with_transcripts_disabled_nothing_is_tracked(tmp_path: Path) -> None:
    daemon = _daemon(tmp_path, enabled=False)
    assert run_usage_block(daemon, run_id=RUN) == {"tracked": False, "reason": "transcripts disabled"}


def test_with_no_collector_at_all_nothing_is_tracked() -> None:
    daemon = _daemon(None)
    assert run_usage_block(daemon, run_id=RUN) == {"tracked": False, "reason": "transcripts disabled"}


def test_without_pricing_the_tokens_are_known_and_the_cost_is_not(tmp_path: Path) -> None:
    daemon = _daemon(tmp_path, priced=False)
    _observe(daemon, RUN, [_usage(1, model=OPUS, input_tokens=ONE_MILLION)])
    block = run_usage_block(daemon, run_id=RUN)
    assert block["tokens"]["input"] == ONE_MILLION
    assert block["cost"]["usd"] is None
    assert block["pricing"] == {"as_of": None, "source": "none"}
