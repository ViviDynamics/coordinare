"""#134 review round 1: regression tests for the blocking and follow-up findings.

One group per finding. Each test fails on the code as it was reviewed.
"""
from __future__ import annotations

import json
import os
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from coordinare.config import CostTrackingConfig
from coordinare.services import token_usage
from coordinare.services.token_usage import (
    TokenLedger,
    UsageCounts,
    price_table_from_config,
    price_totals,
    usage_summary,
)

DAY = "2026-10-13"
NEXT_DAY = "2026-10-14"
NOW = datetime(2026, 10, 14, 12, tzinfo=UTC)


def _usage_record(seq: int, *, ts: str, input_tokens: int, model: str | None = "m-1",
                  scope: str | None = "message", cache_read: int = 0) -> dict[str, Any]:
    record: dict[str, Any] = {
        "ts": ts,
        "run_id": "run-a",
        "seq": seq,
        "type": "usage",
        "usage": {"input_tokens": input_tokens, "output_tokens": 0, "cache_read": cache_read,
                  "cache_write": 0, "model": model},
    }
    if scope is not None:
        record["scope"] = scope
    return record


def _observe(ledger: TokenLedger, run_id: str, records: list[dict[str, Any]]) -> int:
    return ledger.observe(run_id, symphony="acme", card="PVTI_1", stage="implementing", records=records)


def _input(ledger: TokenLedger, **filters: Any) -> int:
    return sum(c.input_tokens for c in ledger.totals(**filters).values())


# --- 1. cumulative totals split by UTC day -----------------------------------------


def test_a_cumulative_total_spanning_midnight_is_split_between_the_two_days() -> None:
    ledger = TokenLedger()
    _observe(ledger, "run-a", [
        _usage_record(1, ts=f"{DAY}T23:50:00+00:00", input_tokens=1000, scope="run"),
        _usage_record(2, ts=f"{NEXT_DAY}T00:10:00+00:00", input_tokens=5000, scope="run"),
    ])
    assert _input(ledger, run_id="run-a") == 5000
    assert _input(ledger, days=(DAY, DAY)) == 1000
    assert _input(ledger, days=(NEXT_DAY, NEXT_DAY)) == 4000


def test_a_run_spanning_two_midnights_splits_each_day_by_its_own_step() -> None:
    ledger = TokenLedger()
    _observe(ledger, "run-a", [
        _usage_record(1, ts=f"{DAY}T22:00:00+00:00", input_tokens=1000, scope="run"),
        _usage_record(2, ts=f"{NEXT_DAY}T09:00:00+00:00", input_tokens=3000, scope="run"),
        _usage_record(3, ts="2026-10-15T08:00:00+00:00", input_tokens=3500, scope="run"),
    ])
    assert _input(ledger, days=(DAY, DAY)) == 1000
    assert _input(ledger, days=(NEXT_DAY, NEXT_DAY)) == 2000
    assert _input(ledger, days=("2026-10-15", "2026-10-15")) == 500
    assert _input(ledger, run_id="run-a") == 3500


def test_a_run_with_one_cumulative_record_books_it_all_to_its_day() -> None:
    ledger = TokenLedger()
    _observe(ledger, "run-a", [_usage_record(1, ts=f"{DAY}T10:00:00+00:00", input_tokens=700, scope="run")])
    assert _input(ledger, days=(DAY, DAY)) == 700
    assert _input(ledger, days=(NEXT_DAY, NEXT_DAY)) == 0
    assert _input(ledger, run_id="run-a") == 700


def test_a_later_cumulative_record_on_the_same_day_replaces_the_earlier_one() -> None:
    ledger = TokenLedger()
    _observe(ledger, "run-a", [
        _usage_record(1, ts=f"{DAY}T10:00:00+00:00", input_tokens=700, scope="run"),
        _usage_record(2, ts=f"{DAY}T11:00:00+00:00", input_tokens=900, scope="run"),
    ])
    assert _input(ledger, run_id="run-a") == 900


def test_the_day_split_uses_the_model_of_each_days_last_record() -> None:
    ledger = TokenLedger()
    _observe(ledger, "run-a", [
        _usage_record(1, ts=f"{DAY}T23:00:00+00:00", input_tokens=100, model="m-1", scope="run"),
        _usage_record(2, ts=f"{NEXT_DAY}T01:00:00+00:00", input_tokens=400, model="m-2", scope="run"),
    ])
    assert ledger.totals(days=(DAY, DAY)) == {"m-1": UsageCounts(input_tokens=100)}
    assert ledger.totals(days=(NEXT_DAY, NEXT_DAY)) == {"m-2": UsageCounts(input_tokens=300)}


