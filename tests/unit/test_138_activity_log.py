"""138 T003/T004: ActivityLog guarantees G1-G11 and data-model invariants 1-7.

Contract: specs/138-dashboard-activity-feed/contracts/activity-event.md
"""
from __future__ import annotations

from coordinare.services.activity_log import ActivityLog

# ---------------------------------------------------------------------------
# G1, G5 — dedup and monotonic seq
# ---------------------------------------------------------------------------


def test_g1_duplicate_returns_none_and_appends_nothing() -> None:
    log = ActivityLog()
    first = log.record(activity_type="progress", card_id="C1", stage="implementing", text="hi")
    second = log.record(activity_type="progress", card_id="C1", stage="implementing", text="hi")
    assert first is not None
    assert second is None
    assert len(log.snapshot()) == 1


def test_g5_seq_strictly_increases_and_is_never_reused() -> None:
    log = ActivityLog(maxlen=5)
    for i in range(20):
        log.record(activity_type="progress", card_id="C1", text=f"line {i}")
    seqs = [e["seq"] for e in log.snapshot()]
    assert seqs == sorted(seqs)
    assert len(set(seqs)) == len(seqs)
    # Eviction must not rewind the counter.
    assert seqs[-1] == 19


# ---------------------------------------------------------------------------
# G2, G3 — truncation before the dedup key
# ---------------------------------------------------------------------------


def test_g2_g3_text_truncated_prefix_preserved_and_flagged() -> None:
    log = ActivityLog()
    entry = log.record(activity_type="progress", card_id="C1", text="x" * 500)
    assert entry is not None
    assert len(entry.text) == 200
    assert entry.text.startswith("x" * 200)
    assert entry.truncated is True


def test_g2_card_title_truncated_to_80() -> None:
    log = ActivityLog()
    entry = log.record(activity_type="progress", card_id="C1", card_title="t" * 300, text="a")
    assert entry is not None
    assert len(entry.card_title) == 80


def test_g2_truncation_precedes_dedup_key() -> None:
    """Two texts differing only past the truncation point are the same event."""
    log = ActivityLog()
    log.record(activity_type="progress", card_id="C1", text="y" * 200 + "AAA")
    dup = log.record(activity_type="progress", card_id="C1", text="y" * 200 + "BBB")
    assert dup is None


def test_short_text_is_not_flagged_truncated() -> None:
    log = ActivityLog()
    entry = log.record(activity_type="progress", card_id="C1", text="short")
    assert entry is not None
    assert entry.truncated is False


# ---------------------------------------------------------------------------
# G4 — retention cap, oldest-first eviction
# ---------------------------------------------------------------------------


def test_g4_retention_cap_evicts_oldest_first() -> None:
    log = ActivityLog(maxlen=10)
    for i in range(25):
        log.record(activity_type="progress", card_id="C1", text=f"line {i}")
    entries = log.snapshot()
    assert len(entries) == 10
    assert entries[0]["text"] == "line 15"
    assert entries[-1]["text"] == "line 24"


def test_snapshot_is_oldest_first() -> None:
    log = ActivityLog()
    log.record(activity_type="progress", card_id="C1", text="first")
    log.record(activity_type="progress", card_id="C1", text="second")
    texts = [e["text"] for e in log.snapshot()]
    assert texts == ["first", "second"]


# ---------------------------------------------------------------------------
# G6, G7 — per-card seen-set bound
# ---------------------------------------------------------------------------


def test_g6_seen_set_bounded_per_card() -> None:
    log = ActivityLog(max_seen_per_card=16)
    for i in range(200):
        log.record(activity_type="progress", card_id="C1", text=f"line {i}")
    assert len(log._seen["C1"]) <= 16


def test_g7_new_event_still_appends_after_seen_eviction() -> None:
    log = ActivityLog(max_seen_per_card=4)
    for i in range(20):
        assert log.record(activity_type="progress", card_id="C1", text=f"line {i}") is not None


def test_seen_sets_are_per_card() -> None:
    log = ActivityLog()
    assert log.record(activity_type="progress", card_id="C1", text="same") is not None
    assert log.record(activity_type="progress", card_id="C2", text="same") is not None


# ---------------------------------------------------------------------------
# G8 — forget_card
# ---------------------------------------------------------------------------


def test_g8_forget_card_drops_bookkeeping_not_entries() -> None:
    log = ActivityLog()
    log.record(activity_type="progress", card_id="C1", text="one")
    log.forget_card("C1")
    assert len(log.snapshot()) == 1
    assert "C1" not in log._seen
    # Bookkeeping released, so the same event is recordable again.
    assert log.record(activity_type="progress", card_id="C1", text="one") is not None


def test_g8_forget_unknown_card_is_a_noop() -> None:
    log = ActivityLog()
    log.forget_card("nope")


# ---------------------------------------------------------------------------
# G9, G10 — malformed input never raises
# ---------------------------------------------------------------------------


