"""429: the observer's verdicts are observable — logs, feed, and session view.

The observer is only trustworthy if its verdicts are visible. This gate pins the
structured-log vocabulary (``observer.evaluation``, ``observer.verdict_<verb>``,
``observer.unreachable``, ``observer.malformed``), the activity-feed entries
(138 dedup rules), and the session view's recent-verdicts list. Summaries only:
raw event text, diffs and prompts never reach any of the three surfaces.
"""
from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
import structlog

from coordinare.services.activity_log import ActivityLog
from coordinare.services.observer import (
    OBSERVER_RECENT_VERDICTS,
    ObserverQuery,
    ObserverVerdict,
    observe,
    record_recent_verdict,
)
from tests.unit.test_425_observer_core import (
    _Backend,
    _observer_cfg,
    _observer_state,
)


def _query(evidence: dict | None = None) -> ObserverQuery:
    return ObserverQuery(
        card_id="C1",
        stage="implementing",
        triggers=["quiet_window"],
        evidence=evidence or {"tool_uses": 2},
        prompt="judge this",
    )


# --- the structured-log vocabulary --------------------------------------------


@pytest.mark.asyncio
async def test_every_evaluation_emits_the_evaluation_event():
    with structlog.testing.capture_logs() as logs:
        verdict = await observe(
            _Backend({"data": {"verdict": "continue", "reason": "moving"}}),
            _query(evidence={"tool_uses": 2, "quiet_age_s": 310}),
        )
    assert verdict is not None and verdict.verdict == "continue"
    events = [e for e in logs if e["event"] == "observer.evaluation"]
    assert len(events) == 1, f"one event per evaluation, got {len(events)}"
    event = events[0]
    assert event["verdict"] == "continue"
    assert event["triggers"] == ["quiet_window"]
    assert event["evidence"] == {"tool_uses": 2, "quiet_age_s": 310}
    assert event["card_id"] == "C1"
    assert event["performer_stage"] == "implementing"
    # A continue verdict does not add a second, per-verb event.
    assert "observer.verdict_continue" not in [e["event"] for e in logs]


@pytest.mark.asyncio
async def test_a_non_continue_verdict_also_emits_the_named_verdict_event():
    with structlog.testing.capture_logs() as logs:
        verdict = await observe(
            _Backend({"data": {"verdict": "kill", "reason": "no results"}}),
            _query(),
        )
    assert verdict is not None and verdict.verdict == "kill"
    names = [e["event"] for e in logs]
    assert names.count("observer.evaluation") == 1
    assert "observer.verdict_kill" in names, "the verb must be greppable by name"


@pytest.mark.asyncio
async def test_every_non_continue_verb_gets_its_own_event_name():
    for verb in ("correction", "kill", "retune", "escalate"):
        with structlog.testing.capture_logs() as logs:
            await observe(_Backend({"data": {"verdict": verb, "reason": "r"}}), _query())
        assert f"observer.verdict_{verb}" in [e["event"] for e in logs], verb


@pytest.mark.asyncio
async def test_the_evaluation_event_carries_no_prompt_and_no_unbounded_text():
    evidence = {"tool_uses": 2, "quiet_age_s": 310, "echo": "x" * 500}
    with structlog.testing.capture_logs() as logs:
        await observe(
            _Backend({"data": {"verdict": "continue", "reason": "moving"}}),
            _query(evidence=evidence),
        )
    event = next(e for e in logs if e["event"] == "observer.evaluation")
    assert "prompt" not in event, "the prompt never reaches a log line"
    # Evidence strings arrive capped: length, not contents, is what survives.
    assert event["evidence"]["echo"] == "x" * 400
    assert event["evidence"]["tool_uses"] == 2


@pytest.mark.asyncio
async def test_an_unreachable_observer_emits_unreachable():
    with structlog.testing.capture_logs() as logs:
        verdict = await observe(_Backend(boom=True), _query())
    assert verdict is None
    names = [e["event"] for e in logs]
    assert "observer.unreachable" in names
    assert "observer.evaluation" not in names


