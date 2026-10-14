"""#147 review: the fixes to the lifecycle log, the timeline's reads and the snapshot's cost.

Each test reproduces one finding from the independent review and fails until it is fixed:
an active card is never evicted; the timeline reads only a bounded tail of a transcript, within a
byte budget per request; the board strips are cached and not recomputed for an unchanged card;
the persisted log stays under its size budget; a reused card id starts a fresh history; and an
event with no symphony is refused rather than written under "".
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
from coordinare.services import run_viewer
from coordinare.services.card_lifecycle import (
    MAX_CARDS_PER_SYMPHONY,
    MAX_EVENTS_PER_CARD,
    CardLifecycleLog,
    record_lifecycle,
)
from coordinare.services.run_transcript import transcript_path

SYMPHONY = "alpha-sym"
T0 = datetime(2026, 10, 14, 9, 0, 0, tzinfo=UTC)
LIFECYCLE = ["assessing", "architecting", "implementing", "reviewing", "security", "qa", "documenting", "closing_review"]


def _at(seconds: float) -> str:
    return (T0 + timedelta(seconds=seconds)).isoformat()


# --- 1. an active card is never evicted ------------------------------------------------

def test_a_live_card_survives_when_other_cards_fill_the_symphony() -> None:
    log = CardLifecycleLog()
    log.set_live_provider(lambda symphony: {"ACTIVE"} if symphony == SYMPHONY else set())
    log.record(SYMPHONY, "ACTIVE", "dispatched", stage="implementing", at=_at(0))
    for index in range(MAX_CARDS_PER_SYMPHONY + 20):
        log.record(SYMPHONY, f"DONE{index}", "merged", at=_at(index + 1), key="merged")
    assert log.has_card(SYMPHONY, "ACTIVE")
    assert len(log.cards(SYMPHONY)) <= MAX_CARDS_PER_SYMPHONY


def test_when_live_cards_alone_exceed_the_bound_all_are_kept_and_only_finished_cards_go() -> None:
    live = {f"LIVE{index}" for index in range(MAX_CARDS_PER_SYMPHONY + 3)}
    log = CardLifecycleLog()
    log.set_live_provider(lambda symphony: live)
    for index, card in enumerate(sorted(live)):
        log.record(SYMPHONY, card, "dispatched", stage="implementing", at=_at(index))
    log.record(SYMPHONY, "FINISHED", "merged", at=_at(10_000), key="merged")
    assert live <= set(log.cards(SYMPHONY))
    assert "FINISHED" not in log.cards(SYMPHONY)


def test_a_finished_card_is_ranked_by_last_activity_not_by_its_first_event() -> None:
    log = CardLifecycleLog()
    log.set_live_provider(lambda symphony: set())
    log.record(SYMPHONY, "OLD_TOUCHED", "dispatched", stage="assessing", at=_at(0))
    for index in range(MAX_CARDS_PER_SYMPHONY):
        log.record(SYMPHONY, f"FILL{index}", "merged", at=_at(index + 1), key="merged")
    log.record(SYMPHONY, "OLD_TOUCHED", "verdict", stage="assessing", at=_at(10_000), key="late")
    log.record(SYMPHONY, "NEWEST", "merged", at=_at(20_000), key="merged")
    assert "OLD_TOUCHED" in log.cards(SYMPHONY)


# --- 2. the timeline reads a bounded tail, within a byte budget ----------------------------

def _header(run: str, card: str) -> dict[str, Any]:
    return {"type": "run_header", "seq": 0, "header_version": 1, "ts": _at(0), "run_id": run,
            "persona": {"name": "implementer", "version": 1, "version_sha256": "ab" * 32,
                        "changed_at": _at(0), "changed_by": "local", "source": "dashboard"},
            "performer": {"id": "p", "backend": "claude-code", "transport": "http", "endpoint": "e"},
            "model": {"model": "m"}, "card": {"id": card, "issue_number": 1, "title": "t"}}


def _write(root: Path, card: str, run: str, *, filler: int, end_seconds: float) -> None:
    path = transcript_path(root, symphony=SYMPHONY, card_id=card, run_id=run)
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(_header(run, card))]
    lines += [json.dumps({"ts": _at(1), "seq": n, "type": "assistant_text", "text": "x" * 200}) for n in range(filler)]
    lines.append(json.dumps({"ts": _at(end_seconds), "seq": filler + 1, "type": "result",
                             "is_error": False, "text": "Done."}))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _daemon(root: Path) -> Any:
    daemon = SimpleNamespace()
    daemon.state = {"transcript_collector": SimpleNamespace(root=root), "symphony_states": {},
                    "active_sessions": {}, "symphony_configs": {SYMPHONY: None}, "card_lifecycle": None}
    return daemon


def test_a_long_transcript_is_read_by_its_tail_and_never_scanned_whole(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def _no_full_scan(*args: object, **kwargs: object) -> None:
        raise AssertionError("the timeline must not read a transcript body through run_detail")

    monkeypatch.setattr(run_viewer, "run_detail", _no_full_scan)
    _write(tmp_path, "C1", "run-big", filler=3_000, end_seconds=150)
    runs, reason, _truncated = transcript_runs(tmp_path, symphony=SYMPHONY, card_id="C1")
    assert reason is None and runs is not None
    assert runs[0]["duration_seconds"] == 150.0
    assert runs[0]["outcome"] == "completed"


def test_the_bytes_read_per_request_are_capped(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(card_pipeline, "TIMELINE_READ_BYTES", 1_000)
    for index in range(4):
        _write(tmp_path, "C2", f"run-{index}", filler=2, end_seconds=60)
    runs, _reason, _truncated = transcript_runs(tmp_path, symphony=SYMPHONY, card_id="C2")
    assert runs is not None and len(runs) == 4
    durations = [run["duration_seconds"] for run in runs]
    assert any(d is None for d in durations), "a request past its byte budget must not read more"
    assert any(d == 60.0 for d in durations)


# --- 3. the board strips are cached, and an unchanged card costs nothing ---------------------

def test_an_unchanged_card_is_not_recomputed_on_the_next_snapshot(monkeypatch: pytest.MonkeyPatch) -> None:
    log = CardLifecycleLog()
    for index in range(20):
        log.record(SYMPHONY, f"C{index}", "dispatched", stage="implementing", at=_at(index))
    daemon = SimpleNamespace(state={"card_lifecycle": log, "symphony_states": {}, "active_sessions": {},
                                    "lifecycle_sequence": LIFECYCLE, "symphony_configs": {SYMPHONY: None}})
    first = card_pipeline_block(daemon)
    calls: list[str] = []
    real = card_pipeline.pipeline_strip

    def _counting(*args: Any, **kwargs: Any) -> Any:
        calls.append("strip")
        return real(*args, **kwargs)

    monkeypatch.setattr(card_pipeline, "pipeline_strip", _counting)
    second = card_pipeline_block(daemon)
    assert second == first
    assert calls == [], "an unchanged card must reuse its cached strip"
    log.record(SYMPHONY, "C0", "verdict", stage="implementing", detail={"result": "pass", "marker": "x"}, key="v")
    card_pipeline_block(daemon)
    assert len(calls) == 1, "only the card that changed is recomputed"


# --- 3b. the persisted log stays under its size budget at the bound -------------------------

#: The persisted lifecycle log at the bound measured 1.78 MB before the review, and about 1.0 MB after it.
LOG_BUDGET_BYTES = 1_100_000


def test_a_full_log_at_the_bound_persists_under_its_size_budget() -> None:
    log = CardLifecycleLog()
    detail = {"result": "fail", "marker": "changes_requested", "head": "9f8e7d6c5b4a", "items": 3}
    for card in range(MAX_CARDS_PER_SYMPHONY):
        for event in range(MAX_EVENTS_PER_CARD):
            log.record(SYMPHONY, f"PVTI_{card:04d}", "verdict", stage="reviewing", detail=detail,
                       key=f"v{event}-{card}-" + "x" * 150, at=_at(card * 1000 + event))
    size = len(json.dumps(log.to_snapshot()))
    assert size <= LOG_BUDGET_BYTES, f"a full log is {size} bytes, over the {LOG_BUDGET_BYTES} budget"


# --- 4. a reused card id starts a fresh history ----------------------------------------------

def test_a_card_id_reused_after_merge_starts_a_fresh_history() -> None:
    log = CardLifecycleLog()
    log.record(SYMPHONY, "7", "dispatched", stage="implementing", at=_at(0))
    log.record(SYMPHONY, "7", "merged", at=_at(60), key="merged")
    log.record(SYMPHONY, "7", "dispatched", stage="assessing", at=_at(3600))
    events = log.events(SYMPHONY, "7")
    assert [event["kind"] for event in events] == ["dispatched"]
    assert events[0]["stage"] == "assessing"
    assert events[0]["epoch"] == _at(3600), "the first event of a history carries its epoch"


def test_a_history_that_has_not_merged_is_not_reset_by_a_later_dispatch() -> None:
    log = CardLifecycleLog()
    log.record(SYMPHONY, "8", "dispatched", stage="assessing", at=_at(0))
    log.record(SYMPHONY, "8", "dispatched", stage="implementing", at=_at(60))
    assert [event["stage"] for event in log.events(SYMPHONY, "8")] == ["assessing", "implementing"]


# --- 5. an event with no symphony is refused, and counted once -------------------------------

def test_an_event_with_no_symphony_is_refused_not_written_under_an_empty_name() -> None:
    log = CardLifecycleLog()
    state = {"card_lifecycle": log, "current_symphony": None}
    assert record_lifecycle(state, "dispatched", "Z", stage="implementing") is False
    assert record_lifecycle(state, "merged", "Z", key="merged") is False
    assert log.symphonies() == []
    assert log.unnamed_refusals == 2
