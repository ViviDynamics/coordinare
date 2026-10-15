"""#134: the usage ledger: per-turn records, the scope rule, rollups, live totals, restart.

The ledger is fed the records the writer accepts (``seq``, ``ts``, ``scope`` and a
``usage`` object), so these tests use that shape directly.
"""
from __future__ import annotations

import json
import os
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from coordinare.services import token_usage
from coordinare.services.token_usage import TokenLedger, UsageCounts, day_of

DAY = "2026-10-13"
NEXT_DAY = "2026-10-14"


def _usage(
    seq: int,
    *,
    model: str | None = "claude-opus-4-5",
    scope: str | None = "message",
    ts: str = f"{DAY}T10:00:00+00:00",
    input_tokens: int = 100,
    output_tokens: int = 10,
    cache_read: int = 0,
    cache_write: int = 0,
    stage: str | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "ts": ts,
        "run_id": "run-a",
        "seq": seq,
        "type": "usage",
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cache_read": cache_read,
            "cache_write": cache_write,
            "model": model,
        },
    }
    if scope is not None:
        record["scope"] = scope
    if stage is not None:
        record["stage"] = stage
    return record


def _observe(ledger: TokenLedger, run_id: str, records: list[dict[str, Any]], *,
             card: str = "PVTI_1", stage: str = "implementing", symphony: str = "acme") -> int:
    return ledger.observe(run_id, symphony=symphony, card=card, stage=stage, records=records)


def _total_input(ledger: TokenLedger, **filters: Any) -> int:
    return sum(c.input_tokens for c in ledger.totals(**filters).values())


# --- counts and day keys -----------------------------------------------------


def test_counts_add_and_total_all_four_kinds() -> None:
    a = UsageCounts(input_tokens=1, output_tokens=2, cache_read=3, cache_write=4)
    b = UsageCounts(input_tokens=10, output_tokens=20, cache_read=30, cache_write=40)
    total = a + b
    assert (total.input_tokens, total.output_tokens, total.cache_read, total.cache_write) == (11, 22, 33, 44)
    assert total.total == 110


def test_counts_from_a_usage_object_ignore_booleans_floats_and_junk() -> None:
    counts = UsageCounts.from_mapping({"input_tokens": True, "output_tokens": 2.5, "cache_read": "9", "cache_write": 7})
    assert (counts.input_tokens, counts.output_tokens, counts.cache_read, counts.cache_write) == (0, 0, 0, 7)


def test_the_day_is_the_utc_day_of_the_record() -> None:
    assert day_of("2026-10-13T23:30:00-05:00") == NEXT_DAY  # 04:30 UTC the next day
    assert day_of("2026-10-13T10:00:00+00:00") == DAY
    assert day_of("not a time") is None


# --- per turn ----------------------------------------------------------------


def test_each_turn_keeps_its_model_and_four_counts() -> None:
    ledger = TokenLedger()
    _observe(ledger, "run-a", [
        _usage(1, input_tokens=5, output_tokens=6, cache_read=7, cache_write=8),
        _usage(2, model="claude-haiku-4-5", input_tokens=50),
    ])
    turns = ledger.turns("run-a")
    assert [t.seq for t in turns] == [1, 2]
    assert (turns[0].counts.input_tokens, turns[0].counts.output_tokens) == (5, 6)
    assert (turns[0].counts.cache_read, turns[0].counts.cache_write) == (7, 8)
    assert turns[0].model == "claude-opus-4-5"
    assert turns[1].model == "claude-haiku-4-5"
    assert turns[1].day == DAY