@pytest.mark.asyncio
async def test_a_malformed_verdict_emits_malformed():
    with structlog.testing.capture_logs() as logs:
        verdict = await observe(
            _Backend({"data": {"verdict": "whatever", "reason": "x"}}),
            _query(),
        )
    assert verdict is None
    names = [e["event"] for e in logs]
    assert "observer.malformed" in names
    assert "observer.evaluation" not in names


# --- the recent-verdicts list (the session view's data source) -----------------


def test_the_recent_verdicts_list_is_bounded():
    recent: list[dict] = []
    for i in range(7):
        recent = record_recent_verdict(
            recent,
            ObserverVerdict("continue", f"r{i}"),
            triggers=["quiet_window"],
            evidence={"tool_uses": i},
            now="2026-09-25T00:00:00+00:00",
        )
    assert OBSERVER_RECENT_VERDICTS == 5
    assert len(recent) == OBSERVER_RECENT_VERDICTS
    assert recent[-1]["reason"] == "r6"
    assert recent[0]["reason"] == "r2"


def test_a_verdict_entry_is_a_summary():
    entry = record_recent_verdict(
        [],
        ObserverVerdict("kill", "looping"),
        triggers=["repetition_signature", "quiet_window"],
        evidence={"tool_uses": 0, "quiet_age_s": 310, "echo": "y" * 500},
        now="2026-09-25T00:00:00+00:00",
    )[0]
    assert entry["verdict"] == "kill"
    assert entry["reason"] == "looping"
    assert entry["triggers"] == ["quiet_window", "repetition_signature"]
    assert entry["evidence"]["echo"] == "y" * 400
    assert entry["at"] == "2026-09-25T00:00:00+00:00"


# --- the wiring: monitor → feed + session view ---------------------------------


def _observer_run(verb: str, reason: str, **extra):
    from coordinare.graph.nodes.monitor_performer import monitor_performer

    log = ActivityLog()
    backend = _Backend({"data": {"verdict": verb, "reason": reason}})
    cfg = _observer_cfg(quiet_window_seconds=100.0)
    s = _observer_state(
        cfg, backend,
        last_production_at=datetime.now(UTC) - timedelta(seconds=600),
        **extra,
    )
    s["activity_log"] = log
    return s, log, monitor_performer


@pytest.mark.asyncio
async def test_a_verdict_becomes_an_activity_entry():
    s, log, monitor_performer = _observer_run("kill", "burning without results")

    await monitor_performer(s)
    entries = [
        e for e in log.snapshot() if e["activity_type"] == "observer_verdict"
    ]
    assert len(entries) == 1
    entry = entries[0]
    assert "kill" in entry["text"] and "burning without results" in entry["text"]
    assert entry["card_id"] == "ITEM_1"
    assert entry["stage"] == "implementing"
    assert entry["session_id"] == "s1"


@pytest.mark.asyncio
async def test_a_repeated_verdict_does_not_scroll_the_feed():
    s, log, monitor_performer = _observer_run("continue", "still quiet")

    for _ in range(2):
        s["last_production_at"] = datetime.now(UTC) - timedelta(seconds=600)
        await monitor_performer(s)
    entries = [
        e for e in log.snapshot() if e["activity_type"] == "observer_verdict"
    ]
    assert len(entries) == 1, "the 138 dedup suppresses the repeated verdict"


@pytest.mark.asyncio
async def test_a_verdict_is_kept_for_the_session_view():
    s, _log, monitor_performer = _observer_run("kill", "burning without results")

    await monitor_performer(s)
    verdicts = s.get("observer_verdicts")
    assert isinstance(verdicts, list) and len(verdicts) == 1
    v = verdicts[0]
    assert v["verdict"] == "kill"
    assert v["reason"] == "burning without results"
    assert "quiet_age_s" in v["evidence"]
    assert v["triggers"] == ["quiet_window"]
    assert datetime.fromisoformat(v["at"])


@pytest.mark.asyncio
async def test_the_session_view_keeps_the_last_verdicts_not_every_one():
    s, _log, monitor_performer = _observer_run("continue", "still quiet")
    s["observer_verdicts"] = [
        {"verdict": "continue", "reason": "old", "evidence": {}, "triggers": [], "at": "t"}
        for _ in range(5)
    ]

    await monitor_performer(s)
    verdicts = s["observer_verdicts"]
    assert len(verdicts) == OBSERVER_RECENT_VERDICTS
    assert verdicts[-1]["reason"] == "still quiet"
    assert verdicts[0]["reason"] == "old"


