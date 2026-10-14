"""#147 final check: the lifecycle cap never rewrites stage history, a failing live provider cannot
break a record, a run past the read budget or still in flight says so, and the timeline names what
the cap dropped.

Each test reproduces one finding and fails until it is fixed.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from coordinare.dashboard import card_pipeline
from coordinare.dashboard.card_pipeline import card_pipeline_block, transcript_runs
from coordinare.services.card_lifecycle import MAX_EVENTS_PER_CARD, CardLifecycleLog
from coordinare.services.card_timeline import (
    CardRef,
    LiveStage,
    RunIndex,
    UsageLedger,
    build_timeline,
)
from coordinare.services.run_transcript import transcript_path

SYMPHONY = "alpha-sym"
T0 = datetime(2026, 10, 14, 9, 0, 0, tzinfo=UTC)
LIFECYCLE = ["assessing", "architecting", "implementing", "reviewing", "security", "qa", "documenting", "closing_review"]


def _at(seconds: float) -> str:
    return (T0 + timedelta(seconds=seconds)).isoformat()


def _stage_statuses(log: CardLifecycleLog, card: str = "C") -> dict[str, str]:
    timeline = build_timeline(
        CardRef(SYMPHONY, card), log.events(SYMPHONY, card),
        LiveStage(current=None, lifecycle=tuple(LIFECYCLE)),
        RunIndex(runs=None, reason="off"), UsageLedger(stages=None, reason="off"),
    )
    return {stage["stage"]: stage["status"] for stage in timeline["stages"]}


# --- the 40-event cap never rewrites stage history --------------------------------------------

def test_the_probe_scenario_keeps_assessing_and_architecting_done_after_40_reviewing_verdicts() -> None:
    log = CardLifecycleLog()
    log.record(SYMPHONY, "C", "dispatched", stage="assessing", at=_at(0))
    log.record(SYMPHONY, "C", "verdict", stage="assessing", detail={"result": "pass", "marker": "ok"}, at=_at(1), key="a")
    log.record(SYMPHONY, "C", "dispatched", stage="architecting", at=_at(2))
    log.record(SYMPHONY, "C", "verdict", stage="architecting", detail={"result": "pass", "marker": "ok"}, at=_at(3), key="b")
    for index in range(MAX_EVENTS_PER_CARD):
        log.record(SYMPHONY, "C", "verdict", stage="reviewing", detail={"result": "fail", "marker": "changes_requested"},
                   at=_at(10 + index), key=f"r{index}")
    statuses = _stage_statuses(log)
    assert statuses["assessing"] == "done"
    assert statuses["architecting"] == "done"
    assert statuses["reviewing"] == "failed"


def test_the_epoch_survives_a_trim_on_the_first_event() -> None:
    log = CardLifecycleLog()
    log.record(SYMPHONY, "C3", "verdict", stage="reviewing", detail={"result": "pass"}, at=_at(0), key="first")
    for index in range(MAX_EVENTS_PER_CARD + 5):
        log.record(SYMPHONY, "C3", "verdict", stage="reviewing", detail={"result": "fail"},
                   at=_at(index + 1), key=f"x{index}")
    events = log.events(SYMPHONY, "C3")
    assert len(events) == MAX_EVENTS_PER_CARD
    assert events[0]["epoch"] == _at(0)
    assert log.revision(SYMPHONY, "C3")[0] == _at(0)


def test_a_merged_event_survives_a_trim_so_a_later_dispatch_still_starts_fresh() -> None:
    log = CardLifecycleLog()
    log.record(SYMPHONY, "C4", "dispatched", stage="implementing", at=_at(0))
    log.record(SYMPHONY, "C4", "merged", at=_at(1), key="merged")
    for index in range(MAX_EVENTS_PER_CARD):
        log.record(SYMPHONY, "C4", "verdict", stage="reviewing", detail={"result": "pass"}, at=_at(2 + index), key=f"m{index}")
    assert any(e["kind"] == "merged" for e in log.events(SYMPHONY, "C4"))
    log.record(SYMPHONY, "C4", "dispatched", stage="assessing", at=_at(10_000))
    events = log.events(SYMPHONY, "C4")
    assert [event["kind"] for event in events] == ["dispatched"]
    assert events[0]["stage"] == "assessing"


def test_the_count_of_dropped_events_is_kept_on_the_first_event_and_the_timeline_says_so() -> None:
    log = CardLifecycleLog()
    log.record(SYMPHONY, "C5", "dispatched", stage="implementing", at=_at(0))
    for index in range(MAX_EVENTS_PER_CARD + 7):
        log.record(SYMPHONY, "C5", "ci_result", stage="implementing", detail={"result": "hold"}, at=_at(index + 1),
                   key=f"ci{index}")
    events = log.events(SYMPHONY, "C5")
    assert events[0]["dropped"] == 8, "the first event counts what the cap dropped (the ci_result middle)"
    timeline = build_timeline(
        CardRef(SYMPHONY, "C5"), events, LiveStage(current=None, lifecycle=tuple(LIFECYCLE)),
        RunIndex(runs=None, reason="off"), UsageLedger(stages=None, reason="off"),
    )
    assert timeline["events_not_shown"] == 8
    assert "8 earlier events not shown" in timeline["not_recorded"]


def test_run_counts_from_events_are_a_lower_bound_once_events_were_dropped() -> None:
    log = CardLifecycleLog()
    for index in range(MAX_EVENTS_PER_CARD + 3):
        log.record(SYMPHONY, "C6", "dispatched", stage="implementing", at=_at(index))
    events = log.events(SYMPHONY, "C6")
    timeline = build_timeline(
        CardRef(SYMPHONY, "C6"), events, LiveStage(current=None, lifecycle=tuple(LIFECYCLE)),
        RunIndex(runs=None, reason="off"), UsageLedger(stages=None, reason="off"),
    )
    implementing = next(stage for stage in timeline["stages"] if stage["stage"] == "implementing")
    assert implementing["run_count_source"] == "lifecycle"
    assert implementing["run_count_at_least"] is True
    assert implementing["run_count"] >= 1


# --- a provider that raises cannot break a record ----------------------------------------------

def test_a_live_provider_that_raises_does_not_propagate_and_evicts_nothing() -> None:
    def _broken(symphony: str) -> set[str]:
        raise RuntimeError("provider down")

    log = CardLifecycleLog()
    log.set_live_provider(_broken)
    assert log.record(SYMPHONY, "KEEP", "dispatched", stage="implementing", at=_at(0)) is True
    for index in range(100 + 5):
        assert log.record(SYMPHONY, f"D{index}", "merged", at=_at(index + 1), key="merged") is True
    assert log.has_card(SYMPHONY, "KEEP"), "a card is treated as live for a trim the provider fails"
    assert log.provider_errors >= 1, "each trim whose provider fails is counted; only the first is logged"


# --- a run past the byte budget, or still in flight, says so -----------------------------------

def _header(run: str, card: str) -> dict[str, Any]:
    return {"type": "run_header", "seq": 0, "header_version": 1, "ts": _at(0), "run_id": run,
            "persona": {"name": "implementer", "version": 1, "version_sha256": "ab" * 32,
                        "changed_at": _at(0), "changed_by": "local", "source": "dashboard"},
            "performer": {"id": "p", "backend": "claude-code", "transport": "http", "endpoint": "e"},
            "model": {"model": "m"}, "card": {"id": card, "issue_number": 1, "title": "t"}}


def _write(root: Path, card: str, run: str, lines: list[dict[str, Any]]) -> None:
    path = transcript_path(root, symphony=SYMPHONY, card_id=card, run_id=run)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(line) for line in [_header(run, card), *lines]) + "\n", encoding="utf-8")


def test_a_run_past_the_read_budget_is_marked_not_read_in_the_payload(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(card_pipeline, "TIMELINE_READ_BYTES", 1_000)
    for index in range(4):
        _write(tmp_path, "C7", f"run-{index}", [
            {"ts": _at(1), "seq": 1, "type": "assistant_text", "text": "x" * 50},
            {"ts": _at(60), "seq": 2, "type": "result", "is_error": False, "text": "done"},
        ])
    runs, _reason, _truncated = transcript_runs(tmp_path, symphony=SYMPHONY, card_id="C7")
    assert runs is not None
    states = [run["read_state"] for run in runs]
    assert "budget" in states, "a run past the budget must say it was not read"
    skipped = next(run for run in runs if run["read_state"] == "budget")
    assert skipped["ended_at"] is None and skipped["outcome"] is None and skipped["duration_seconds"] is None
    timeline = build_timeline(
        CardRef(SYMPHONY, "C7"), [], LiveStage(current=None, lifecycle=tuple(LIFECYCLE)),
        RunIndex(runs=runs), UsageLedger(stages=None, reason="off"),
    )
    listed = [run for stage in timeline["stages"] for run in stage["runs"]]
    assert any(run["read_state"] == "budget" for run in listed)


def test_a_run_still_in_flight_has_no_end_time_and_is_marked_running(tmp_path: Path) -> None:
    _write(tmp_path, "C8", "run-live", [
        {"ts": _at(1), "seq": 1, "type": "assistant_text", "text": "still working"},
        {"ts": _at(20), "seq": 2, "type": "tool_call", "tool_name": "Bash", "call_id": "c1", "input": {}},
    ])
    runs, _reason, _truncated = transcript_runs(tmp_path, symphony=SYMPHONY, card_id="C8")
    assert runs is not None
    assert runs[0]["ended_at"] is None, "a run with no terminal record has not ended"
    assert runs[0]["read_state"] == "running"
    assert runs[0]["duration_seconds"] is None
    assert runs[0]["outcome"] is None


def test_a_finished_run_still_reports_its_end_and_outcome(tmp_path: Path) -> None:
    _write(tmp_path, "C9", "run-done", [
        {"ts": _at(1), "seq": 1, "type": "assistant_text", "text": "working"},
        {"ts": _at(90), "seq": 2, "type": "result", "is_error": True, "text": "failed"},
    ])
    runs, _reason, _truncated = transcript_runs(tmp_path, symphony=SYMPHONY, card_id="C9")
    assert runs is not None
    assert runs[0]["read_state"] == "done"
    assert runs[0]["ended_at"] == _at(90)
    assert runs[0]["outcome"] == "failed"
    assert runs[0]["duration_seconds"] == 90.0


# --- the snapshot's strip still follows the trimmed history ----------------------------------

def test_the_strip_reads_stage_history_after_a_trim() -> None:
    log = CardLifecycleLog()
    log.record(SYMPHONY, "S", "dispatched", stage="assessing", at=_at(0))
    log.record(SYMPHONY, "S", "verdict", stage="assessing", detail={"result": "pass"}, at=_at(1), key="a")
    for index in range(MAX_EVENTS_PER_CARD):
        log.record(SYMPHONY, "S", "verdict", stage="reviewing", detail={"result": "fail"}, at=_at(10 + index), key=f"s{index}")
    daemon = SimpleNamespace(state={"card_lifecycle": log, "symphony_states": {}, "active_sessions": {},
                                    "symphony_configs": {SYMPHONY: None}, "lifecycle_sequence": LIFECYCLE})
    strip = card_pipeline_block(daemon)[SYMPHONY]["S"]
    statuses = {item["stage"]: item["status"] for item in strip["statuses"]}
    assert statuses["assessing"] == "done"
