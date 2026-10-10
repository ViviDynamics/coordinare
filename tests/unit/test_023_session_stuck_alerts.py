from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from coordinare.config import StuckAlertConfig
from coordinare.daemon import CoordinareDaemon
from coordinare.services.activity_log import ActivityLog
from tests.utils.fake_notification import FakeNotificationService


def worker(card: str, *, age: int = 300, phase: str = "monitoring_performer") -> dict[str, Any]:
    entered = datetime.now(UTC) - timedelta(seconds=age)
    return {
        "phase": phase,
        "phase_entered_at": entered,
        "agent_dispatch": {"session_id": f"worker-{card}"},
        "agent_dispatch_at": entered,
        "current_card": {"id": card, "title": f"Task {card}", "issue_number": 1},
    }


def detector(sessions: dict[str, Any]) -> tuple[CoordinareDaemon, ActivityLog, FakeNotificationService]:
    log = ActivityLog()
    service = FakeNotificationService()
    daemon = CoordinareDaemon(None)
    daemon.state.update({
        "phase": "blocked",
        "phase_entered_at": datetime.now(UTC) - timedelta(seconds=300),
        "current_card": None,
        "config": SimpleNamespace(stuck_alerts=StuckAlertConfig(
            threshold_seconds=1800,
            per_phase_thresholds={"monitoring_performer": 180, "monitoring_agent": 180},
            cooldown_seconds=1800,
        )),
        "activity_log": log,
        "active_sessions": sessions,
    })
    return daemon, log, service


def multi_detector() -> tuple[CoordinareDaemon, ActivityLog, FakeNotificationService]:
    from coordinare.config import ProjectConfiguration, SymphonyConfig
    from coordinare.graph.state import SymphonyRuntimeState

    sessions = {name: worker(name) for name in ("fast", "slow", "paused")}
    daemon, log, service = detector(sessions)
    daemon.state["config"] = ProjectConfiguration(
        github_org="example", github_token="synthetic-token", human_reviewers=["reviewer"],
        stuck_alerts=StuckAlertConfig(threshold_seconds=1800, per_phase_thresholds={}, cooldown_seconds=1800),
    )
    daemon.state["symphony_configs"] = {
        name: SymphonyConfig(
            name=name, github_project_number=1, enabled=name != "paused",
            overrides={"stuck_alerts": {"threshold_seconds": threshold, "per_phase_thresholds": {}, "cooldown_seconds": 10}},
        ) for name, threshold in (("fast", 180), ("slow", 600), ("paused", 180))
    }
    daemon.state["symphony_states"] = {
        name: SymphonyRuntimeState(name=name, active_sessions={name: session})
        for name, session in sessions.items()
    }
    return daemon, log, service


@pytest.mark.asyncio
async def test_aggregate_worker_detection_uses_each_symphonys_effective_threshold() -> None:
    daemon, log, service = multi_detector()
    await daemon._detect_stuck_card(service)
    assert [entry["card_id"] for entry in log.snapshot()] == ["fast"]
    assert service.dispatched[0].payload["threshold_seconds"] == "180"


@pytest.mark.asyncio
async def test_disabled_symphonys_retained_worker_never_emits_alert() -> None:
    daemon, log, service = multi_detector()
    daemon.state["config"].stuck_alerts.threshold_seconds = 180
    await daemon._detect_stuck_card(service)
    assert [entry["card_id"] for entry in log.snapshot()] == ["fast"]
    assert len(service.dispatched) == 1


@pytest.mark.asyncio
async def test_aggregate_worker_alert_uses_its_symphonys_cooldown(monkeypatch: pytest.MonkeyPatch) -> None:
    import coordinare.daemon as daemon_module

    daemon, _log, service = multi_detector()
    clock = [100.0]
    monkeypatch.setattr(daemon_module, "monotonic", lambda: clock[0])
    await daemon._detect_stuck_card(service)
    clock[0] += 11
    await daemon._detect_stuck_card(service)
    assert [event.payload["card_id"] for event in service.dispatched] == ["fast", "fast"]