# --- 8. pruning runs with no parseable timestamp -------------------------------------


def test_a_run_with_no_parseable_timestamp_is_pruned_once_its_ingest_day_is_stale() -> None:
    ledger = TokenLedger(window_days=8)
    _observe(ledger, "unstamped", [_usage_record(1, ts="not a time", input_tokens=5)])
    assert ledger.prune(date(2026, 10, 14)) == 0  # ingested today, kept
    assert ledger.prune(date(2030, 1, 1)) == 1
    assert ledger.run_info("unstamped") is None


# --- 8. global turn budget ------------------------------------------------------------


def test_the_per_turn_budget_is_global_and_drops_detail_from_the_oldest_runs_first(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(token_usage, "MAX_TURNS_PER_RUN", 3)
    monkeypatch.setattr(token_usage, "MAX_TURNS_TOTAL", 4)
    ledger = TokenLedger()
    _observe(ledger, "old", [_usage_record(seq, ts=f"{DAY}T10:00:00+00:00", input_tokens=1) for seq in (1, 2, 3)])
    _observe(ledger, "new", [_usage_record(seq, ts=f"{DAY}T10:00:00+00:00", input_tokens=1) for seq in (1, 2, 3)])
    assert len(ledger.turns("old")) + len(ledger.turns("new")) <= 4
    assert len(ledger.turns("new")) == 3  # the newest run keeps its detail
    assert ledger.run_info("old") is not None and ledger.run_info("old").turns_dropped is True
    # Totals stay exact for every run, even where detail was dropped.
    assert _input(ledger, run_id="old") == 3
    assert _input(ledger, run_id="new") == 3


# --- 10. exact money and unknown cost ----------------------------------------------------


def _table(rate: float) -> Any:
    return price_table_from_config(CostTrackingConfig.model_validate({
        "pricing_as_of": "2026-10-01",
        "prices": [{"models": ["m-1"], "input_per_million": rate, "output_per_million": 0,
                    "cache_read_per_million": 0, "cache_write_per_million": 0}],
    }))


def test_money_sums_are_exact_decimals_not_float_drift() -> None:
    table = _table(0.1)
    totals = {"m-1": UsageCounts(input_tokens=3_000_000)}
    priced = price_totals(totals, table)
    assert priced.cost_usd == Decimal("0.3")
    assert isinstance(priced.cost_usd, Decimal)
    assert usage_summary(totals, table)["cost"]["usd"] == 0.3


def test_an_unknown_cost_is_none_in_the_summary_not_zero() -> None:
    summary = usage_summary({"gpt-x": UsageCounts(input_tokens=10)}, _table(1.0))
    assert summary["cost"]["usd"] is None


# --- 4. a terminal drain that times out still counts the usage it wrote -------------


def test_release_counts_usage_the_writer_holds_before_it_is_dropped(tmp_path: Path) -> None:
    from coordinare.services.run_transcript import TranscriptCollector

    collector = TranscriptCollector(tmp_path / "t", enabled=True, max_bytes_per_run=10**6)
    writer = collector.writer_for(run_id="run-r", symphony="acme", card_id="PVTI_1")
    writer.set_stage("implementing")
    writer.append_batch([_usage_record(1, ts=f"{DAY}T10:00:00+00:00", input_tokens=42)], last_seq=1)
    collector.release("run-r")
    assert _input(collector.usage, run_id="run-r") == 42


async def test_a_timed_out_absorb_still_counts_the_usage_already_written(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:

    from typing import cast

    from coordinare.models.performer_endpoint import JobStatus, PerformerEndpointConfig
    from coordinare.services import http_performer_service as hps
    from coordinare.services.http_performer_service import HTTPPerformerService
    from coordinare.services.run_transcript import TranscriptCollector
    from coordinare.transport.http_transport import PerformerHTTPClient

    collector = TranscriptCollector(tmp_path / "t", enabled=True, max_bytes_per_run=10**6)
    config = PerformerEndpointConfig(
        id="perf-r", mode="persistent", image="performer:full",
        endpoint="http://127.0.0.1:9", roles=["implementer"],
    )
    service = HTTPPerformerService(
        config, client=cast("PerformerHTTPClient", None), transcripts=collector,
    )
    writer = collector.writer_for(run_id="run-t", symphony="acme", card_id="PVTI_1")
    writer.set_stage("implementing")
    writer.append_batch([_usage_record(1, ts=f"{DAY}T10:00:00+00:00", input_tokens=9)], last_seq=1)
    status = JobStatus(job_id="j", state="running", started_at=datetime.now(UTC),
                       transcript=[], transcript_last_seq=1)

    async def _timeout(*_args: Any, **_kwargs: Any) -> None:
        raise TimeoutError

    monkeypatch.setattr(hps.asyncio, "wait_for", _timeout)
    await service._absorb_transcript(writer, status, "run-t")
    assert _input(collector.usage, run_id="run-t") == 9


# --- 7. the boot rebuild is bounded, streaming and best effort ----------------------


def _write_run(root: Path, *, symphony: str, card: str, run_id: str, lines: list[str]) -> Path:
    path = root / symphony / card / f"{run_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(line + "\n" for line in lines), encoding="utf-8")
    return path


def _usage_line(seq: int, input_tokens: int = 1) -> str:
    return json.dumps(_usage_record(seq, ts=f"{DAY}T10:00:00+00:00", input_tokens=input_tokens))


def test_an_oversize_usage_line_is_skipped_and_reported_not_loaded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(token_usage, "USAGE_LINE_MAX_BYTES", 400)
    big = json.dumps({"ts": f"{DAY}T10:00:00+00:00", "seq": 1, "type": "usage", "usage": {"input_tokens": 9},
                      "text": "x" * 2000})
    _write_run(tmp_path, symphony="acme", card="PVTI_1", run_id="run-big",
               lines=[big, _usage_line(2, input_tokens=4)])
    ledger = TokenLedger()
    counted = ledger.load_from_transcripts(tmp_path, now=NOW)
    assert counted == 1
    assert _input(ledger, run_id="run-big") == 4
    assert ledger.last_rebuild.oversize_usage_lines == 1


def test_the_rebuild_stops_at_its_byte_budget_and_says_so(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(token_usage, "REBUILD_MAX_BYTES", 1)
    _write_run(tmp_path, symphony="acme", card="PVTI_1", run_id="run-a", lines=[_usage_line(1)])
    ledger = TokenLedger()
    assert ledger.load_from_transcripts(tmp_path, now=NOW) == 0
    assert ledger.last_rebuild.stopped_by == "bytes"


def test_the_rebuild_stops_at_its_time_budget(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(token_usage, "REBUILD_MAX_SECONDS", -1.0)
    _write_run(tmp_path, symphony="acme", card="PVTI_1", run_id="run-a", lines=[_usage_line(1)])
    ledger = TokenLedger()
    ledger.load_from_transcripts(tmp_path, now=NOW)
    assert ledger.last_rebuild.stopped_by == "time"


def test_an_unreadable_card_directory_is_skipped_and_the_rest_still_loads(tmp_path: Path) -> None:
    _write_run(tmp_path, symphony="acme", card="PVTI_good", run_id="run-good", lines=[_usage_line(1, 7)])
    locked = tmp_path / "acme" / "PVTI_locked"
    locked.mkdir()
    _write_run(tmp_path, symphony="acme", card="PVTI_locked", run_id="run-locked", lines=[_usage_line(1, 99)])
    os.chmod(locked, 0)
    try:
        enforced = not os.access(locked, os.R_OK)  # running as root: permissions do not bind
        ledger = TokenLedger()
        counted = ledger.load_from_transcripts(tmp_path, now=NOW)
    finally:
        os.chmod(locked, 0o755)
    if not enforced:
        pytest.skip("permissions are not enforced for this user")
    assert counted == 1
    assert _input(ledger, run_id="run-good") == 7
    assert ledger.last_rebuild.dirs_skipped == 1


def test_a_file_that_raises_mid_read_is_skipped_not_fatal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write_run(tmp_path, symphony="acme", card="PVTI_1", run_id="run-x", lines=[_usage_line(1, 3)])
    real_open = Path.open

    def _broken_open(self: Path, *args: Any, **kwargs: Any) -> Any:
        if self.name == "run-x.jsonl":
            raise OSError("simulated read failure")
        return real_open(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", _broken_open)
    ledger = TokenLedger()
    assert ledger.load_from_transcripts(tmp_path, now=NOW) == 0
    assert ledger.last_rebuild.files_skipped == 1


def test_the_window_still_excludes_old_files_with_the_streaming_reader(tmp_path: Path) -> None:
    path = _write_run(tmp_path, symphony="acme", card="PVTI_1", run_id="run-old", lines=[_usage_line(1, 5)])
    old = (datetime(2026, 10, 14, tzinfo=UTC) - timedelta(days=30)).timestamp()
    os.utime(path, (old, old))
    ledger = TokenLedger()
    assert ledger.load_from_transcripts(tmp_path, now=NOW) == 0
    assert ledger.last_rebuild.files_read == 0
