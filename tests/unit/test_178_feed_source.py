"""178: each feed row keeps the kind of the backend event it came from.

A stray line showed up as the agent's narration between tool rows. The raw
``output`` event (a backend line, not model text) was mapped to ``progress``,
which the feed draws as the agent speaking. These tests pin the corrected
mapping, that a raw line is stored under its own kind, and that the event's
transcript record travels with the entry so an operator can open the source.
"""

from __future__ import annotations

from typing import Any

import pytest

from coordinare.graph.nodes.monitor.activity import _feed_fields, _record_activity_batch
from coordinare.services.activity_log import ActivityLog
from coordinare.services.workflow_step import is_step_event

_CARD = {"id": "PVTI_178", "issue_number": 178, "title": "Stray feed text"}


def _state(log: ActivityLog) -> dict[str, Any]:
    return {
        "activity_log": log,
        "current_card": dict(_CARD),
        "agent_dispatch": {"session_id": "sess-178", "performer_id": "closer"},
    }


@pytest.mark.parametrize(
    ("event_type", "expected"),
    [("output", "output"), ("progress", "progress"), ("thinking", "thinking")],
)
def test_each_source_keeps_its_own_feed_kind(event_type: str, expected: str) -> None:
    assert _feed_fields({"type": event_type, "text": "words"}) == {"activity_type": expected}


def test_an_unrecognised_event_type_is_a_raw_output_line_in_the_daemon_too() -> None:
    assert _feed_fields({"type": "mystery_kind", "text": "stray"}) == {"activity_type": "output"}
    assert _feed_fields({"text": "no type at all"}) == {"activity_type": "output"}


def test_an_unrecognised_event_is_never_a_workflow_step() -> None:
    assert is_step_event({"type": "mystery_kind", "text": "Step 1: plan"}) is False


def test_a_raw_output_line_is_not_mapped_to_narration() -> None:
    fields = _feed_fields({"type": "output", "text": "stray line"})
    assert fields["activity_type"] != "progress"


def test_a_batch_stores_a_raw_line_under_its_own_kind_with_its_record() -> None:
    log = ActivityLog()
    _record_activity_batch(
        _state(log),
        [
            {
                "type": "progress",
                "text": "No inline review threads.",
                "timestamp": "t1",
                "transcript_seq": 41,
            },
            {"type": "output", "text": "stray line", "timestamp": "t2", "transcript_seq": 42},
        ],
        card_id="PVTI_178",
        stage="closing_review",
    )
    narration, raw = log.snapshot()
    assert (narration["activity_type"], narration["transcript_seq"]) == ("progress", 41)
    assert (raw["activity_type"], raw["text"], raw["transcript_seq"]) == (
        "output",
        "stray line",
        42,
    )
