"""Spec 076 T087 — monitor_performer IDLE_TIMEOUT handler.

Covers FR-019 (idle-timeout retry budget) integration with the
performer-status parser.  Tests run the retry_counter at the
monitor_performer boundary; full retry_counter coverage lives in
``test_retry_counter.py``.
"""
from __future__ import annotations

import pytest


def _state_with_in_flight_session(card_id: str = "PVTI_X") -> dict:
    return {
        "active_sessions": {
            card_id: {
                "phase": "monitoring_performer",
                "performer_stage": "implementing",
                "idle_timeout_retries": {},
            },
        },
        "agent_dispatch": {"session_id": "uuid-x"},
        "performer_services": {},
    }


def test_idle_timeout_marker_routes_through_retry_counter() -> None:
    """When the performer emits ``outcome=idle_timeout``, the retry
    counter MUST be invoked at the monitor_performer boundary."""
    # We exercise the retry_counter directly here because the full
    # monitor_performer test harness needs a github_service + many other
    # things — the integration-level wiring is covered in the
    # smoke-test below.  This confirms the contract.
    from coordinare.services.retry_counter import record_idle_timeout

    state = _state_with_in_flight_session()
    decision = record_idle_timeout(state, "PVTI_X", "implementing", budget=2)
    assert decision == "retry"
    # Counter persisted on the session
    retries = state["active_sessions"]["PVTI_X"]["idle_timeout_retries"]
    assert "PVTI_X:implementing" in retries
    assert retries["PVTI_X:implementing"]["attempt_count"] == 1


def test_idle_timeout_at_budget_returns_block() -> None:
    """3rd idle-timeout (budget=2) → ``block``.  monitor_performer's
    IDLE_TIMEOUT handler routes this to phase=blocked."""
    from coordinare.services.retry_counter import record_idle_timeout

    state = _state_with_in_flight_session()
    record_idle_timeout(state, "PVTI_X", "implementing", budget=2)
    record_idle_timeout(state, "PVTI_X", "implementing", budget=2)
    decision = record_idle_timeout(state, "PVTI_X", "implementing", budget=2)
    assert decision == "block"


class _IdleTimeoutPerformer:
    """Service that returns an idle_timeout status from check_status."""

    def __init__(self, marker: str = "idle_timeout") -> None:
        self._marker = marker

    async def check_status(self, session_id: str, **kwargs):
        if self._marker == "idle_timeout":
            return {"status": "idle_timeout", "reason": "claude code reader idle timeout"}
        return {"status": "error", "reason": "claude code reader idle timeout had_output=False"}


class _RealIdleErrorPerformer:
    """Returns the EXACT error string the claude_code backend emits on a
    no-output idle timeout (agent/performer/.../claude_code.py:515).

    This is the string card #101 actually hit on 2026-05-29 — it does
    NOT contain the substring "idle timeout", so the original 076
    detector missed it and the card fell through to the generic blocked
    path instead of the FR-019 retry counter.
    """

    async def check_status(self, session_id: str, **kwargs):
        return {
            "status": "error",
            "reason": "claude CLI idle for 600s with no terminal event",
        }


class _IdleStopReasonPerformer:
    """Returns the done-with-output idle case: stop_reason=idle_timeout."""

    async def check_status(self, session_id: str, **kwargs):
        return {"status": "error", "stop_reason": "idle_timeout", "reason": ""}


@pytest.mark.asyncio
async def test_monitor_performer_idle_timeout_first_retry() -> None:
    """End-to-end: first idle-timeout outcome routes through the retry
    counter and returns phase=dispatching for fresh dispatch."""
    from coordinare.graph.nodes.monitor_performer import monitor_performer
    from coordinare.graph.state import initial_state

    state = initial_state()
    state["performer_services"] = {"implementing": _IdleTimeoutPerformer()}
    state["performer_stage"] = "implementing"
    state["lifecycle_sequence"] = ["implementing"]
    state["current_card"] = {"id": "PVTI_IDLE", "status": "IN_PROGRESS"}
    state["agent_dispatch"] = {"session_id": "uuid-x"}
    state["active_sessions"] = {
        "PVTI_IDLE": {
            "phase": "monitoring_performer",
            "performer_stage": "implementing",
            "idle_timeout_retries": {},
            "agent_dispatch": {"session_id": "uuid-x"},
        },
    }

    result = await monitor_performer(state)

    # Retry path → phase=dispatching, agent_dispatch cleared
    assert result.get("phase") == "dispatching"
    assert result.get("agent_dispatch") == {}
    # Retry counter incremented
    retries = state["active_sessions"]["PVTI_IDLE"]["idle_timeout_retries"]
    assert "PVTI_IDLE:implementing" in retries
    assert retries["PVTI_IDLE:implementing"]["attempt_count"] == 1


