"""#211: an activity row carries the transcript record its event was built from, when the performer sends one."""
from __future__ import annotations

from typing import Any

import pytest

from coordinare.graph.nodes.monitor.activity import _record_activity_batch
from coordinare.services.activity_log import (
    MAX_TRANSCRIPT_SEQ,
    ActivityLog,
    EventLink,
    transcript_seq_or_none,
)

CARD = "PVTI_211"
STAGE = "implementing"
SEQ = 7


def _event(**extra: Any) -> dict[str, Any]:
    return {"type": "progress", "text": "reading the spec", "timestamp": "2026-10-14T10:00:00.123+00:00", **extra}


def _state(log: ActivityLog) -> dict[str, Any]:
    return {
        "activity_log": log,
        "current_card": {"issue_number": 211, "title": "Exact transcript links"},
        "agent_dispatch": {"session_id": "sess-1", "performer_id": STAGE},
    }


def _record_one(event: dict[str, Any]) -> ActivityLog:
    log = ActivityLog()
    _record_activity_batch(_state(log), [event], card_id=CARD, stage=STAGE)
    return log


def test_an_event_with_a_seq_carries_it_into_the_entry_and_its_dict() -> None:
    log = _record_one(_event(transcript_seq=SEQ))
    entry = log.snapshot()
    assert len(entry) == 1
    assert entry[0]["transcript_seq"] == SEQ
    assert entry[0]["event_at"] == "2026-10-14T10:00:00.123+00:00"


def test_an_event_without_the_key_gives_none() -> None:
    log = _record_one(_event())
    assert log.snapshot()[0]["transcript_seq"] is None


@pytest.mark.parametrize("bad", [True, False, 0, -1, MAX_TRANSCRIPT_SEQ + 1, "7", 7.0, None])
def test_a_value_that_is_not_a_transcript_seq_gives_none(bad: Any) -> None:
    log = _record_one(_event(transcript_seq=bad))
    assert log.snapshot()[0]["transcript_seq"] is None


def test_the_validator_accepts_the_ends_of_the_range_and_nothing_outside_it() -> None:
    assert transcript_seq_or_none(1) == 1
    assert transcript_seq_or_none(MAX_TRANSCRIPT_SEQ) == MAX_TRANSCRIPT_SEQ
    assert transcript_seq_or_none(0) is None
    assert transcript_seq_or_none(MAX_TRANSCRIPT_SEQ + 1) is None
    assert transcript_seq_or_none(True) is None
    assert transcript_seq_or_none("7") is None


def test_a_huge_seq_in_a_batch_is_dropped_and_the_row_still_lands() -> None:
    log = _record_one(_event(transcript_seq=MAX_TRANSCRIPT_SEQ + 1))
    rows = log.snapshot()
    assert len(rows) == 1
    assert rows[0]["transcript_seq"] is None


def test_record_with_the_keyword_validates_the_same_way() -> None:
    log = ActivityLog()
    entry = log.record(activity_type="progress", text="step", link=EventLink(transcript_seq=SEQ))
    assert entry is not None and entry.transcript_seq == SEQ
    bad = log.record(activity_type="progress", text="other", link=EventLink(transcript_seq=MAX_TRANSCRIPT_SEQ + 1))
    assert bad is not None and bad.transcript_seq is None


def test_the_seq_is_not_part_of_the_dedup_key() -> None:
    log = ActivityLog()
    appended = log.record_many([
        _event(transcript_seq=SEQ),
        _event(transcript_seq=SEQ + 1),
    ])
    assert len(appended) == 1
    assert log.snapshot()[0]["transcript_seq"] == SEQ


def test_two_events_that_differ_only_by_seq_are_one_entry_with_the_first_seq() -> None:
    log = ActivityLog()
    first = log.record_many([{"activity_type": "progress", "text": "same", "card_id": CARD,
                              "transcript_seq": SEQ}])
    second = log.record_many([{"activity_type": "progress", "text": "same", "card_id": CARD,
                               "transcript_seq": None}])
    assert len(first) == 1
    assert second == []
    assert [row["transcript_seq"] for row in log.snapshot()] == [SEQ]