@pytest.mark.asyncio
async def test_paused_sibling_cannot_hide_overdue_worker_or_its_identity() -> None:
    paused = worker("paused", phase="blocked")
    paused["agent_dispatch"] = {}
    paused["agent_dispatch_at"] = None
    daemon, log, service = detector({"active": worker("active"), "paused": paused})
    await daemon._detect_stuck_card(service)
    assert [(e["card_id"], e["stage"]) for e in log.snapshot()] == [("active", "monitoring_performer")]
    assert len(service.dispatched) == 1
    assert service.dispatched[0].payload["card_id"] == "active"
    assert service.dispatched[0].payload["threshold_seconds"] == "180"


@pytest.mark.asyncio
async def test_overdue_workers_do_not_share_a_cooldown() -> None:
    daemon, log, service = detector({"first": worker("first"), "second": worker("second", phase="monitoring_agent")})
    await daemon._detect_stuck_card(service)
    assert {e["card_id"] for e in log.snapshot()} == {"first", "second"}
    assert len(service.dispatched) == 2
    await daemon._detect_stuck_card(service)
    assert len(service.dispatched) == 2


@pytest.mark.asyncio
async def test_newly_overdue_sibling_does_not_inherit_another_cards_cooldown() -> None:
    daemon, log, service = detector({"first": worker("first"), "second": worker("second", age=1)})
    await daemon._detect_stuck_card(service)
    daemon.state["active_sessions"]["second"] = worker("second")
    await daemon._detect_stuck_card(service)
    assert [e["card_id"] for e in log.snapshot()] == ["first", "second"]
    assert len(service.dispatched) == 2


@pytest.mark.asyncio
async def test_fresh_replacement_does_not_inherit_old_phase_age_or_cooldown() -> None:
    daemon, log, service = detector({"active": worker("active")})
    await daemon._detect_stuck_card(service)
    fresh = worker("active", age=1)
    fresh["agent_dispatch"]["session_id"] = "replacement"
    fresh["phase_entered_at"] = datetime.now(UTC) - timedelta(hours=2)
    daemon.state["active_sessions"]["active"] = fresh
    await daemon._detect_stuck_card(service)
    assert len(service.dispatched) == 1
    fresh["agent_dispatch_at"] = datetime.now(UTC) - timedelta(seconds=300)
    await daemon._detect_stuck_card(service)
    assert len(service.dispatched) == 2
    assert [e["session_id"] for e in log.snapshot()] == ["worker-active", "replacement"]


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["idle", "blocked", "system_error", "monitoring_pr", "dispatching"])
async def test_retained_inactive_sessions_do_not_emit_worker_stuck_alerts(phase: str) -> None:
    session = worker("inactive", phase=phase)
    daemon, log, service = detector({"inactive": session})
    daemon.state["phase_entered_at"] = datetime.now(UTC) - timedelta(hours=2)
    await daemon._detect_stuck_card(service)
    assert log.snapshot() == []
    assert service.dispatched == []


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["identity", "dispatch", "paused"])
async def test_monitoring_without_a_live_worker_is_not_a_stuck_worker(missing: str) -> None:
    session = worker("inactive")
    if missing == "identity":
        session["agent_dispatch"] = {}
    elif missing == "dispatch":
        session["agent_dispatch_at"] = None
    else:
        session["board_paused"] = True
    daemon, log, service = detector({"inactive": session})
    daemon.state["phase_entered_at"] = datetime.now(UTC) - timedelta(hours=2)
    await daemon._detect_stuck_card(service)
    assert log.snapshot() == []
    assert service.dispatched == []


@pytest.mark.asyncio
async def test_disabled_monitoring_threshold_and_fresh_dispatch_are_respected() -> None:
    daemon, log, service = detector({"disabled": worker("disabled"), "fresh": worker("fresh", age=1, phase="monitoring_agent")})
    daemon.state["config"].stuck_alerts.per_phase_thresholds["monitoring_performer"] = 0
    await daemon._detect_stuck_card(service)
    assert log.snapshot() == []
    assert service.dispatched == []


