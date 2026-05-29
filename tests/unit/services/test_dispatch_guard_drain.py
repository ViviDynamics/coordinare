"""Spec 076 T088 — drain_or_reap unit tests.

Covers FR-007 / clarification Q5: drain first with 5s budget, force-stop
within additional 5s if drain doesn't complete, total ≤10s.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass

import pytest

from coordinare.services.dispatch_guard import drain_or_reap


@dataclass
class _FakeJob:
    container_id: str


class _DrainSucceedingService:
    def __init__(self) -> None:
        self._active_jobs: dict = {"sess-x": _FakeJob(container_id="ctr-1")}
        self.drained_sessions: list[str] = []

    async def drain_session(self, session_id: str) -> None:
        self.drained_sessions.append(session_id)
        # Drain completes immediately


class _DrainTimingOutService:
    def __init__(self) -> None:
        self._active_jobs: dict = {"sess-x": _FakeJob(container_id="ctr-1")}

    async def drain_session(self, session_id: str) -> None:
        await asyncio.sleep(10.0)  # Always longer than the drain budget


class _DrainErrorService:
    def __init__(self) -> None:
        self._active_jobs: dict = {"sess-x": _FakeJob(container_id="ctr-1")}

    async def drain_session(self, session_id: str) -> None:
        raise RuntimeError("drain endpoint refused")


class _NoDrainSupportService:
    """A service with no drain_session method — older performer shape."""

    def __init__(self) -> None:
        self._active_jobs: dict = {"sess-x": _FakeJob(container_id="ctr-1")}


class _MockExecutor:
    def __init__(self, *, stop_succeeds: bool = True) -> None:
        self.stopped: list[str] = []
        self._stop_succeeds = stop_succeeds

    async def stop_container(self, container_id: str, *, timeout: float = 5.0) -> bool:
        self.stopped.append(container_id)
        return self._stop_succeeds


@pytest.mark.asyncio
async def test_drain_succeeds_within_budget_returns_drained() -> None:
    """When the service's drain_session returns before the budget
    elapses, drain_or_reap reports ``drained`` and does NOT stop the
    container."""
    svc = _DrainSucceedingService()
    executor = _MockExecutor()
    outcome, elapsed_ms = await drain_or_reap(
        "sess-x", service=svc, docker_executor=executor, drain_budget=5.0
    )
    assert outcome == "drained"
    assert "sess-x" in svc.drained_sessions
    # Container NOT stopped — drain succeeded
    assert executor.stopped == []
    assert elapsed_ms < 5000


@pytest.mark.asyncio
async def test_drain_timeout_falls_back_to_reap() -> None:
    """If drain hangs beyond the budget, docker_stop is invoked and the
    outcome is ``reaped``.  Total wall-clock ≤ drain + reap budget."""
    svc = _DrainTimingOutService()
    executor = _MockExecutor()
    outcome, elapsed_ms = await drain_or_reap(
        "sess-x",
        service=svc,
        docker_executor=executor,
        drain_budget=0.1,  # 100ms — well below the simulated 10s drain
        reap_budget=2.0,
    )
    assert outcome == "reaped"
    assert "ctr-1" in executor.stopped
    # Total budget honoured (drain budget elapsed + reap)
    assert elapsed_ms < 2500


@pytest.mark.asyncio
async def test_drain_error_falls_back_to_reap() -> None:
    """If drain raises, fall through to reap (best-effort; the relay
    must not wedge on a broken drain endpoint)."""
    svc = _DrainErrorService()
    executor = _MockExecutor()
    outcome, _ = await drain_or_reap(
        "sess-x", service=svc, docker_executor=executor
    )
    assert outcome == "reaped"
    assert "ctr-1" in executor.stopped


@pytest.mark.asyncio
async def test_service_without_drain_support_falls_back_to_reap() -> None:
    """Older performer images don't have drain_session.  Skip drain
    entirely and reap directly."""
    svc = _NoDrainSupportService()
    executor = _MockExecutor()
    outcome, _ = await drain_or_reap("sess-x", service=svc, docker_executor=executor)
    assert outcome == "reaped"
    assert "ctr-1" in executor.stopped


@pytest.mark.asyncio
async def test_stop_failure_still_returns_reaped() -> None:
    """If docker_stop itself fails, drain_or_reap still returns
    ``reaped`` so the relay can proceed.  The daemon.reap_failed log
    event surfaces the issue separately."""
    svc = _NoDrainSupportService()
    executor = _MockExecutor(stop_succeeds=False)
    outcome, _ = await drain_or_reap("sess-x", service=svc, docker_executor=executor)
    assert outcome == "reaped"


@pytest.mark.asyncio
async def test_missing_service_or_executor_short_circuits_to_reaped() -> None:
    """With no service/executor (defensive call) → returns reaped
    without crashing."""
    outcome, _ = await drain_or_reap("sess-x", service=None, docker_executor=None)
    assert outcome == "reaped"


@pytest.mark.asyncio
async def test_total_budget_hard_cap_10_seconds() -> None:
    """The total wall-clock for drain_or_reap MUST NOT exceed
    drain_budget + reap_budget (10s default) under any circumstances."""
    svc = _DrainTimingOutService()
    executor = _MockExecutor()
    from time import perf_counter
    started = perf_counter()
    await drain_or_reap(
        "sess-x", service=svc, docker_executor=executor, drain_budget=1.0, reap_budget=1.0
    )
    elapsed = perf_counter() - started
    assert elapsed < 3.0  # 1s drain + 1s reap + slack
