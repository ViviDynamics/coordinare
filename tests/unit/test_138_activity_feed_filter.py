"""138 US3: snapshot ordering, per-card selection, and what the cap guarantees.

T045 (ordering + limit + selection), T046 (retention window — FR-020, SC-005).
"""
from __future__ import annotations

from coordinare.services.activity_log import ActivityLog

# ---------------------------------------------------------------------------
# T045 — ordering, limit, per-card selection
# ---------------------------------------------------------------------------


def test_snapshot_is_oldest_first_by_ascending_seq() -> None:
    """Wire order (contract §1.2 invariant 1). Newest-first is a client rule."""
    log = ActivityLog()
    for i in range(10):
        log.record(activity_type="progress", card_id="C1", text=f"line {i}")
    entries = log.snapshot()
    assert [e["seq"] for e in entries] == sorted(e["seq"] for e in entries)
    assert entries[0]["text"] == "line 0"
    assert entries[-1]["text"] == "line 9"


def test_limit_returns_the_newest_n_still_oldest_first() -> None:
    log = ActivityLog()
    for i in range(10):
        log.record(activity_type="progress", card_id="C1", text=f"line {i}")
    entries = log.snapshot(limit=3)
    assert [e["text"] for e in entries] == ["line 7", "line 8", "line 9"]


def test_limit_zero_and_oversized_limit() -> None:
    log = ActivityLog()
    for i in range(3):
        log.record(activity_type="progress", card_id="C1", text=f"line {i}")
    assert log.snapshot(limit=0) == []
    assert len(log.snapshot(limit=999)) == 3


def test_per_card_selection_and_clearing_restores_all() -> None:
    """The filter the client applies: select one card, then clear it."""
    log = ActivityLog()
    for i in range(4):
        log.record(activity_type="progress", card_id="C1", text=f"a{i}")
        log.record(activity_type="progress", card_id="C2", text=f"b{i}")
    everything = log.snapshot()
    selected = [e for e in everything if e["card_id"] == "C1"]
    assert len(selected) == 4
    assert {e["card_id"] for e in selected} == {"C1"}
    assert [e["seq"] for e in selected] == sorted(e["seq"] for e in selected)
    # Clearing the filter is just dropping the predicate — history is untouched.
    assert len(log.snapshot()) == 8


# ---------------------------------------------------------------------------
# T046 — what the retention cap actually guarantees
# ---------------------------------------------------------------------------


def _flood(log: ActivityLog, *, polls: int, per_poll: int, card: str = "C1") -> None:
    for poll in range(polls):
        log.record_many([
            {"activity_type": "progress", "card_id": card, "text": f"p{poll} e{i}"}
            for i in range(per_poll)
        ])


def test_sixty_minute_window_fits_at_the_documented_rate() -> None:
    """Assumption 2: ~120 polls x 16 distinct events fits inside 2000 entries."""
    log = ActivityLog()
    _flood(log, polls=120, per_poll=16)   # 1920
    entries = log.snapshot()
    assert len(entries) == 1920
    assert entries[0]["text"] == "p0 e0", "the start of the window was trimmed"


def test_one_event_per_poll_more_trims_the_oldest_end() -> None:
    log = ActivityLog()
    _flood(log, polls=120, per_poll=17)   # 2040
    entries = log.snapshot()
    assert len(entries) == 2000
    assert entries[0]["text"] != "p0 e0"


def test_eviction_past_the_cap_is_oldest_first() -> None:
    log = ActivityLog()
    for i in range(2500):
        log.record(activity_type="progress", card_id="C1", text=f"line {i}")
    entries = log.snapshot()
    assert len(entries) == 2000
    assert entries[0]["text"] == "line 500"
    assert entries[-1]["text"] == "line 2499"


def test_cap_is_a_shared_budget_across_cards() -> None:
    """At 20 cards a chatty card ages out a quiet card's older entries.

    2000 is a memory budget, not 20 x 100 — the per-card 100-entry source cap
    bounds what is in flight at one instant, while the feed accumulates every
    distinct event over time. History depth is what gives; the ceiling holds.
    """
    log = ActivityLog()
    log.record(activity_type="progress", card_id="QUIET", text="the only thing it ever said")
    _flood(log, polls=200, per_poll=15, card="CHATTY")   # 3000 entries

    entries = log.snapshot()
    assert len(entries) == 2000
    assert not [e for e in entries if e["card_id"] == "QUIET"], (
        "expected the quiet card's entry to age out — the cap is shared"
    )
