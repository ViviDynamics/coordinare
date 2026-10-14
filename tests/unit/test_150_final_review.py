"""#150 final review: folder-phase cap, card cap, listing bound, every partial reason, the wall-clock
anchor, the stat budget counted against real calls, and the cursor bound to its filters.

Each test here was written against the code the review examined, before the fix.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from coordinare.services import run_history
from coordinare.services.run_history import (
    HistoryFilter,
    HistoryQuery,
    HistoryQueryError,
    UsageSource,
    list_history,
)
from coordinare.services.run_transcript import transcript_path
from coordinare.services.token_usage import price_table_from_config

SYMPHONY = "coordinare"
EPOCH = 1_700_000_000
BASE_TS = datetime(2026, 10, 1, 10, 0, tzinfo=UTC)
NO_USAGE = UsageSource(ledger=None, table=price_table_from_config(None))


def _write(root: Path, run: str, card: str, *, mtime_ns: int, symphony: str = SYMPHONY) -> Path:
    path = transcript_path(root, symphony=symphony, card_id=card, run_id=run)
    path.parent.mkdir(parents=True, exist_ok=True)
    header = {"type": "run_header", "seq": 0, "header_version": 1, "ts": BASE_TS.isoformat(), "run_id": run,
              "persona": {"name": "implementing", "version": 1, "version_sha256": "ab" * 32},
              "performer": {"id": "p", "backend": "claude-code", "transport": "http", "endpoint": "e"},
              "model": {"model": "m"}, "card": {"id": card, "issue_number": 1, "title": "t"}}
    body = json.dumps(header) + "\n" + json.dumps({"type": "assistant", "ts": BASE_TS.isoformat()}) + "\n"
    path.write_text(body, encoding="utf-8")
    os.utime(path, ns=(mtime_ns, mtime_ns))
    return path


def _query(**kwargs: Any) -> HistoryQuery:
    limit = kwargs.pop("limit", 50)
    cursor = kwargs.pop("cursor", None)
    visible = kwargs.pop("visible", frozenset({SYMPHONY}))
    return HistoryQuery(filters=HistoryFilter(**kwargs), visible=visible, limit=limit, cursor=cursor)


def _runs(body: dict[str, Any]) -> list[str]:
    return [row["run"] for row in body["rows"]]


@pytest.fixture
def root(tmp_path: Path) -> Path:
    base = tmp_path / "transcripts"
    base.mkdir()
    return base.resolve()


# --- 1: the folder-phase cap still lists transcripts ----------------------------- #


def test_a_cap_hit_in_the_folder_phase_still_lists_runs(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(run_history, "HISTORY_STAT_MAX", 4)
    for card in ("a1", "a2", "a3", "z9"):
        _write(root, f"{card}-run", card, mtime_ns=EPOCH * 10**9)
    body = list_history(root, _query(), NO_USAGE)
    assert body["rows"], "an empty page with the budget spent in the folder phase"
    assert run_history.FOLDER_CAP_REASON in body["partial_reasons"]


def test_the_folder_phase_message_says_not_every_card_was_checked(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(run_history, "HISTORY_STAT_MAX", 4)
    for card in ("a1", "a2", "a3", "z9"):
        _write(root, f"{card}-run", card, mtime_ns=EPOCH * 10**9)
    body = list_history(root, _query(), NO_USAGE)
    details = " ".join(body["partial_details"])
    assert "This listing is incomplete: not every card was checked." in details


# --- 2: the card cap keeps the newest runs and says what it dropped --------------- #


def test_a_card_over_the_cap_keeps_its_newest_run(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(run_history, "HISTORY_CARD_FILES_MAX", 3)
    for index in range(4):
        _write(root, f"a{index}", "c1", mtime_ns=(EPOCH + index) * 10**9)
    _write(root, "zz-newest", "c1", mtime_ns=(EPOCH + 10_000) * 10**9)
    body = list_history(root, _query(card="c1"), NO_USAGE)
    assert "zz-newest" in _runs(body)
    assert len(body["rows"]) == 3


def test_the_card_cap_message_says_the_oldest_were_dropped(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(run_history, "HISTORY_CARD_FILES_MAX", 3)
    for index in range(4):
        _write(root, f"a{index}", "c1", mtime_ns=(EPOCH + index) * 10**9)
    body = list_history(root, _query(card="c1"), NO_USAGE)
    assert run_history.CARD_CAP_REASON in body["partial_reasons"]
    assert any("oldest" in detail for detail in body["partial_details"])
    assert not any("older runs are not listed" in detail for detail in body["partial_details"])


def test_a_card_under_the_cap_is_not_reported(root: Path) -> None:
    for index in range(3):
        _write(root, f"a{index}", "c1", mtime_ns=(EPOCH + index) * 10**9)
    body = list_history(root, _query(card="c1"), NO_USAGE)
    assert body["partial"] is False
    assert body["partial_reasons"] == []


# --- 2b: the per-folder listing is itself bounded -------------------------------- #


def test_a_folder_listing_stops_at_its_entry_bound_and_says_so(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(run_history, "HISTORY_FOLDER_LIST_MAX", 3)
    for index in range(8):
        _write(root, f"r{index}", "c1", mtime_ns=(EPOCH + index) * 10**9)
    body = list_history(root, _query(card="c1"), NO_USAGE)
    assert run_history.LIST_CAP_REASON in body["partial_reasons"]
    assert body["scanned"] <= 6  # the symphony and conductor checks, the card, and the three listed runs


# --- 3: every partial reason that fired is reported ------------------------------ #


def test_every_partial_reason_that_fired_is_reported(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(run_history, "HISTORY_STAT_MAX", 40)
    monkeypatch.setattr(run_history, "HISTORY_CARD_FILES_MAX", 2)
    for index in range(4):
        _write(root, f"many{index}", "AA0", mtime_ns=(EPOCH + index) * 10**9)
    for card_index in range(30):
        _write(root, "r0", f"PVTI_{card_index:02d}", mtime_ns=EPOCH * 10**9)
    os.utime(root / SYMPHONY / "AA0", ns=(4 * 10**18, 4 * 10**18))  # the newest folder, read first in the file phase
    body = list_history(root, _query(), NO_USAGE)
    assert run_history.CARD_CAP_REASON in body["partial_reasons"]
    assert run_history.FOLDER_CAP_REASON in body["partial_reasons"] or run_history.STAT_CAP_REASON in body["partial_reasons"]
    assert len(body["partial_details"]) == len(body["partial_reasons"])


# --- 4: the wall-clock anchor ----------------------------------------------------- #


def test_the_anchor_is_the_wall_clock_when_page_one_is_produced(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(run_history.time, "time_ns", lambda: 1_234_567_890_123_456_789)
    _write(root, "r0", "c1", mtime_ns=EPOCH * 10**9)
    _write(root, "r1", "c1", mtime_ns=(EPOCH + 1) * 10**9)
    page = list_history(root, _query(limit=1), NO_USAGE)
    assert page["next_cursor"] is not None
    assert page["next_cursor"].split("|")[1] == "1234567890123456789"


def test_a_run_written_on_the_anchor_tick_is_announced_not_silently_skipped(
    root: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    tick = 1_700_000_100 * 10**9
    monkeypatch.setattr(run_history.time, "time_ns", lambda: tick)
    for index in range(4):
        _write(root, f"base{index}", "c1", mtime_ns=(EPOCH + index) * 10**9)
    first = list_history(root, _query(limit=2), NO_USAGE)
    _write(root, "tick-run", "c9", mtime_ns=tick)  # the same nanosecond as the anchor, sorts before the cursor
    second = list_history(root, _query(limit=2, cursor=first["next_cursor"]), NO_USAGE)
    assert "tick-run" not in _runs(second)
    assert second["newer_since_anchor"] >= 1


def test_a_run_written_after_the_anchor_is_counted_and_not_listed(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tick = 1_700_000_100 * 10**9
    monkeypatch.setattr(run_history.time, "time_ns", lambda: tick)
    for index in range(4):
        _write(root, f"base{index}", "c1", mtime_ns=(EPOCH + index) * 10**9)
    first = list_history(root, _query(limit=2), NO_USAGE)
    _write(root, "later", "c1", mtime_ns=tick + 10**9)
    second = list_history(root, _query(limit=2, cursor=first["next_cursor"]), NO_USAGE)
    assert "later" not in _runs(second)
    assert second["newer_since_anchor"] == 1


# --- 5: the stat budget counts every call against the real filesystem ------------ #


@pytest.mark.parametrize("cap", [1, 3, 7])
def test_no_run_listing_makes_more_stat_calls_than_its_budget(
    root: Path, monkeypatch: pytest.MonkeyPatch, cap: int,
) -> None:
    for symphony in ("alpha", "beta"):
        for card in range(4):
            for run in range(3):
                _write(root, f"{symphony}-{card}-{run}", f"c{card}", mtime_ns=(EPOCH + run) * 10**9, symphony=symphony)
    monkeypatch.setattr(run_history, "HISTORY_STAT_MAX", cap)
    real_lstat, real_stat = os.lstat, os.stat
    calls: list[str] = []

    def counting_lstat(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        calls.append(os.fspath(path))
        return real_lstat(path, *args, **kwargs)

    def counting_stat(path: Any, *args: Any, **kwargs: Any) -> os.stat_result:
        calls.append(os.fspath(path))
        return real_stat(path, *args, **kwargs)

    monkeypatch.setattr(os, "lstat", counting_lstat)
    monkeypatch.setattr(os, "stat", counting_stat)
    list_history(root, _query(visible=frozenset({"alpha", "beta"}), limit=200), NO_USAGE)
    assert len(calls) <= cap


# --- 6: the cursor is bound to its filters --------------------------------------- #


def test_a_cursor_from_other_filters_is_ignored_and_page_one_is_served(root: Path) -> None:
    for index in range(4):
        _write(root, f"r{index}", "c1", mtime_ns=(EPOCH + index) * 10**9)
    first = list_history(root, _query(limit=2), NO_USAGE)
    other = list_history(root, _query(limit=2, cursor=first["next_cursor"], outcome="failed"), NO_USAGE)
    fresh = list_history(root, _query(limit=2, outcome="failed"), NO_USAGE)
    assert other["cursor_ignored"] is True
    assert _runs(other) == _runs(fresh)


def test_a_cursor_for_the_same_filters_is_not_ignored(root: Path) -> None:
    for index in range(4):
        _write(root, f"r{index}", "c1", mtime_ns=(EPOCH + index) * 10**9)
    first = list_history(root, _query(limit=2), NO_USAGE)
    second = list_history(root, _query(limit=2, cursor=first["next_cursor"]), NO_USAGE)
    assert second["cursor_ignored"] is False
    assert set(_runs(first)).isdisjoint(_runs(second))


@pytest.mark.parametrize("cursor", [
    "²|0|1|alpha|c1|r1", "1" * 5000 + "|0|1|alpha|c1|r1", "abc|0|1|alpha|c1|r1",
    "0123456789ab|²|1|alpha|c1|r1", "0123456789ab|0|1|alpha|c1|", "0123456789ab|0|1|alpha|c1",
])
def test_a_malformed_six_part_cursor_is_refused(root: Path, cursor: str) -> None:
    _write(root, "r1", "c1", mtime_ns=EPOCH * 10**9)
    with pytest.raises(HistoryQueryError):
        list_history(root, _query(cursor=cursor), NO_USAGE)


def test_the_filter_fingerprint_is_stable_for_equal_filters() -> None:
    assert run_history.filter_fingerprint(HistoryFilter(card="c1")) == run_history.filter_fingerprint(
        HistoryFilter(card="c1", stage=None, since=None),
    )
    assert run_history.filter_fingerprint(HistoryFilter(card="c1")) != run_history.filter_fingerprint(
        HistoryFilter(card="c2"),
    )


def test_a_date_filter_round_trips_through_the_fingerprint() -> None:
    assert run_history.filter_fingerprint(HistoryFilter(since=date(2026, 10, 1))) != run_history.filter_fingerprint(
        HistoryFilter(since=date(2026, 10, 2)),
    )
