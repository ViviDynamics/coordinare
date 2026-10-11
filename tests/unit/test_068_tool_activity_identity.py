"""Distinct tool executions remain visible without turning replays into activity."""
from __future__ import annotations

from coordinare.graph.nodes.monitor.activity import _record_activity_batch
from coordinare.services.activity_log import ActivityLog


def test_distinct_tool_calls_survive_and_accumulated_replay_is_suppressed() -> None:
    log = ActivityLog()
    state = {"activity_log": log, "agent_dispatch": {"session_id": "session-1"}}
    first = {"type": "tool_use", "text": "Bash", "timestamp": "2026-01-01T00:00:00Z"}
    second = {**first, "timestamp": "2026-01-01T00:00:10Z"}
    _record_activity_batch(state, [first], card_id="card-1", stage="reviewing")
    initial = log.snapshot()[0]
    _record_activity_batch(state, [first, second], card_id="card-1", stage="reviewing")
    entries = log.snapshot()
    assert len(entries) == 2
    assert entries[0] == initial
    assert entries[1]["seq"] > initial["seq"]
    _record_activity_batch(state, [first, second], card_id="card-1", stage="reviewing")
    assert log.snapshot() == entries


def test_unidentified_tool_replay_remains_conservative() -> None:
    log = ActivityLog()
    state = {"activity_log": log}
    events = [{"type": "tool_use", "text": "Bash"}] * 2
    _record_activity_batch(state, events, card_id="card-1", stage="reviewing")
    assert len(log.snapshot()) == 1


def test_identical_progress_with_different_source_times_remains_suppressed() -> None:
    log = ActivityLog()
    state = {"activity_log": log}
    events = [
        {"type": "progress", "text": "Working", "timestamp": "2026-01-01T00:00:00Z"},
        {"type": "progress", "text": "Working", "timestamp": "2026-01-01T00:00:10Z"},
    ]
    _record_activity_batch(state, events, card_id="card-1", stage="reviewing")
    assert len(log.snapshot()) == 1
