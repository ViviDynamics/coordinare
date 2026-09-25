"""427 — kill and reprompt: early termination of a turn the observer judges dead.

A ``kill`` verdict stops the live turn through the spec 076 drain-or-reap
path, records any PR artefacts first (partial work is not forgotten), and
re-dispatches with the observer's correction riding the payload. The
kill-and-reprompt counts through the retry counter, so the second kill in
a 24h window escalates to BLOCKED. The 076 per-card mutex applies: a kill
is refused while a dispatch or reconciliation pass holds the card, and a
failed kill falls back to today's behavior.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import pytest

from coordinare.services.observer import correction_signature
from tests.unit.test_425_observer_core import (
    _Backend,
    _observer_cfg,
    _observer_state,
)

KILL = {"data": {"verdict": "kill", "reason": "all motion, no artifacts"}}


class _KillablePerformer:
    """Performer double whose drain (the kill's teardown step) is observable."""

    def __init__(self, response: dict | None = None, drain_fails: bool = False):
        self._response = response or {"status": "working"}
        self.drain_calls: list[str] = []
        self._drain_fails = drain_fails
        self._active_jobs = {"s1": SimpleNamespace(container_id="c1"), "s2": SimpleNamespace(container_id="c2")}

    async def check_status(self, session_id: str, **kwargs: object) -> dict:
        _ = session_id
        return self._response

    async def drain_session(self, session_id: str) -> None:
        self.drain_calls.append(session_id)
        if self._drain_fails:
            raise RuntimeError("container refused to drain")


def _kill_state(**extra: Any) -> tuple[_KillablePerformer, _Backend, Any]:
    performer = _KillablePerformer()
    backend = _Backend(dict(KILL))
    cfg = _observer_cfg(quiet_window_seconds=100.0)
    s = _observer_state(
        cfg, backend, performer,
        last_production_at=datetime.now(UTC) - timedelta(seconds=600),
    )
    s.update(extra)
    return performer, backend, s


# --- the kill path ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_kill_verdict_ends_the_turn_early():
    from coordinare.graph.nodes.monitor_performer import monitor_performer

    performer, _, s = _kill_state()
    result = await monitor_performer(s)
    assert result["phase"] == "dispatching", "the dead turn was not stopped"
    assert result["agent_dispatch"] == {}, "the dead session was not released"
    assert result["agent_dispatch_at"] is None
    assert performer.drain_calls == ["s1"], "the live session was not drained or reaped"


@pytest.mark.asyncio
async def test_the_kill_resets_the_dead_run_s_repetition_streak():
    from coordinare.graph.nodes.monitor_performer import monitor_performer

    _, _, s = _kill_state(observer_repetition_count=7)
    result = await monitor_performer(s)
    assert result["phase"] == "dispatching"
    assert result["observer_repetition_count"] == 0, (
        "a fresh run must not inherit the dead run's repetition evidence"
    )


# --- artefact write-through ------------------------------------------------------


@pytest.mark.asyncio
async def test_the_kill_records_pr_artefacts_before_teardown():
    from coordinare.graph.nodes.monitor_performer import monitor_performer

    performer = _KillablePerformer(
        {
            "status": "working",
            "pr_url": "https://github.com/o/r/pull/5",
            "pr_number": 5,
            "head_sha": "abc123",
            "pushed_branch": "coordinare/ITEM_1/wip",
        },
    )
    backend = _Backend(dict(KILL))
    cfg = _observer_cfg(quiet_window_seconds=100.0)
    s = _observer_state(
        cfg, backend, performer,
        last_production_at=datetime.now(UTC) - timedelta(seconds=600),
        active_sessions={"ITEM_1": {"current_card": {"id": "ITEM_1"}}},
    )
    result = await monitor_performer(s)
    assert result["current_card"]["pr_url"] == "https://github.com/o/r/pull/5"
    assert result["current_card"]["head_after"] == "abc123"
    sessions = result["active_sessions"]
    assert sessions["ITEM_1"]["current_card"]["pr_url"] == "https://github.com/o/r/pull/5"
    assert sessions["ITEM_1"]["pr_artefacts_recorded_at"] is not None


# --- the correction rides the fresh dispatch -------------------------------------


@pytest.mark.asyncio
async def test_the_kill_correction_rides_the_fresh_dispatch():
    from coordinare.graph.nodes.monitor_performer import monitor_performer

    _, _, s = _kill_state()
    result = await monitor_performer(s)
    pending = result["observer_correction"]
    assert pending["signature"] == correction_signature("all motion, no artifacts")
    assert "all motion, no artifacts" in pending["body"]
    # The correction never dispatches on its own (426): it rides the payload.
    assert not result.get("relay_feedback")

    from coordinare.graph.nodes.dispatch_performer import inject_observer_correction

    payload: dict[str, Any] = {}
    inject_observer_correction(payload, result)
    items = payload["relay_feedback"]
    assert len(items) == 1
    assert items[0]["author_login"] == "observer"
    assert "all motion, no artifacts" in items[0]["body"]


# --- retry-counter escalation -----------------------------------------------------


@pytest.mark.asyncio
async def test_two_kills_in_a_window_escalate_to_blocked():
    from coordinare.graph.nodes.monitor_performer import monitor_performer

    performer, _backend, s = _kill_state()
    first = await monitor_performer(s)
    assert first["phase"] == "dispatching"

    # The graph dispatches a fresh session for the same card; the observer
    # judges that one dead too — the second kill inside the 24h window
    # trips the retry-counter escalation instead of re-dispatching.
    s["phase"] = "monitoring_performer"
    s["agent_dispatch"] = {"session_id": "s2"}
    s["agent_dispatch_at"] = datetime.now(UTC)
    s["observer_backend"] = _Backend(dict(KILL))
    s["last_production_at"] = datetime.now(UTC) - timedelta(seconds=600)
    second = await monitor_performer(s)
    assert second["phase"] == "blocked"
    assert any("kill" in str(q).lower() for q in second["open_questions"])
    assert performer.drain_calls == ["s1", "s2"], "the escalated turn was still stopped"


def test_the_kill_counter_is_namespaced_from_the_idle_timeout_counter():
    from coordinare.services.retry_counter import attempts_in_window, record_observer_kill

    s = _observer_state(_observer_cfg(), _Backend(dict(KILL)))
    s["active_sessions"] = {"ITEM_1": {}}
    assert record_observer_kill(s, "ITEM_1", "implementing") == "retry"
    assert record_observer_kill(s, "ITEM_1", "implementing") == "block"
    assert attempts_in_window(s, "ITEM_1", "implementing") == 0, (
        "a kill must not consume the idle-timeout budget"
    )


# --- the 076 mutex: one kill in flight per card -----------------------------------


@pytest.mark.asyncio
async def test_an_in_flight_reconciliation_pass_blocks_the_kill_until_it_settles():
    from coordinare.graph.nodes.monitor_performer import monitor_performer
    from coordinare.services.dispatch_guard import acquire_dispatch_lock

    performer, _, s = _kill_state()
    lock = acquire_dispatch_lock("ITEM_1", "implementing")
    await lock.acquire()  # a reconciliation pass (or dispatch) holds the card
    try:
        result = await monitor_performer(s)
        assert result["phase"] == "monitoring_performer"
        assert result["agent_dispatch"] == {"session_id": "s1"}, "the live session was not left alone"
        assert performer.drain_calls == [], "the kill ran while the card was being reconciled"
    finally:
        lock.release()

    result = await monitor_performer(s)
    assert result["phase"] == "dispatching", "the kill proceeded once the pass settled"
    assert performer.drain_calls == ["s1"]


# --- the kill only applies to a live turn ----------------------------------------


@pytest.mark.asyncio
async def test_a_terminal_turn_is_never_killed():
    """A terminal status carries finished work: the kill must not replace its
    normal stage advancement with a re-dispatch."""
    from coordinare.graph.nodes.monitor_performer import monitor_performer

    performer = _KillablePerformer({"status": "success"})
    backend = _Backend(dict(KILL))
    cfg = _observer_cfg(quiet_window_seconds=100.0)
    s = _observer_state(
        cfg, backend, performer,
        last_production_at=datetime.now(UTC) - timedelta(seconds=600),
    )
    result = await monitor_performer(s)
    assert result["phase"] != "dispatching", "the finished turn was rerun"
    assert result["agent_dispatch"] == {"session_id": "s1"}, "the finished turn was released"
    assert performer.drain_calls == [], "the finished turn was stopped"


@pytest.mark.asyncio
async def test_the_kill_releases_the_service_s_session_bookkeeping():
    """A stopped session must not leave its log-poll task and job entry
    behind: the kill hands the session to the service's own teardown
    hook, which is also where the stop stays runtime-abstract."""
    from coordinare.graph.nodes.monitor_performer import monitor_performer

    performer, _, s = _kill_state()
    cleanups: list[str] = []

    async def _cleanup(job_id: str) -> None:
        cleanups.append(job_id)
        performer._active_jobs.pop(job_id, None)

    performer._cleanup_ephemeral_job_by_id = _cleanup
    result = await monitor_performer(s)
    assert result["phase"] == "dispatching"
    assert cleanups == ["s1"], "the session bookkeeping was not released"
    assert performer._active_jobs == {"s2": performer._active_jobs.get("s2")}


@pytest.mark.asyncio
async def test_a_failed_docker_stop_fails_the_kill(monkeypatch):
    """docker stop returning False (both stop and kill failed) is a failed
    teardown: the kill fails safe instead of re-dispatching over a live
    container."""
    from coordinare.graph.nodes.monitor_performer import monitor_performer

    class _StubExecutor:
        async def stop_container(self, container_id: str, *, timeout: float = 5.0) -> bool:
            return False

    monkeypatch.setattr("coordinare.graph.nodes.monitor.body.DockerExecutor", _StubExecutor)
    performer = _KillablePerformer(drain_fails=True)
    backend = _Backend(dict(KILL))
    cfg = _observer_cfg(quiet_window_seconds=100.0)
    s = _observer_state(
        cfg, backend, performer,
        last_production_at=datetime.now(UTC) - timedelta(seconds=600),
    )
    result = await monitor_performer(s)
    assert result["phase"] == "monitoring_performer", "the card fell back to monitoring"
    assert result["agent_dispatch"] == {"session_id": "s1"}, "the live turn was untouched"
    assert not result.get("open_questions"), "no block was raised for a failed stop"


# --- fail-safe --------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_failed_kill_falls_back_to_today_s_behavior(monkeypatch):
    from coordinare.graph.nodes.monitor_performer import monitor_performer

    async def explode(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("docker unreachable")

    monkeypatch.setattr(
        "coordinare.services.dispatch_guard.stop_session_turn", explode,
    )
    _, _, s = _kill_state()
    result = await monitor_performer(s)
    assert result["phase"] == "monitoring_performer", "the card fell back to monitoring"
    assert result["agent_dispatch"] == {"session_id": "s1"}, "the live turn was untouched"
    assert not result.get("open_questions"), "no block was raised for a failed kill"


@pytest.mark.asyncio
async def test_an_untracked_or_failed_stop_never_redispatches(monkeypatch):
    """The teardown's honest signal drives the fail-safe: when the stop did
    not happen (stop failed, or the session was never tracked), the turn
    keeps running and nothing is re-dispatched."""
    from coordinare.graph.nodes.monitor_performer import monitor_performer

    async def refuse(*args: Any, **kwargs: Any) -> tuple[bool, str, float]:
        return False, "stop_failed", 0.0

    monkeypatch.setattr(
        "coordinare.services.dispatch_guard.stop_session_turn", refuse,
    )
    _, _, s = _kill_state()
    result = await monitor_performer(s)
    assert result["phase"] == "monitoring_performer", "the card fell back to monitoring"
    assert result["agent_dispatch"] == {"session_id": "s1"}, "the live turn was untouched"
    assert not result.get("open_questions"), "no block was raised for a failed stop"


@pytest.mark.asyncio
async def test_a_kill_failure_still_records_the_artefacts():
    """Artefact write-through precedes the teardown, so a failed kill cannot
    unrecord partial work."""
    from coordinare.graph.nodes.monitor_performer import monitor_performer

    async def explode(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("docker unreachable")

    monkey = pytest.MonkeyPatch()
    monkey.setattr("coordinare.services.dispatch_guard.stop_session_turn", explode)
    try:
        performer = _KillablePerformer(
            {
                "status": "working",
                "pr_url": "https://github.com/o/r/pull/9",
                "head_sha": "def456",
            },
        )
        backend = _Backend(dict(KILL))
        cfg = _observer_cfg(quiet_window_seconds=100.0)
        s = _observer_state(
            cfg, backend, performer,
            last_production_at=datetime.now(UTC) - timedelta(seconds=600),
        )
        result = await monitor_performer(s)
        assert result["current_card"]["pr_url"] == "https://github.com/o/r/pull/9"
        assert result["current_card"]["head_after"] == "def456"
    finally:
        monkey.undo()