@pytest.mark.asyncio
async def test_legacy_flat_detection_still_names_and_deduplicates_a_stuck_card() -> None:
    daemon, log, service = detector({})
    daemon.state.update({
        "phase": "monitoring_performer",
        "phase_entered_at": datetime.now(UTC) - timedelta(seconds=300),
        "current_card": {"id": "legacy", "title": "Legacy task", "issue_number": 9},
    })
    await daemon._detect_stuck_card(service)
    await daemon._detect_stuck_card(service)
    assert [e["card_id"] for e in log.snapshot()] == ["legacy"]
    assert len(service.dispatched) == 1


@pytest.mark.asyncio
async def test_live_worker_alert_reaches_feed_without_notification_channels() -> None:
    daemon, log, _service = detector({"active": worker("active")})
    await daemon._detect_stuck_card(None)
    assert [(e["card_id"], e["session_id"]) for e in log.snapshot()] == [("active", "worker-active")]


@pytest.mark.asyncio
async def test_legacy_dispatching_detection_remains_available_with_session_history() -> None:
    daemon, log, service = detector({"pending": {"phase": "dispatching"}})
    daemon.state.update({
        "phase": "dispatching",
        "phase_entered_at": datetime.now(UTC) - timedelta(seconds=1900),
        "current_card": {"id": "pending", "title": "Pending task", "issue_number": 9},
    })
    await daemon._detect_stuck_card(service)
    assert [e["card_id"] for e in log.snapshot()] == ["pending"]


@pytest.mark.asyncio
async def test_worker_alert_does_not_consume_a_separate_dispatch_alert_cooldown() -> None:
    daemon, log, service = detector({"active": worker("active"), "pending": {"phase": "dispatching"}})
    daemon.state.update({
        "phase": "dispatching",
        "phase_entered_at": datetime.now(UTC) - timedelta(seconds=1900),
        "current_card": {"id": "pending", "title": "Pending task", "issue_number": 2},
    })
    await daemon._detect_stuck_card(service)
    assert [e["card_id"] for e in log.snapshot()] == ["active", "pending"]
    assert [e.payload["card_id"] for e in service.dispatched] == ["active", "pending"]
    await daemon._detect_stuck_card(service)
    assert len(service.dispatched) == 2


@pytest.mark.asyncio
async def test_real_persistence_and_restore_preserve_overdue_worker_dispatch_clock() -> None:
    from coordinare.daemon import _persist_active_sessions, _restored_session_dict
    from coordinare.state_store import WorkflowSnapshot

    original = worker("active")
    persisted = _persist_active_sessions({"active": original})
    snapshot = WorkflowSnapshot(snapshot_at=datetime.now(UTC), phase="blocked", active_sessions=persisted)
    restored = _restored_session_dict("active", snapshot.active_sessions["active"], snapshot, original["current_card"])
    assert restored.get("agent_dispatch_at") == original["agent_dispatch_at"]
    daemon, log, service = detector({"active": restored, "paused": worker("paused", phase="blocked")})
    await daemon._detect_stuck_card(service)
    assert [e["card_id"] for e in log.snapshot()] == ["active"]


@pytest.mark.asyncio
async def test_old_snapshot_uses_its_persisted_progress_clock_conservatively() -> None:
    from coordinare.daemon import _restored_session_dict
    from coordinare.state_store import PersistedSession, WorkflowSnapshot

    persisted = PersistedSession(card_id="active", phase="monitoring_performer", agent_session_id="restored",
                                 last_progress_at=datetime.now(UTC) - timedelta(seconds=300))
    snapshot = WorkflowSnapshot(snapshot_at=datetime.now(UTC), phase="blocked", schema_version=31, active_sessions={"active": persisted})
    restored = _restored_session_dict("active", persisted, snapshot, {"id": "active", "title": "Restored"})
    daemon, log, service = detector({"active": restored})
    await daemon._detect_stuck_card(service)
    assert [e["card_id"] for e in log.snapshot()] == ["active"]


