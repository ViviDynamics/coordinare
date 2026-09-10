"""327: one runaway delta stream must not evict the rest of the feed.

Live incident (website #160): a degenerate model loop emitted ~10 delta events
per second. Each became its own entry in the process-wide 2000-entry activity
log, so within minutes the log held nothing but that one stream and every other
card's activity had been evicted -- which is precisely what made the loop hard
to see. The dashboard already concatenates consecutive same-stream deltas into
one displayed block, so bounding them server-side costs no information that was
being shown.
"""
from __future__ import annotations

from coordinare.services.activity_log import MAX_STREAM_ENTRIES, ActivityLog


def _flood(
    log: ActivityLog,
    n: int,
    *,
    card_id: str = "card-loop",
    stream: str = "msg-1",
    offset: int = 0,
) -> None:
    """`offset` keeps source ids unique across calls, so the existing dedup does
    not silently swallow a second batch and hide what a test means to check."""
    for i in range(offset, offset + n):
        log.record(
            activity_type="progress",
            card_id=card_id,
            stage="architecting",
            text=("Go. Tool. No. " * 2)[i % 14 : (i % 14) + 12],
            is_delta=True,
            stream_id=stream,
            source_event_id=f"ev-{i}",
        )


def test_one_stream_cannot_evict_other_cards() -> None:
    """The regression: a neighbouring card's entry must survive the flood."""
    log = ActivityLog(maxlen=500)
    log.record(activity_type="dispatched", card_id="card-other", text="other card started")
    _flood(log, 5000)
    kinds = [e["card_id"] for e in log.snapshot()]
    assert "card-other" in kinds, "the flood evicted every other card from the feed"


def test_single_stream_footprint_is_bounded() -> None:
    log = ActivityLog(maxlen=5000)
    _flood(log, 5000)
    deltas = [e for e in log.snapshot() if e["stream_id"] == "msg-1" and e["is_delta"]]
    assert len(deltas) <= MAX_STREAM_ENTRIES


def test_truncation_is_surfaced_exactly_once() -> None:
    """Dropping output silently would be dishonest -- say so, but only once."""
    log = ActivityLog(maxlen=5000)
    _flood(log, 5000)
    markers = [e for e in log.snapshot() if e["activity_type"] == "stream_truncated"]
    assert len(markers) == 1
    assert "msg-1" in markers[0]["text"] or "truncated" in markers[0]["text"].lower()


def test_each_stream_gets_its_own_budget() -> None:
    """A new message must stream normally after an earlier one hit the cap."""
    log = ActivityLog(maxlen=8000)
    _flood(log, 3000, stream="msg-1")
    _flood(log, 5, stream="msg-2")
    second = [e for e in log.snapshot() if e["stream_id"] == "msg-2"]
    assert len(second) == 5


def test_non_delta_entries_are_never_capped() -> None:
    """Discrete events are not a stream and must not be suppressed."""
    log = ActivityLog(maxlen=5000)
    for i in range(MAX_STREAM_ENTRIES + 50):
        log.record(activity_type="tool_use", card_id="c", text=f"ran step {i}")
    kept = [e for e in log.snapshot() if e["activity_type"] == "tool_use"]
    assert len(kept) == MAX_STREAM_ENTRIES + 50


def test_stream_bookkeeping_does_not_grow_without_bound() -> None:
    """Per-stream counters must not become their own leak."""
    log = ActivityLog(maxlen=5000)
    for s in range(500):
        _flood(log, 2, stream=f"stream-{s}")
    assert len(log._stream_counts) <= 128


# ---------------------------------------------------------------------------
# Review follow-ups (PR #328 adversarial review)
# ---------------------------------------------------------------------------


def test_capped_stream_keeps_its_budget_under_lru_pressure() -> None:
    """A capped stream must NOT get a second budget after being pushed out of
    the bookkeeping by other streams.

    Found in review: eviction dropped the oldest entry unconditionally, so a
    looping message that fell out of `_stream_counts` and then reappeared was
    handed a fresh cap and flooded the log all over again.
    """
    log = ActivityLog(maxlen=20000, max_stream_entries=10, max_tracked_streams=8)
    _flood(log, 40, stream="loop")  # hits the cap
    first = len([e for e in log.snapshot() if e["stream_id"] == "loop" and e["is_delta"]])
    assert first == 10
    # Enough other streams to overflow the bookkeeping several times over.
    for s in range(60):
        _flood(log, 2, stream=f"other-{s}")
    _flood(log, 40, stream="loop", offset=100)  # the same message comes back
    total = len([e for e in log.snapshot() if e["stream_id"] == "loop" and e["is_delta"]])
    assert total == 10, f"capped stream reflooded: {total} entries"


def test_active_stream_is_not_evicted_from_bookkeeping() -> None:
    """The LRU keep-alive (pop + re-insert on every touch) must actually work.

    Found in review: removing the move-to-end left every test passing, so the
    behaviour was unprotected.
    """
    log = ActivityLog(maxlen=20000, max_stream_entries=10, max_tracked_streams=4)
    for i in range(30):
        # "hot" is touched throughout, so it must never be the one evicted.
        _flood(log, 1, stream="hot", offset=i)
        _flood(log, 1, stream=f"cold-{i}")
    assert "hot" in log._stream_counts
    kept = len([e for e in log.snapshot() if e["stream_id"] == "hot" and e["is_delta"]])
    assert kept == 10, f"hot stream lost its budget and reflooded: {kept}"


def test_cap_is_the_configured_number_not_merely_bounded() -> None:
    """Assert the EXACT retained count against an independently chosen cap.

    Found in review: the original assertion compared against the same constant
    the implementation used, so any value passed.
    """
    log = ActivityLog(maxlen=20000, max_stream_entries=7)
    _flood(log, 500)
    deltas = [e for e in log.snapshot() if e["stream_id"] == "msg-1" and e["is_delta"]]
    assert len(deltas) == 7


def test_marker_not_duplicated_under_dedup_pressure() -> None:
    """One stream, one truncation marker, even when the per-card dedup cache
    has rotated far past the marker's key."""
    log = ActivityLog(maxlen=20000, max_stream_entries=5, max_seen_per_card=8)
    _flood(log, 200, stream="loop")
    for s in range(40):
        _flood(log, 2, stream=f"other-{s}")
    _flood(log, 200, stream="loop", offset=500)
    markers = [
        e
        for e in log.snapshot()
        if e["activity_type"] == "stream_truncated" and "loop" in e["text"]
    ]
    assert len(markers) == 1, f"{len(markers)} markers for one stream"
