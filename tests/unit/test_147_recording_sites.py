"""#147: the lifecycle events are recorded where they happen, and the log survives the snapshot.

Each recording site is driven directly: the verdict phase, the PR artefact write-through and the
override. The snapshot round trip checks the v35 field. The sites' full graph runs are in the
existing node tests.
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from coordinare.graph.nodes.monitor.artefacts import _record_pr_artefacts
from coordinare.graph.nodes.monitor.body import _BodyCtx, _phase_verdict_record
from coordinare.graph.nodes.monitor.verdict import _apply_pending_override
from coordinare.services.card_lifecycle import CardLifecycleLog
from coordinare.state_store import WorkflowSnapshot

SYMPHONY = "alpha-sym"
CARD = "PVTI_1"
HEAD = "9f8e7d6c5b4a" + "0" * 28


def _state(log: CardLifecycleLog, **extra: Any) -> dict[str, Any]:
    state: dict[str, Any] = {
        "card_lifecycle": log,
        "current_symphony": SYMPHONY,
        "performer_stage": "reviewing",
        "current_card": {"id": CARD},
    }
    state.update(extra)
    return state


def _ctx(stage: str, marker: str, status: dict[str, Any] | None = None) -> _BodyCtx:
    return _BodyCtx(stage=stage, marker=marker, card_id=CARD, status=status or {"head_after": HEAD})


async def _verdict(state: dict[str, Any], ctx: _BodyCtx) -> None:
    assert await _phase_verdict_record(state, ctx) is None


@pytest.mark.asyncio
async def test_a_passing_verdict_is_recorded_once_for_its_head() -> None:
    log = CardLifecycleLog()
    state = _state(log)
    await _verdict(state, _ctx("reviewing", "approved"))
    await _verdict(state, _ctx("reviewing", "approved"))
    events = log.events(SYMPHONY, CARD)
    assert [e["kind"] for e in events] == ["verdict"]
    assert events[0]["stage"] == "reviewing"
    assert events[0]["detail"] == {"result": "pass", "marker": "approved", "head": HEAD[:12]}


@pytest.mark.asyncio
async def test_a_failing_verdict_is_recorded_as_a_fail() -> None:
    log = CardLifecycleLog()
    await _verdict(_state(log), _ctx("reviewing", "changes_requested"))
    assert log.events(SYMPHONY, CARD)[0]["detail"]["result"] == "fail"


@pytest.mark.asyncio
async def test_a_marker_that_is_neither_a_pass_nor_a_failure_is_not_a_verdict() -> None:
    log = CardLifecycleLog()
    await _verdict(_state(log), _ctx("reviewing", "partial_progress"))
    await _verdict(_state(log), _ctx("implementing", "approved"))
    assert log.events(SYMPHONY, CARD) == []


@pytest.mark.asyncio
async def test_the_verdict_phase_is_a_no_op_without_a_log() -> None:
    state: dict[str, Any] = {"current_symphony": SYMPHONY}
    await _verdict(state, _ctx("reviewing", "approved"))
    assert "card_lifecycle" not in state


def test_a_pr_is_recorded_once_with_its_number_and_url() -> None:
    log = CardLifecycleLog()
    state = _state(log, performer_stage="implementing")
    url = "https://github.com/acme/repo/pull/9"
    _record_pr_artefacts(state, {"pr_url": url, "head_after": HEAD})
    _record_pr_artefacts(state, {"pr_url": url, "head_after": HEAD})
    events = log.events(SYMPHONY, CARD)
    assert [e["kind"] for e in events] == ["pr_opened"]
    assert events[0]["stage"] == "implementing"
    assert events[0]["detail"] == {"pr_number": 9, "pr_url": url}


def test_an_applied_override_is_recorded_once_by_its_control() -> None:
    log = CardLifecycleLog()
    state = _state(log, pending_override={"action": "skip", "control_id": "ctl-1", "target_stage": None})
    _apply_pending_override(state)
    # A redelivered command with the same control id is not a second override.
    state["pending_override"] = {"action": "skip", "control_id": "ctl-1", "target_stage": None}
    _apply_pending_override(state)
    events = log.events(SYMPHONY, CARD)
    assert [e["kind"] for e in events] == ["override"]
    assert events[0]["detail"]["action"] == "skip"


def test_the_snapshot_carries_the_log_and_loads_it_back() -> None:
    log = CardLifecycleLog()
    log.record(SYMPHONY, CARD, "merged", stage=None, at="2026-10-14T09:00:00+00:00", key="merged")
    snapshot = WorkflowSnapshot(snapshot_at=datetime(2026, 10, 14, 9, 0, tzinfo=UTC), phase="idle",
                                card_lifecycle=log.to_snapshot())
    loaded = WorkflowSnapshot.model_validate_json(snapshot.model_dump_json())
    restored = CardLifecycleLog()
    restored.load(loaded.card_lifecycle)
    assert [e["kind"] for e in restored.events(SYMPHONY, CARD)] == ["merged"]