@pytest.mark.asyncio
async def test_old_worker_without_any_clock_becomes_overdue_after_observation(monkeypatch: pytest.MonkeyPatch) -> None:
    import coordinare.daemon as daemon_module

    class Clock(datetime):
        current = datetime.now(UTC)

        @classmethod
        def now(cls, tz=None):
            return cls.current

    monkeypatch.setattr(daemon_module, "datetime", Clock)
    old = worker("active")
    old["agent_dispatch_at"] = None
    old["phase_entered_at"] = None
    daemon, log, service = detector({"active": old})
    await daemon._detect_stuck_card(service)
    assert log.snapshot() == []
    Clock.current += timedelta(seconds=181)
    await daemon._detect_stuck_card(service)
    assert [e["card_id"] for e in log.snapshot()] == ["active"]


def test_dispatch_clock_has_a_versioned_strict_snapshot_contract() -> None:
    import json
    from pathlib import Path

    from jsonschema import validate

    from coordinare.state_store import CURRENT_SCHEMA_VERSION, PersistedSession, WorkflowSnapshot

    assert CURRENT_SCHEMA_VERSION == 33
    contract = json.loads(Path("specs/003-state-persistence/contracts/workflow-snapshot.schema.json").read_text())
    assert contract["properties"]["schema_version"]["enum"] == list(range(1, 34))
    assert {"last_production_at", "last_production_fingerprint"} <= set(
        contract["properties"]["active_sessions"]["additionalProperties"]["properties"],
    )
    snapshot = WorkflowSnapshot(snapshot_at=datetime.now(UTC), phase="blocked", active_sessions={"active": PersistedSession(card_id="active", agent_dispatch_at=datetime.now(UTC), last_production_at=datetime.now(UTC), last_production_fingerprint=(1, 0))})
    validate(snapshot.model_dump(mode="json"), contract)
    old = WorkflowSnapshot.model_validate({"schema_version": 31, "snapshot_at": datetime.now(UTC), "phase": "blocked", "active_sessions": {"active": {"card_id": "active"}}})
    assert old.active_sessions["active"].agent_dispatch_at is None


@pytest.mark.asyncio
@pytest.mark.parametrize("production_age,dispatch_age,expected_phase", [
    (5, 2400, "monitoring_performer"),
    (None, 2400, "blocked"),
    (1300, 2400, "blocked"),
    (5, 4900, "blocked"),
])
async def test_restored_worker_preserves_production_based_timeout_and_absolute_ceiling(
    production_age: int | None, dispatch_age: int, expected_phase: str,
) -> None:
    from coordinare.daemon import _persist_active_sessions, _restored_session_dict
    from coordinare.graph.nodes.monitor_performer import monitor_performer
    from coordinare.graph.state import initial_state
    from coordinare.session import session_to_state, state_to_session
    from coordinare.state_store import WorkflowSnapshot
    from tests.unit.graph.nodes.test_monitor_performer import _make_state, _Performer

    now = datetime.now(UTC)
    service = _Performer({"status": "working", "events": [{"type": "tool_use", "text": "running tests"}]})
    original = _make_state(service=service, stage="reviewing")
    produced_at = now - timedelta(seconds=production_age) if production_age is not None else None
    original.update(phase="monitoring_performer", role_timeouts={"reviewing": 1200},
                    agent_dispatch_at=now - timedelta(seconds=dispatch_age),
                    last_production_at=produced_at, last_production_fingerprint=(1, 0))
    snapshot = WorkflowSnapshot(snapshot_at=now, phase="monitoring_performer", active_card_id="ITEM_1",
                                active_sessions=_persist_active_sessions({"ITEM_1": state_to_session(original)}))
    snapshot = WorkflowSnapshot.model_validate_json(snapshot.model_dump_json())
    restored = _restored_session_dict("ITEM_1", snapshot.active_sessions["ITEM_1"], snapshot, original["current_card"])
    state = initial_state()
    session_to_state(restored, state)
    state.update(performer_services={"reviewing": service}, lifecycle_sequence=["reviewing"],
                 role_timeouts={"reviewing": 1200})
    await monitor_performer(state)
    assert state["phase"] == expected_phase
    if expected_phase == "monitoring_performer":
        # Re-reported cumulative tools are not fresh production after restart.
        assert state["last_production_at"] == produced_at
        assert state["last_production_fingerprint"] == (1, 0)