@pytest.mark.asyncio
async def test_monitor_performer_idle_timeout_via_error_marker() -> None:
    """The 'error' marker with 'idle timeout' substring in reason ALSO
    routes through the retry counter (FR-019)."""
    from coordinare.graph.nodes.monitor_performer import monitor_performer
    from coordinare.graph.state import initial_state

    state = initial_state()
    state["performer_services"] = {"implementing": _IdleTimeoutPerformer(marker="error")}
    state["performer_stage"] = "implementing"
    state["lifecycle_sequence"] = ["implementing"]
    state["current_card"] = {"id": "PVTI_ERR", "status": "IN_PROGRESS"}
    state["agent_dispatch"] = {"session_id": "uuid-x"}
    state["active_sessions"] = {
        "PVTI_ERR": {
            "phase": "monitoring_performer",
            "performer_stage": "implementing",
            "idle_timeout_retries": {},
            "agent_dispatch": {"session_id": "uuid-x"},
        },
    }

    result = await monitor_performer(state)

    assert result.get("phase") == "dispatching"
    retries = state["active_sessions"]["PVTI_ERR"]["idle_timeout_retries"]
    assert "PVTI_ERR:implementing" in retries


@pytest.mark.asyncio
async def test_monitor_performer_real_claude_idle_error_string_routes_to_retry() -> None:
    """REGRESSION (card #101, 2026-05-29): the claude_code backend emits
    'claude CLI idle for 600s with no terminal event' on a no-output
    idle timeout.  This string does NOT contain 'idle timeout', so the
    original 076 detector missed it and the card fell through to the
    blocked path.  The fixed detector MUST route it through the FR-019
    retry counter."""
    from coordinare.graph.nodes.monitor_performer import monitor_performer
    from coordinare.graph.state import initial_state

    state = initial_state()
    state["performer_services"] = {"implementing": _RealIdleErrorPerformer()}
    state["performer_stage"] = "implementing"
    state["lifecycle_sequence"] = ["implementing"]
    state["current_card"] = {"id": "PVTI_101", "status": "IN_PROGRESS"}
    state["agent_dispatch"] = {"session_id": "uuid-x"}
    state["active_sessions"] = {
        "PVTI_101": {
            "phase": "monitoring_performer",
            "performer_stage": "implementing",
            "idle_timeout_retries": {},
            "agent_dispatch": {"session_id": "uuid-x"},
        },
    }

    result = await monitor_performer(state)

    # Retry path taken (NOT blocked) — counter incremented
    assert result.get("phase") == "dispatching"
    retries = state["active_sessions"]["PVTI_101"]["idle_timeout_retries"]
    assert "PVTI_101:implementing" in retries
    assert retries["PVTI_101:implementing"]["attempt_count"] == 1


@pytest.mark.asyncio
async def test_monitor_performer_stop_reason_idle_timeout_routes_to_retry() -> None:
    """The stop_reason=idle_timeout field (done-with-output idle case)
    is also detected as an idle-timeout and routed through the retry
    counter rather than the generic error/blocked path."""
    from coordinare.graph.nodes.monitor_performer import monitor_performer
    from coordinare.graph.state import initial_state

    state = initial_state()
    state["performer_services"] = {"implementing": _IdleStopReasonPerformer()}
    state["performer_stage"] = "implementing"
    state["lifecycle_sequence"] = ["implementing"]
    state["current_card"] = {"id": "PVTI_SR", "status": "IN_PROGRESS"}
    state["agent_dispatch"] = {"session_id": "uuid-sr"}
    state["active_sessions"] = {
        "PVTI_SR": {
            "phase": "monitoring_performer",
            "performer_stage": "implementing",
            "idle_timeout_retries": {},
            "agent_dispatch": {"session_id": "uuid-sr"},
        },
    }

    result = await monitor_performer(state)
    assert result.get("phase") == "dispatching"
    assert "PVTI_SR:implementing" in state["active_sessions"]["PVTI_SR"]["idle_timeout_retries"]


