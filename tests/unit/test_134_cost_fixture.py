"""#134 acceptance: the cost of a sample run matches a hand calculation from its transcript.

The fixture ``tests/fixtures/134/sample_run.jsonl`` is one Claude Code run with three
model turns (two Opus, one Haiku), cache reads and writes, and the run-total record
that the stream also carries. The arithmetic, done by hand from the fixture:

    claude-opus-4-5 (input 15, output 75, cache read 1.5, cache write 18.75 per M)
      input        1000 + 50          = 1050    x 15.00   / 1e6 = 0.01575
      output       200 + 800          = 1000    x 75.00   / 1e6 = 0.075
      cache read   5000 + 12000       = 17000   x 1.50    / 1e6 = 0.0255
      cache write  300 + 0            = 300     x 18.75   / 1e6 = 0.005625
      subtotal                                                  = 0.121875

    claude-haiku-4-5 (input 1, output 5, cache read 0.10, cache write 1.25 per M)
      input        2000               = 2000    x 1.00    / 1e6 = 0.002
      output       100                = 100     x 5.00    / 1e6 = 0.0005
      cache read   0                  = 0       x 0.10    / 1e6 = 0
      cache write  1000               = 1000    x 1.25    / 1e6 = 0.00125
      subtotal                                                  = 0.00375

    run total                                                   = 0.125625

The run-total record (scope "run", no model) repeats the message sums, so it must
not be counted on top of them, and it has no model to price it by.
"""
from __future__ import annotations

import shutil
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from coordinare.config import CostTrackingConfig
from coordinare.services.token_usage import TokenLedger, price_table_from_config, price_totals

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "134" / "sample_run.jsonl"
NOW = datetime(2026, 10, 14, 12, tzinfo=UTC)

PRICES = CostTrackingConfig.model_validate({
    "pricing_as_of": "2026-10-01",
    "prices": [
        {
            "models": ["claude-opus-4-5"],
            "input_per_million": 15.0,
            "output_per_million": 75.0,
            "cache_read_per_million": 1.5,
            "cache_write_per_million": 18.75,
        },
        {
            "models": ["claude-haiku-4-5"],
            "input_per_million": 1.0,
            "output_per_million": 5.0,
            "cache_read_per_million": 0.1,
            "cache_write_per_million": 1.25,
        },
    ],
})


@pytest.fixture()
def run_on_disk(tmp_path: Path) -> Path:
    """The fixture laid out where the daemon writes it: <root>/<symphony>/<card>/<run>.jsonl."""
    target = tmp_path / "acme" / "PVTI_134" / "run-134.jsonl"
    target.parent.mkdir(parents=True)
    shutil.copyfile(FIXTURE, target)
    return tmp_path


def test_the_sample_run_totals_by_kind_and_model(run_on_disk: Path) -> None:
    ledger = TokenLedger()
    # Four usage records: three model turns and the run total (which the rule ignores).
    assert ledger.load_from_transcripts(run_on_disk, now=NOW) == 4
    by_model = ledger.totals(run_id="run-134")
    opus = by_model["claude-opus-4-5"]
    haiku = by_model["claude-haiku-4-5"]
    assert (opus.input_tokens, opus.output_tokens, opus.cache_read, opus.cache_write) == (1050, 1000, 17000, 300)
    assert (haiku.input_tokens, haiku.output_tokens, haiku.cache_read, haiku.cache_write) == (2000, 100, 0, 1000)
    assert set(by_model) == {"claude-opus-4-5", "claude-haiku-4-5"}


def test_the_sample_run_cost_matches_the_hand_calculation(run_on_disk: Path) -> None:
    ledger = TokenLedger()
    ledger.load_from_transcripts(run_on_disk, now=NOW)
    priced = price_totals(ledger.totals(run_id="run-134"), price_table_from_config(PRICES))
    assert priced.cost_usd == pytest.approx(0.125625, abs=1e-12)
    assert priced.unknown_reason is None
    lines = {line.model: line for line in priced.models}
    assert lines["claude-opus-4-5"].cost_usd == pytest.approx(0.121875, abs=1e-12)
    assert lines["claude-haiku-4-5"].cost_usd == pytest.approx(0.00375, abs=1e-12)


def test_the_breakdown_by_kind_adds_up_to_the_total(run_on_disk: Path) -> None:
    ledger = TokenLedger()
    ledger.load_from_transcripts(run_on_disk, now=NOW)
    priced = price_totals(ledger.totals(run_id="run-134"), price_table_from_config(PRICES))
    kinds = priced.kind_usd
    assert kinds["input"] == pytest.approx(0.01575 + 0.002, abs=1e-12)
    assert kinds["output"] == pytest.approx(0.075 + 0.0005, abs=1e-12)
    assert kinds["cache_read"] == pytest.approx(0.0255, abs=1e-12)
    assert kinds["cache_write"] == pytest.approx(0.005625 + 0.00125, abs=1e-12)
    assert sum(kinds.values()) == pytest.approx(0.125625, abs=1e-12)


def test_the_run_total_record_is_not_counted_on_top(run_on_disk: Path) -> None:
    ledger = TokenLedger()
    ledger.load_from_transcripts(run_on_disk, now=NOW)
    tokens = price_totals(ledger.totals(run_id="run-134"), price_table_from_config(PRICES)).tokens
    assert tokens.input_tokens == 3050
    assert tokens.output_tokens == 1100
    assert tokens.cache_read == 17000
    assert tokens.cache_write == 1300


def test_the_run_counts_on_the_day_of_its_turns(run_on_disk: Path) -> None:
    ledger = TokenLedger()
    ledger.load_from_transcripts(run_on_disk, now=NOW)
    today = ledger.totals(days=(date(2026, 10, 14).isoformat(), date(2026, 10, 14).isoformat()))
    assert sum(c.input_tokens for c in today.values()) == 3050