def test_production_clock_is_card_scoped_and_survives_session_projection() -> None:
    from coordinare.graph.state import initial_state
    from coordinare.session import create_session_from_card, session_to_state, state_to_session

    state = initial_state()
    produced_at = datetime.now(UTC)
    state.update(last_production_at=produced_at, last_production_fingerprint=(7, 2))
    saved = state_to_session(state)
    session_to_state(create_session_from_card({"id": "new"}), state)
    assert state.get("last_production_at") is None
    assert state.get("last_production_fingerprint") is None
    session_to_state(saved, state)
    assert state["last_production_at"] == produced_at
    assert state["last_production_fingerprint"] == (7, 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("shutdown", [False, True])
async def test_actual_snapshot_save_gate_flushes_new_production_and_ignores_replayed_tools(shutdown: bool) -> None:
    from coordinare.graph.nodes.monitor_performer import monitor_performer
    from coordinare.session import state_to_session
    from coordinare.state_store import WorkflowSnapshot
    from tests.unit.graph.nodes.test_monitor_performer import _make_state, _Performer

    class SnapshotSink:
        def __init__(self) -> None:
            self.saved: list[WorkflowSnapshot] = []

        async def save(self, snapshot: WorkflowSnapshot) -> None:
            self.saved.append(WorkflowSnapshot.model_validate_json(snapshot.model_dump_json()))

    sink = SnapshotSink()
    daemon = CoordinareDaemon(None, state_store=sink)
    service = _Performer({"status": "working", "events": [{"type": "tool_use", "text": "run tests"}]})
    state = _make_state(service=service, stage="reviewing")
    state.update(phase="monitoring_performer", role_timeouts={"reviewing": 1200},
                 agent_dispatch_at=datetime.now(UTC) - timedelta(seconds=600))
    state["active_sessions"] = {"ITEM_1": state_to_session(state)}
    daemon.state.update(state)
    signature = await daemon._save_snapshot_if_changed(None)
    await monitor_performer(state)
    daemon.state.update(state)
    daemon.state["active_sessions"] = {"ITEM_1": state_to_session(state)}
    if shutdown:
        await daemon._shutdown_flush(signature)
    else:
        signature = await daemon._save_snapshot_if_changed(signature)
    assert len(sink.saved) == 2
    assert sink.saved[-1].active_sessions["ITEM_1"].last_production_at == state["last_production_at"]
    assert sink.saved[-1].active_sessions["ITEM_1"].last_production_fingerprint == (1, 0)
    signature = daemon._lifecycle_signature()
    await monitor_performer(state)
    daemon.state.update(state)
    daemon.state["active_sessions"] = {"ITEM_1": state_to_session(state)}
    await daemon._save_snapshot_if_changed(signature)
    assert len(sink.saved) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("restart", [False, True])
async def test_full_event_buffer_records_fresh_production_after_replay(restart: bool) -> None:
    from coordinare.daemon import _persist_active_sessions, _restored_session_dict
    from coordinare.graph.nodes.monitor_performer import monitor_performer
    from coordinare.graph.state import initial_state
    from coordinare.session import session_to_state, state_to_session
    from coordinare.state_store import WorkflowSnapshot
    from tests.unit.graph.nodes.test_monitor_performer import _make_state, _Performer

    now = datetime.now(UTC)
    produced_at = now - timedelta(seconds=1190)
    events = [{"type": "tool_use", "text": f"run test {i}", "seq": i} for i in range(100)]
    service = _Performer({"status": "working", "events": events})
    state = _make_state(service=service, stage="reviewing")
    state.update(phase="monitoring_performer", role_timeouts={"reviewing": 1200},
                 agent_dispatch_at=now - timedelta(seconds=2400),
                 last_production_at=produced_at, last_production_fingerprint=(100, 0),
                 performer_events=list(events))
    if restart:
        snapshot = WorkflowSnapshot(snapshot_at=now, phase="monitoring_performer", active_card_id="ITEM_1",
                                    active_sessions=_persist_active_sessions({"ITEM_1": state_to_session(state)}))
        snapshot = WorkflowSnapshot.model_validate_json(snapshot.model_dump_json())
        restored = _restored_session_dict("ITEM_1", snapshot.active_sessions["ITEM_1"], snapshot, state["current_card"])
        state = initial_state()
        session_to_state(restored, state)
        state.update(performer_services={"reviewing": service}, lifecycle_sequence=["reviewing"],
                     role_timeouts={"reviewing": 1200})
    await monitor_performer(state)
    assert state["last_production_at"] == produced_at, "Replaying a full buffer must not reset the clock"
    # Persist the seeded cursor, then restore without the in-memory event list.
    snapshot = WorkflowSnapshot(snapshot_at=now, phase="monitoring_performer", active_card_id="ITEM_1",
                                active_sessions=_persist_active_sessions({"ITEM_1": state_to_session(state)}))
    snapshot = WorkflowSnapshot.model_validate_json(snapshot.model_dump_json())
    restored = _restored_session_dict("ITEM_1", snapshot.active_sessions["ITEM_1"], snapshot, state["current_card"])
    state = initial_state()
    session_to_state(restored, state)
    state.update(performer_services={"reviewing": service}, lifecycle_sequence=["reviewing"],
                 role_timeouts={"reviewing": 1200})
    await monitor_performer(state)
    assert state["last_production_at"] == produced_at
    service._response["events"] = [*events[1:], {"type": "tool_use", "text": "run fresh test", "seq": 100}]
    await monitor_performer(state)
    assert state["phase"] == "monitoring_performer"
    assert state["last_production_at"] > produced_at, "A new tool at unchanged counts is real production"
    fresh_clock = state["last_production_at"]
    service._response["events"] = [{"type": "progress", "text": f"thought {i}"} for i in range(100)]
    await monitor_performer(state)
    assert state["last_production_at"] == fresh_clock
    service._response["events"] = [{"type": "tool_use", "text": "run next fresh test", "seq": 101}]
    await monitor_performer(state)
    assert state["last_production_at"] > fresh_clock


def test_production_cursor_changes_only_for_newest_productive_event() -> None:
    from coordinare.services.progress_evidence import production_cursor

    events = [{"type": "tool_use", "text": "run same command", "seq": 1},
              {"type": "progress", "text": "thinking", "seq": 2}]
    cursor = production_cursor(events)
    assert cursor is not None
    assert production_cursor(events[:1]) == cursor
    assert production_cursor([*events, {"type": "progress", "text": "more thoughts"}]) == cursor
    assert production_cursor([*events, {"type": "tool_use", "text": "run same command", "seq": 3}]) != cursor
    assert production_cursor([{"type": "progress", "text": "thinking"}]) is None


def test_production_cursor_is_durable_and_card_scoped() -> None:
    from coordinare.daemon import _persist_active_sessions, _restored_session_dict
    from coordinare.graph.state import initial_state
    from coordinare.session import create_session_from_card, session_to_state, state_to_session
    from coordinare.state_store import WorkflowSnapshot

    state = initial_state()
    card = {"id": "active", "title": "Synthetic card"}
    state.update(current_card=card, last_production_cursor="cursor-A")
    saved = state_to_session(state)
    snapshot = WorkflowSnapshot(snapshot_at=datetime.now(UTC), phase="monitoring_performer",
                                active_sessions=_persist_active_sessions({"active": saved}))
    snapshot = WorkflowSnapshot.model_validate_json(snapshot.model_dump_json())
    restored = _restored_session_dict("active", snapshot.active_sessions["active"], snapshot, card)
    state = initial_state()
    session_to_state(restored, state)
    assert state.get("last_production_cursor") == "cursor-A"
    session_to_state(create_session_from_card({"id": "sibling"}), state)
    assert state.get("last_production_cursor") is None