@pytest.mark.asyncio
async def test_no_raw_telemetry_reaches_the_observer_surfaces():
    telemetry = "SECRET-DIFF-CONTENT $password /etc/shadow"
    s, log, monitor_performer = _observer_run(
        "continue", "quiet",
        performer_events=[{"type": "progress", "text": telemetry}] * 3,
    )

    await monitor_performer(s)
    surfaces = json.dumps({
        "feed": log.snapshot(),
        "verdicts": s.get("observer_verdicts"),
    })
    assert "SECRET-DIFF-CONTENT" not in surfaces


@pytest.mark.asyncio
async def test_a_disabled_observer_writes_neither_feed_nor_verdicts():
    from coordinare.graph.nodes.monitor_performer import monitor_performer

    log = ActivityLog()
    s = _observer_state(_observer_cfg(enabled=False), None)
    s["activity_log"] = log

    await monitor_performer(s)
    assert "observer_verdicts" not in s
    assert log.snapshot() == []


@pytest.mark.asyncio
async def test_a_failed_observer_writes_no_verdicts():
    from coordinare.graph.nodes.monitor_performer import monitor_performer

    log = ActivityLog()
    backend = _Backend(boom=True)
    cfg = _observer_cfg(quiet_window_seconds=100.0)
    s = _observer_state(
        cfg, backend,
        last_production_at=datetime.now(UTC) - timedelta(seconds=600),
    )
    s["activity_log"] = log

    await monitor_performer(s)
    assert backend.calls == 1
    assert "observer_verdicts" not in s
    assert [e for e in log.snapshot() if e["activity_type"] == "observer_verdict"] == []


# --- the session round-trip -----------------------------------------------------


def test_the_verdicts_round_trip_through_the_card_session():
    from coordinare.graph.state import initial_state
    from coordinare.session import (
        _SESSION_FIELDS,
        _SESSION_OPTIONAL_FIELDS,
        session_to_state,
        state_to_session,
    )

    assert "observer_verdicts" in _SESSION_FIELDS
    assert "observer_verdicts" in _SESSION_OPTIONAL_FIELDS

    s = initial_state()
    s["observer_verdicts"] = [
        {"verdict": "continue", "reason": "r", "evidence": {}, "triggers": [], "at": "t"},
    ]
    session = state_to_session(s)
    assert session["observer_verdicts"] == s["observer_verdicts"]

    fresh: dict = {}
    session_to_state(session, fresh)  # type: ignore[arg-type]
    assert fresh["observer_verdicts"] == s["observer_verdicts"]


def test_a_card_without_verdicts_stays_without_them():
    from coordinare.graph.state import initial_state
    from coordinare.session import session_to_state, state_to_session

    assert "observer_verdicts" not in initial_state()
    session = state_to_session(initial_state())
    assert "observer_verdicts" not in session
    fresh: dict = {"observer_verdicts": [{"verdict": "stale"}]}
    session_to_state(session, fresh)  # type: ignore[arg-type]
    assert "observer_verdicts" not in fresh


# --- the dashboard store ---------------------------------------------------------


def test_the_session_summary_exposes_recent_observer_verdicts():
    from coordinare.dashboard.store import DashboardStore

    daemon = SimpleNamespace(state={})
    keep = {"verdict": "continue", "reason": "r", "evidence": {}, "at": "t"}
    sessions = [
        DashboardStore._session_summary(daemon, "c1", {"observer_verdicts": [
            {"verdict": "old", "reason": "o", "evidence": {}, "at": "t"},
            keep, keep, keep, keep, keep,
        ]}),
        DashboardStore._session_summary(daemon, "c1", {}),
    ]
    assert len(sessions[0]["observer_verdicts"]) == OBSERVER_RECENT_VERDICTS
    assert sessions[0]["observer_verdicts"][-1] == keep
    assert sessions[1]["observer_verdicts"] == []
