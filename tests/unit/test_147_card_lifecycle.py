"""#147: the card lifecycle log. Bounds, once-only keys, redaction, persistence and the v35 schema.

The timeline's assembly is in tests/unit/test_147_card_timeline.py, the endpoint and the board
strip in tests/unit/test_147_card_pipeline_api.py.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from coordinare.services.card_lifecycle import (
    EVENT_KINDS,
    MAX_CARDS_PER_SYMPHONY,
    MAX_EVENTS_PER_CARD,
    CardLifecycleLog,
    card_id_of,
    record_lifecycle,
    stage_of,
)
from coordinare.state_store import CURRENT_SCHEMA_VERSION, WorkflowSnapshot

SYMPHONY = "alpha-sym"
CARD = "PVTI_1"
SECRET = "ghp_" + "A" * 36
TS = "2026-10-14T09:00:00+00:00"


def _snapshot_with(**overrides: Any) -> WorkflowSnapshot:
    base: dict[str, Any] = {"snapshot_at": datetime(2026, 10, 14, 9, 0, tzinfo=UTC), "phase": "idle"}
    base.update(overrides)
    return WorkflowSnapshot(**base)


def test_schema_version_is_35_and_v34_snapshots_load_with_an_empty_log() -> None:
    assert CURRENT_SCHEMA_VERSION == 35
    snapshot = _snapshot_with(schema_version=34)
    assert snapshot.card_lifecycle == {}


def test_events_keep_their_order_and_stage_and_time() -> None:
    log = CardLifecycleLog()
    assert log.record(SYMPHONY, CARD, "dispatched", stage="implementing", at=TS)
    assert log.record(SYMPHONY, CARD, "pr_opened", stage="implementing", detail={"pr_number": 9}, at=TS)
    events = log.events(SYMPHONY, CARD)
    assert [e["kind"] for e in events] == ["dispatched", "pr_opened"]
    assert events[1]["stage"] == "implementing"
    assert events[1]["detail"] == {"pr_number": 9}
    assert events[0]["at"] == TS


def test_unknown_kinds_and_blank_cards_are_refused() -> None:
    log = CardLifecycleLog()
    assert not log.record(SYMPHONY, CARD, "not_a_kind")
    assert not log.record(SYMPHONY, "", "dispatched")
    assert log.events(SYMPHONY, CARD) == []
    assert "merged" in EVENT_KINDS


def test_a_keyed_event_is_recorded_once_per_card_and_kind() -> None:
    log = CardLifecycleLog()
    assert log.record(SYMPHONY, CARD, "review_received", key="review:7")
    assert not log.record(SYMPHONY, CARD, "review_received", key="review:7")
    assert log.record(SYMPHONY, CARD, "review_received", key="review:8")
    assert len(log.events(SYMPHONY, CARD)) == 2


def test_a_card_keeps_only_its_newest_events() -> None:
    log = CardLifecycleLog()
    for index in range(MAX_EVENTS_PER_CARD + 5):
        log.record(SYMPHONY, CARD, "dispatched", stage="implementing", detail={"n": index})
    events = log.events(SYMPHONY, CARD)
    assert len(events) == MAX_EVENTS_PER_CARD
    assert events[-1]["detail"] == {"n": MAX_EVENTS_PER_CARD + 4}


def test_a_symphony_keeps_only_its_most_recently_active_cards() -> None:
    log = CardLifecycleLog()
    base = datetime(2026, 10, 14, 9, 0, tzinfo=UTC)
    for index in range(MAX_CARDS_PER_SYMPHONY + 3):
        log.record(SYMPHONY, f"PVTI_{index}", "dispatched", stage="assessing",
                   at=(base + timedelta(seconds=index)).isoformat())
    cards = log.cards(SYMPHONY)
    assert len(cards) == MAX_CARDS_PER_SYMPHONY
    assert "PVTI_0" not in cards
    assert f"PVTI_{MAX_CARDS_PER_SYMPHONY + 2}" in cards


def test_free_text_detail_is_redacted_and_long_text_is_clipped() -> None:
    log = CardLifecycleLog()
    log.record(SYMPHONY, CARD, "human_action", detail={"action": f"skip {SECRET}", "note": "x" * 500})
    detail = log.events(SYMPHONY, CARD)[0]["detail"]
    assert SECRET not in str(detail)
    assert len(str(detail["note"])) <= 200


def test_the_log_round_trips_through_the_snapshot_payload() -> None:
    log = CardLifecycleLog()
    log.record(SYMPHONY, CARD, "verdict", stage="reviewing", detail={"result": "pass"}, key="approved:abc")
    payload = log.to_snapshot()
    restored = CardLifecycleLog()
    restored.load(payload)
    assert restored.events(SYMPHONY, CARD) == log.events(SYMPHONY, CARD)
    assert not restored.record(SYMPHONY, CARD, "verdict", stage="reviewing", key="approved:abc")


def test_a_malformed_entry_is_dropped_and_the_rest_is_kept() -> None:
    log = CardLifecycleLog()
    log.load({
        SYMPHONY: {
            CARD: [
                {"at": TS, "kind": "dispatched", "stage": "implementing", "detail": {}},
                {"at": TS, "kind": "bogus"},
                "not an event",
                {"kind": "merged"},
            ],
            7: [{"at": TS, "kind": "dispatched"}],
        },
        "other": "not a mapping",
    })
    assert [e["kind"] for e in log.events(SYMPHONY, CARD)] == ["dispatched"]
    assert log.symphonies() == [SYMPHONY]


def test_loading_a_non_mapping_empties_the_log() -> None:
    log = CardLifecycleLog()
    log.record(SYMPHONY, CARD, "dispatched", stage="implementing")
    log.load(None)
    assert log.symphonies() == []


def test_record_lifecycle_is_a_no_op_without_a_log_and_uses_the_running_symphony() -> None:
    assert not record_lifecycle({}, "dispatched", card_id=CARD)
    log = CardLifecycleLog()
    state: dict[str, Any] = {"card_lifecycle": log, "current_symphony": SYMPHONY, "performer_stage": "reviewing"}
    assert record_lifecycle(state, "dispatched", card_id=CARD, stage=stage_of(state))
    assert log.events(SYMPHONY, CARD)[0]["stage"] == "reviewing"


def test_stage_and_card_read_from_state() -> None:
    assert stage_of({"performer_stage": ""}) is None
    assert stage_of({"performer_stage": "qa"}) == "qa"
    assert card_id_of({"current_card": {"id": CARD}}) == CARD
    assert card_id_of({"current_card": None}) == ""


def test_an_unknown_event_field_is_an_error_not_a_silent_drop() -> None:
    log = CardLifecycleLog()
    with pytest.raises(TypeError, match="unknown lifecycle event field"):
        log.record(SYMPHONY, CARD, "dispatched", stag="implementing")
    assert log.events(SYMPHONY, CARD) == []
