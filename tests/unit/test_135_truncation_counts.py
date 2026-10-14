"""135: every cut the activity log makes records what it removed.

The feed can only say "text clipped at 200 characters (1,040 hidden)" if the
daemon kept the original length, and "Showing 40 of 312 updates" only if it kept
how many updates a stream produced past its budget. These tests pin those counts
at each cut: the per-entry text clip, the summary clip, the per-stream update
cap (including the live re-send of its marker), and the bounded log.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from coordinare.lib.redaction import redact_command_text
from coordinare.services.activity_log import (
    MAX_STREAM_ENTRIES,
    MAX_SUMMARY,
    MAX_TEXT,
    ActivityEntry,
    ActivityLog,
)

_CARD = "PVTI_135"
_SESSION = "session-135"
_PERFORMER = "performer-135"


class _Clock:
    """A clock the test moves by hand."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def _delta(log: ActivityLog, index: int, stream: str = "st135") -> ActivityEntry | None:
    return log.record(
        activity_type="progress", card_id=_CARD, stage="implementing", text=f"w{index} ",
        session_id=_SESSION, performer_id=_PERFORMER, is_delta=True, stream_id=stream,
    )


def test_a_clipped_text_records_its_original_length() -> None:
    log = ActivityLog()
    entry = log.record(activity_type="progress", card_id=_CARD, text="x" * 1240)
    assert entry is not None
    assert len(entry.text) == MAX_TEXT
    assert entry.truncated is True
    assert entry.text_length == 1240
    assert log.snapshot()[0]["text_length"] == 1240


def test_an_uncut_text_records_its_length_and_is_not_flagged() -> None:
    log = ActivityLog()
    entry = log.record(activity_type="progress", card_id=_CARD, text="short")
    assert entry is not None
    assert entry.truncated is False
    assert entry.text_length == len("short")


def test_a_clipped_summary_records_its_original_length() -> None:
    log = ActivityLog()
    entry = log.record(
        activity_type="tool_use", card_id=_CARD, text="Bash", summary="s" * (MAX_SUMMARY + 90),
    )
    assert entry is not None
    assert len(entry.summary) == MAX_SUMMARY
    assert entry.summary_length == MAX_SUMMARY + 90


def test_the_counts_are_the_redacted_lengths_the_log_stores() -> None:
    """Redaction runs before the clip, so the recorded length is the text the log held."""
    log = ActivityLog()
    raw = "run with token ghp_" + "A" * 36 + " " + "y" * 300
    entry = log.record(activity_type="progress", card_id=_CARD, text=raw)
    assert entry is not None
    assert entry.text_length == len(redact_command_text(raw))


def test_a_stream_under_its_budget_records_no_hidden_updates() -> None:
    log = ActivityLog()
    for i in range(10):
        _delta(log, i)
    assert all(e["hidden_updates"] is None for e in log.snapshot())
    assert not [e for e in log.snapshot() if e["activity_type"] == "stream_truncated"]


def test_a_stream_past_its_budget_counts_what_it_hid_on_its_marker() -> None:
    log = ActivityLog()
    total = MAX_STREAM_ENTRIES + 5
    for i in range(total):
        _delta(log, i)
    entries = log.snapshot()
    markers = [e for e in entries if e["activity_type"] == "stream_truncated"]
    shown = [e for e in entries if e["is_delta"]]
    assert len(markers) == 1
    assert len(shown) == MAX_STREAM_ENTRIES
    # The marker stands in for the update that tripped the cap, so the hidden
    # count is every update past the budget, including that one.
    assert markers[0]["hidden_updates"] == total - MAX_STREAM_ENTRIES


def test_the_marker_count_is_re_sent_on_a_schedule_as_the_stream_keeps_flowing() -> None:
    clock = _Clock()
    log = ActivityLog()
    log._clock = clock
    sent: list[dict[str, Any]] = []
    log.sink = lambda entries: sent.extend(e.to_dict() for e in entries)
    for i in range(MAX_STREAM_ENTRIES + 3):
        _delta(log, i)
    markers = [e for e in sent if e["activity_type"] == "stream_truncated"]
    # The announcement (1 hidden) and the power of two (2). Hidden 3 waits for a second or the flush.
    assert [m["hidden_updates"] for m in markers] == [1, 2]
    log.flush_pending_markers()
    assert [m["hidden_updates"] for m in sent if m["activity_type"] == "stream_truncated"] == [1, 2, 3]
    # A re-send carries the marker's own seq, so a connected feed redraws it in place.
    assert len({m["seq"] for m in sent if m["activity_type"] == "stream_truncated"}) == 1
    # The marker keeps its one slot in the log: a re-send replaces it, never adds one.
    assert len([e for e in log.snapshot() if e["activity_type"] == "stream_truncated"]) == 1


def test_dropped_deltas_do_not_grow_the_log() -> None:
    log = ActivityLog()
    for i in range(MAX_STREAM_ENTRIES + 50):
        _delta(log, i)
    assert len(log.snapshot()) == MAX_STREAM_ENTRIES + 1


def test_the_log_counts_entries_it_dropped_from_its_bound() -> None:
    log = ActivityLog(maxlen=5)
    for i in range(8):
        log.record(activity_type="progress", card_id=_CARD, text=f"line {i}")
    entries = log.snapshot()
    assert len(entries) == 5
    # The newest entry reports every drop so far. An older entry reports the drops
    # as they stood when it was recorded, so the feed reads the newest value.
    assert entries[-1]["log_dropped"] == 3
    assert entries[0]["log_dropped"] == 0


def test_an_entry_without_135_counts_reports_none_so_the_feed_shows_nothing() -> None:
    """An entry built without the 135 fields reports None, which the feed reads as "not recorded"."""
    entry = ActivityEntry(
        seq=0, timestamp=datetime.now(UTC), card_id=_CARD, card_number=None, card_title="",
        stage="", activity_type="progress", text="old", truncated=True,
    )
    assert entry.text_length is None
    assert entry.summary_length is None
    assert entry.hidden_updates is None
    assert entry.log_dropped is None