@pytest.mark.asyncio
async def test_monitor_performer_idle_timeout_exhausted_blocks() -> None:
    """After 2 retries (budget exhausted), idle-timeout moves card to
    BLOCKED with operator-intervention message."""
    from coordinare.graph.nodes.monitor_performer import monitor_performer
    from coordinare.graph.state import initial_state
    from coordinare.services.retry_counter import record_idle_timeout

    state = initial_state()
    state["performer_services"] = {"implementing": _IdleTimeoutPerformer()}
    state["performer_stage"] = "implementing"
    state["lifecycle_sequence"] = ["implementing"]
    state["current_card"] = {"id": "PVTI_EXH", "status": "IN_PROGRESS"}
    state["agent_dispatch"] = {"session_id": "uuid-x"}
    state["active_sessions"] = {
        "PVTI_EXH": {
            "phase": "monitoring_performer",
            "performer_stage": "implementing",
            "idle_timeout_retries": {},
            "agent_dispatch": {"session_id": "uuid-x"},
        },
    }

    # Pre-exhaust the counter
    record_idle_timeout(state, "PVTI_EXH", "implementing", budget=2)
    record_idle_timeout(state, "PVTI_EXH", "implementing", budget=2)

    # The 3rd call from monitor_performer → block
    result = await monitor_performer(state)

    assert result.get("phase") == "blocked"
    assert "idle-timed-out" in str(result.get("open_questions", [""])[0]).lower()


@pytest.mark.asyncio
async def test_monitor_performer_partial_progress_invokes_drain(monkeypatch) -> None:
    """The partial_progress branch in monitor_performer MUST call
    drain_or_reap on the prior container before clearing agent_dispatch
    (FR-007 / clarification Q5)."""
    from coordinare.graph.nodes.monitor_performer import monitor_performer
    from coordinare.graph.state import initial_state

    drain_calls: list[str] = []

    async def _fake_drain_or_reap(session_id, *, service=None, docker_executor=None, **kw):
        drain_calls.append(session_id)
        return ("drained", 10.0)

    monkeypatch.setattr(
        "coordinare.services.dispatch_guard.drain_or_reap",
        _fake_drain_or_reap,
    )

    class _PartialProgressService:
        async def check_status(self, session_id, **kwargs):
            return {
                "status": "partial_progress",
                "next_focus": "finish migrations",
                "reason": "checkpoint",
            }

    state = initial_state()
    state["performer_services"] = {"implementing": _PartialProgressService()}
    state["performer_stage"] = "implementing"
    state["lifecycle_sequence"] = ["implementing"]
    state["current_card"] = {"id": "PVTI_PP", "status": "IN_PROGRESS"}
    state["agent_dispatch"] = {"session_id": "uuid-pp"}
    state["active_sessions"] = {
        "PVTI_PP": {
            "phase": "monitoring_performer",
            "performer_stage": "implementing",
            "agent_dispatch": {"session_id": "uuid-pp"},
        },
    }

    result = await monitor_performer(state)

    # Drain was called with the prior session id
    assert "uuid-pp" in drain_calls
    # Relay path: phase=dispatching, agent_dispatch cleared
    assert result.get("phase") == "dispatching"
    assert result.get("agent_dispatch") == {}


@pytest.mark.asyncio
async def test_idle_timeout_handler_smoke_persistence_across_session_round_trip() -> None:
    """The retry counter MUST persist across the session ↔ snapshot
    round-trip.  A daemon restart MUST NOT launder away the counter so a
    qwen-stall loop re-attempts forever (the bug 076 closes)."""
    from coordinare.graph.state import initial_state
    from coordinare.services.retry_counter import record_idle_timeout
    from coordinare.session import session_to_state, state_to_session

    state = _state_with_in_flight_session()
    record_idle_timeout(state, "PVTI_X", "implementing", budget=2)
    record_idle_timeout(state, "PVTI_X", "implementing", budget=2)

    # Snapshot write + read: simulates daemon restart
    sess = state["active_sessions"]["PVTI_X"]
    fresh = initial_state()
    session_to_state(sess, fresh)
    rehydrated = state_to_session(fresh)

    # Counter preserved
    retries = rehydrated["idle_timeout_retries"]
    assert "PVTI_X:implementing" in retries
    assert retries["PVTI_X:implementing"]["attempt_count"] == 2