def test_g9_missing_attribution_still_appends() -> None:
    log = ActivityLog()
    entry = log.record(activity_type="progress", text="orphan")
    assert entry is not None
    assert entry.card_id == ""
    assert entry.card_number is None
    assert entry.stage == ""


def test_g10_non_string_input_does_not_raise() -> None:
    log = ActivityLog()
    entry = log.record(
        activity_type="progress",
        card_id=None,  # type: ignore[arg-type]
        card_number="not-an-int",  # type: ignore[arg-type]
        card_title=None,  # type: ignore[arg-type]
        text={"weird": True},  # type: ignore[arg-type]
    )
    assert entry is not None
    assert entry.card_id == ""
    assert entry.card_number is None
    assert isinstance(entry.text, str)


def test_g10_record_many_tolerates_junk_items() -> None:
    log = ActivityLog()
    appended = log.record_many([
        {"activity_type": "progress", "text": "good"},
        "not a mapping",  # type: ignore[list-item]
        None,  # type: ignore[list-item]
        {"activity_type": "tool_use", "text": "also good"},
    ])
    assert [e.text for e in appended] == ["good", "also good"]


def test_record_many_returns_only_appended() -> None:
    log = ActivityLog()
    items = [
        {"activity_type": "progress", "card_id": "C1", "text": "a"},
        {"activity_type": "progress", "card_id": "C1", "text": "a"},
        {"activity_type": "progress", "card_id": "C1", "text": "b"},
    ]
    appended = log.record_many(items)
    assert [e.text for e in appended] == ["a", "b"]


# ---------------------------------------------------------------------------
# G11 — sink fan-out
# ---------------------------------------------------------------------------


def test_g11_sink_receives_exactly_the_appended_entries() -> None:
    log = ActivityLog()
    seen: list[list] = []
    log.sink = seen.append
    entry = log.record(activity_type="progress", card_id="C1", text="a")
    assert seen == [[entry]]

    batch = log.record_many([
        {"activity_type": "progress", "card_id": "C1", "text": "a"},  # duplicate
        {"activity_type": "progress", "card_id": "C1", "text": "b"},
    ])
    assert seen[1] == batch
    assert [e.text for e in seen[1]] == ["b"]


def test_g11_sink_never_called_with_empty_list() -> None:
    log = ActivityLog()
    calls: list[list] = []
    log.sink = calls.append
    log.record(activity_type="progress", card_id="C1", text="a")
    log.record(activity_type="progress", card_id="C1", text="a")  # suppressed
    log.record_many([])
    log.record_many([{"activity_type": "progress", "card_id": "C1", "text": "a"}])  # suppressed
    assert len(calls) == 1


def test_g11_raising_sink_neither_propagates_nor_blocks_the_append() -> None:
    log = ActivityLog()

    def _boom(_entries: list) -> None:
        raise RuntimeError("transport is down")

    log.sink = _boom
    entry = log.record(activity_type="progress", card_id="C1", text="a")
    assert entry is not None
    assert len(log.snapshot()) == 1


# ---------------------------------------------------------------------------
# T004 — named behaviour tests
# ---------------------------------------------------------------------------


def test_wedged_agent_reemits_nothing() -> None:
    """SC-009: a backend re-reporting its whole accumulated list adds nothing."""
    log = ActivityLog()
    batch = [
        {"activity_type": "tool_use", "card_id": "C1", "stage": "implementing", "text": f"Edit f{i}.py"}
        for i in range(20)
    ]
    assert len(log.record_many(batch)) == 20
    for _ in range(100):
        assert log.record_many(batch) == []
    assert len(log.snapshot()) == 20


def test_memory_ceiling_under_flood() -> None:
    """SC-013: 100k oversized records leave <=2000 entries, each <=200 chars."""
    log = ActivityLog()
    for i in range(100_000):
        log.record(activity_type="progress", card_id=f"C{i % 20}", text=f"{i} " + "z" * 4000)
    entries = log.snapshot()
    assert len(entries) == 2000
    assert all(len(e["text"]) <= 200 for e in entries)
    assert all(len(log._seen[cid]) <= 256 for cid in log._seen)


def test_timestamp_not_in_dedup_key() -> None:
    """The same event observed at two different times appends exactly once."""
    log = ActivityLog()
    first = log.record(activity_type="progress", card_id="C1", text="same")
    assert first is not None
    second = log.record(activity_type="progress", card_id="C1", text="same")
    assert second is None
    assert len(log.snapshot()) == 1
    key = "progress|C1||same"
    assert key in log._seen["C1"]


def test_serialised_entry_shape_matches_the_wire_contract() -> None:
    log = ActivityLog()
    log.record(
        activity_type="tool_use",
        card_id="PVTI_x",
        card_number=142,
        card_title="Add retry budget",
        stage="implementer",
        text="Edit notify.py",
    )
    (entry,) = log.snapshot()
    assert set(entry) == {
        "seq", "timestamp", "card_id", "card_number", "card_title",
        "stage", "activity_type", "text", "truncated",
    }
    assert entry["timestamp"].endswith("+00:00")
    assert entry["card_number"] == 142