def test_turns_beyond_the_bound_are_not_kept_but_totals_stay_exact(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(token_usage, "MAX_TURNS_PER_RUN", 3)
    ledger = TokenLedger()
    _observe(ledger, "run-a", [_usage(seq, input_tokens=10) for seq in range(1, 6)])
    assert len(ledger.turns("run-a")) == 3
    info = ledger.run_info("run-a")
    assert info is not None and info.turns_dropped is True
    assert _total_input(ledger, run_id="run-a") == 50


def test_records_without_usage_counts_are_not_turns() -> None:
    ledger = TokenLedger()
    empty = {"ts": f"{DAY}T10:00:00+00:00", "seq": 1, "type": "usage", "usage": {}}
    not_usage = {"ts": f"{DAY}T10:00:00+00:00", "seq": 2, "type": "assistant_text", "text": "hi"}
    assert _observe(ledger, "run-a", [empty, not_usage]) == 0
    assert ledger.turns("run-a") == []


# --- scope rule per run --------------------------------------------------------


def test_the_larger_of_the_message_sum_and_the_run_total_is_the_total_and_the_gap_is_named() -> None:
    """Spec 242: a lost message record can only make the message sum low, so the larger value wins
    and the disagreement is reported rather than read as an exact figure."""
    ledger = TokenLedger()
    _observe(ledger, "run-a", [
        _usage(1, input_tokens=100),
        _usage(2, input_tokens=200),
        _usage(3, input_tokens=999, scope="run", model=None),
    ])
    assert _total_input(ledger, run_id="run-a") == 999
    assert ledger.run_disagreement("run-a") is not None

    _observe(ledger, "run-b", [
        _usage(1, input_tokens=300),
        _usage(2, input_tokens=50, scope="run", model=None),
    ])
    assert _total_input(ledger, run_id="run-b") == 300
    assert ledger.run_disagreement("run-b") is not None


def test_a_run_record_alone_is_the_total_and_the_last_one_wins() -> None:
    ledger = TokenLedger()
    _observe(ledger, "run-a", [_usage(1, input_tokens=400, scope="run", model=None)])
    assert _total_input(ledger, run_id="run-a") == 400
    _observe(ledger, "run-a", [_usage(2, input_tokens=700, scope="run", model=None)])
    assert _total_input(ledger, run_id="run-a") == 700  # cumulative, so replaced, not added


def test_unscoped_records_are_summed_and_flagged() -> None:
    ledger = TokenLedger()
    _observe(ledger, "run-a", [_usage(1, input_tokens=30, scope=None), _usage(2, input_tokens=70, scope=None)])
    assert _total_input(ledger, run_id="run-a") == 100
    info = ledger.run_info("run-a")
    assert info is not None and info.unscoped is True


def test_a_run_with_message_records_is_not_flagged_unscoped() -> None:
    ledger = TokenLedger()
    _observe(ledger, "run-a", [_usage(1)])
    info = ledger.run_info("run-a")
    assert info is not None and info.unscoped is False


# --- live totals --------------------------------------------------------------


def test_totals_rise_with_each_absorbed_batch_while_the_run_is_in_progress() -> None:
    ledger = TokenLedger()
    seen: list[int] = []
    _observe(ledger, "run-a", [_usage(1, input_tokens=100)])
    seen.append(_total_input(ledger, run_id="run-a"))
    _observe(ledger, "run-a", [_usage(2, input_tokens=50, cache_read=500)])
    seen.append(_total_input(ledger, run_id="run-a"))
    _observe(ledger, "run-a", [_usage(3, input_tokens=25)])
    seen.append(_total_input(ledger, run_id="run-a"))
    assert seen == [100, 150, 175]
    assert seen == sorted(seen) and len(set(seen)) == 3


def test_a_record_already_counted_is_not_counted_twice() -> None:
    ledger = TokenLedger()
    batch = [_usage(1, input_tokens=100), _usage(2, input_tokens=100)]
    assert _observe(ledger, "run-a", batch) == 2
    assert _observe(ledger, "run-a", batch) == 0
    assert _total_input(ledger, run_id="run-a") == 200


def test_the_version_moves_when_totals_change() -> None:
    ledger = TokenLedger()
    before = ledger.version
    _observe(ledger, "run-a", [_usage(1)])
    assert ledger.version > before
    after = ledger.version
    _observe(ledger, "run-a", [_usage(1)])  # nothing new
    assert ledger.version == after


# --- rollups ----------------------------------------------------------------------


def _two_runs_two_stages() -> TokenLedger:
    ledger = TokenLedger()
    _observe(ledger, "run-impl", [
        _usage(1, input_tokens=100, output_tokens=10),
        _usage(2, input_tokens=200, output_tokens=20, model="claude-haiku-4-5"),
    ], stage="implementing")
    _observe(ledger, "run-rev", [
        _usage(1, input_tokens=1000, output_tokens=1, ts=f"{NEXT_DAY}T08:00:00+00:00"),
    ], stage="reviewing")
    _observe(ledger, "run-other-card", [_usage(1, input_tokens=7)], card="PVTI_2", symphony="other", stage="triage")
    return ledger


def test_rollup_by_run() -> None:
    ledger = _two_runs_two_stages()
    assert _total_input(ledger, run_id="run-impl") == 300
    assert _total_input(ledger, run_id="run-rev") == 1000


def test_rollup_by_stage_and_by_card_and_by_symphony() -> None:
    ledger = _two_runs_two_stages()
    assert _total_input(ledger, stage="implementing") == 300
    assert _total_input(ledger, stage="reviewing") == 1000
    assert _total_input(ledger, stage="triage") == 7
    assert _total_input(ledger, card="PVTI_1") == 1300
    assert _total_input(ledger, card="PVTI_2") == 7
    assert _total_input(ledger, symphony="acme") == 1300
    assert _total_input(ledger, symphony="other") == 7


def test_rollup_by_day_uses_the_utc_day_of_each_turn() -> None:
    ledger = _two_runs_two_stages()
    assert _total_input(ledger, days=(DAY, DAY)) == 300 + 7
    assert _total_input(ledger, days=(NEXT_DAY, NEXT_DAY)) == 1000
    assert _total_input(ledger, days=(DAY, NEXT_DAY)) == 1307


def test_totals_split_by_model() -> None:
    ledger = _two_runs_two_stages()
    by_model = ledger.totals(run_id="run-impl")
    assert by_model["claude-opus-4-5"].input_tokens == 100
    assert by_model["claude-haiku-4-5"].input_tokens == 200
    assert by_model["claude-haiku-4-5"].output_tokens == 20


def test_a_filter_matching_nothing_is_empty_not_zero() -> None:
    ledger = _two_runs_two_stages()
    assert ledger.totals(card="PVTI_nothing") == {}


def test_runs_are_listed_for_a_card() -> None:
    ledger = _two_runs_two_stages()
    assert sorted(ledger.run_ids(card="PVTI_1")) == ["run-impl", "run-rev"]
    assert ledger.run_ids(card="PVTI_missing") == []


def test_run_info_records_the_attribution_of_a_run() -> None:
    ledger = _two_runs_two_stages()
    info = ledger.run_info("run-rev")
    assert info is not None
    assert (info.symphony, info.card, info.stage, info.last_seq) == ("acme", "PVTI_1", "reviewing", 1)


def test_the_nil_model_is_its_own_bucket() -> None:
    ledger = TokenLedger()
    _observe(ledger, "run-a", [_usage(1, model=None, input_tokens=9)])
    assert list(ledger.totals(run_id="run-a")) == [None]


# --- pruning ------------------------------------------------------------------------


def test_runs_older_than_the_window_are_pruned() -> None:
    ledger = TokenLedger(window_days=8)
    _observe(ledger, "old", [_usage(1, ts="2026-10-01T10:00:00+00:00")])
    _observe(ledger, "new", [_usage(1)])
    removed = ledger.prune(date(2026, 10, 13))
    assert removed == 1
    assert ledger.run_info("old") is None
    assert ledger.run_info("new") is not None


# --- restart: rebuild from the transcript files ---------------------------------------


def _write_transcript(root: Path, *, symphony: str, card: str, run_id: str, records: list[dict[str, Any]]) -> Path:
    path = root / symphony / card / f"{run_id}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return path


def test_rebuild_from_disk_matches_the_live_totals(tmp_path: Path) -> None:
    records = [
        {"ts": f"{DAY}T10:00:00+00:00", "run_id": "run-a", "seq": 1, "type": "user", "text": "go"},
        _usage(2, input_tokens=100, cache_read=40, stage="implementing"),
        _usage(3, input_tokens=60, cache_write=5, stage="implementing"),
    ]
    _write_transcript(tmp_path, symphony="acme", card="PVTI_1", run_id="run-a", records=records)
    rebuilt = TokenLedger()
    assert rebuilt.load_from_transcripts(tmp_path, now=datetime(2026, 10, 13, 12, tzinfo=UTC)) == 2
    assert _total_input(rebuilt, run_id="run-a") == 160
    assert _total_input(rebuilt, stage="implementing") == 160
    assert _total_input(rebuilt, card="PVTI_1", symphony="acme") == 160


def test_a_restarted_run_continues_without_double_counting(tmp_path: Path) -> None:
    first = [_usage(2, input_tokens=100, stage="implementing")]
    path = _write_transcript(tmp_path, symphony="acme", card="PVTI_1", run_id="run-a", records=first)
    live = TokenLedger()
    _observe(live, "run-a", first, stage="implementing")

    restarted = TokenLedger()
    restarted.load_from_transcripts(tmp_path, now=datetime(2026, 10, 13, 12, tzinfo=UTC))
    assert _total_input(restarted, run_id="run-a") == _total_input(live, run_id="run-a") == 100

    # The writer's cursor is read from the file, so the next delivery starts at seq 3.
    more = [_usage(3, input_tokens=25, stage="implementing")]
    path.write_text(path.read_text(encoding="utf-8") + json.dumps(more[0]) + "\n", encoding="utf-8")
    _observe(restarted, "run-a", more, stage="implementing")
    assert _total_input(restarted, run_id="run-a") == 125
    # A redelivery of anything already on disk is not counted again.
    _observe(restarted, "run-a", first, stage="implementing")
    assert _total_input(restarted, run_id="run-a") == 125


def test_rebuild_reads_only_files_inside_the_window(tmp_path: Path) -> None:
    _write_transcript(tmp_path, symphony="acme", card="PVTI_1", run_id="run-a", records=[_usage(1, input_tokens=1)])
    old = tmp_path / "acme" / "PVTI_1" / "run-a.jsonl"
    long_ago = (datetime(2026, 10, 13, tzinfo=UTC) - timedelta(days=30)).timestamp()
    os.utime(old, (long_ago, long_ago))
    ledger = TokenLedger()
    assert ledger.load_from_transcripts(tmp_path, now=datetime(2026, 10, 13, 12, tzinfo=UTC)) == 0
    assert ledger.run_info("run-a") is None


def test_rebuild_ignores_torn_and_foreign_lines(tmp_path: Path) -> None:
    path = _write_transcript(tmp_path, symphony="acme", card="PVTI_1", run_id="run-a", records=[_usage(1, input_tokens=4)])
    path.write_text(path.read_text(encoding="utf-8") + '{"seq": 2, "type": "usage", "usa', encoding="utf-8")
    ledger = TokenLedger()
    ledger.load_from_transcripts(tmp_path, now=datetime(2026, 10, 13, 12, tzinfo=UTC))
    assert _total_input(ledger, run_id="run-a") == 4


def test_a_missing_root_rebuilds_nothing(tmp_path: Path) -> None:
    assert TokenLedger().load_from_transcripts(tmp_path / "absent", now=datetime(2026, 10, 13, tzinfo=UTC)) == 0
