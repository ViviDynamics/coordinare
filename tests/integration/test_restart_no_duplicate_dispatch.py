"""Spec 076 T066 (FR-014) — regression for today's duplicate-dispatch incident.

Reconstructs the exact failure mode observed on 2026-05-28: persisted
snapshot showing card IN_PROGRESS with a session_id, no entry in the
fresh process's ``_active_jobs``, a still-running container labelled
with that session_id.

Asserts that after one reconciliation pass the system reaches a clean
state — either ADOPTED (container kept, dispatched to new _active_jobs)
or REAPED_AND_REPLACED (container stopped, agent_dispatch cleared) —
NEVER two containers, NEVER zero (or some other invalid combination).
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from coordinare.services.docker_executor import ContainerInfo, DockerUnreachableError
from coordinare.services.reconciliation import (
    ReconciliationDecision,
    run_startup_reconciliation,
)

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


class _DockerWithOneRunningContainer:
    """The fresh daemon's view of Docker: ONE container, labelled with
    the persisted session_id, still running."""

    def __init__(self, *, healthy: bool) -> None:
        self.container = ContainerInfo(
            container_id="ctr-from-prior-daemon",
            name="compassionate_meitner",
            image="coordinare-performer:full",
            started_at=datetime(2026, 5, 28, 22, 6, 50, tzinfo=UTC),
            labels={
                "coordinare.performer.id": "claude-ephemeral",
                "coordinare.session_id": "uuid-from-prior-daemon",
                "coordinare.card_id": "PVTI_TODAY",
                "coordinare.performer_stage": "implementing",
                "coordinare.daemon_started_at": "2026-05-28T21:14:17Z",
                "coordinare.spec_version": "076",
            },
        )
        self._healthy = healthy
        self.stopped: list[str] = []
        self.port_calls: list[str] = []

    async def list_containers_by_label(self, label_filters, *, timeout=5.0):
        if all(self.container.labels.get(k) == v for k, v in label_filters.items()):
            return [self.container]
        return []

    async def stop_container(self, container_id, *, timeout=5.0):
        self.stopped.append(container_id)
        return True

    async def port_of(self, container_id, *, internal_port=8088):
        self.port_calls.append(container_id)
        return 55555 if self._healthy else None

    async def probe_healthz(self, container_id, *, timeout=15.0, internal_port=8088, auth_token=None):
        return self._healthy


class _FakeEphemeralService:
    """Fresh daemon's performer service: empty _active_jobs (as on every
    restart), ephemeral mode."""

    def __init__(self) -> None:
        self._active_jobs: dict = {}

        class _Cfg:
            mode = "ephemeral"
        self._config = _Cfg()

    def _auth_token(self):
        return None

    def has_live_session(self, session_id: str) -> bool:
        return session_id in self._active_jobs


# ---------------------------------------------------------------------------
# FR-014 regression scenarios
# ---------------------------------------------------------------------------


def _make_persisted_state(svc: _FakeEphemeralService) -> dict:
    """Build the exact state shape the daemon constructs after snapshot
    rehydration: card IN_PROGRESS, session_id present in agent_dispatch,
    _active_jobs empty (cold process)."""
    return {
        "active_sessions": {
            "PVTI_TODAY": {
                "phase": "monitoring_performer",
                "performer_stage": "implementing",
                "agent_dispatch": {"session_id": "uuid-from-prior-daemon"},
                "current_card": {
                    "id": "PVTI_TODAY",
                    "title": "Time tracking schema",
                    "status": "IN_PROGRESS",
                },
            },
        },
        "performer_services": {"implementing": svc},
    }


@pytest.mark.asyncio
async def test_fr014_one_container_after_restart_when_prior_container_unhealthy() -> None:
    """When the prior container's job-runner doesn't respond, the
    reconciliation pass MUST stop it (FR-004 reap-and-replace).  The new
    daemon's _active_jobs MUST stay empty; the next graph cycle will
    fresh-dispatch a single replacement container.

    Steady state: ONE container worth of work in flight — never two."""
    svc = _FakeEphemeralService()
    state = _make_persisted_state(svc)
    docker = _DockerWithOneRunningContainer(healthy=False)

    report = await run_startup_reconciliation(state, docker, budget_seconds=10.0)

    assert report.decisions["PVTI_TODAY"] == ReconciliationDecision.REAPED_AND_REPLACED
    # Prior container is stopped
    assert "ctr-from-prior-daemon" in docker.stopped
    # _active_jobs MUST be empty (the fresh dispatch will populate it
    # via the normal dispatch_performer path on the next cycle)
    assert svc._active_jobs == {}
    # agent_dispatch is cleared so dispatch_performer will see no
    # session and proceed to launch a single new container
    assert state["active_sessions"]["PVTI_TODAY"]["agent_dispatch"] == {}
    assert state["active_sessions"]["PVTI_TODAY"]["phase"] == "dispatching"


@pytest.mark.asyncio
async def test_fr014_adopted_client_points_at_real_host_port() -> None:
    """Code-review #1 regression: a healthy container being adopted MUST
    end up registered with a client whose endpoint resolves to the
    container's REAL host port — not the ``127.0.0.1:0`` placeholder.
    Without this, subsequent monitor_card calls would silently fail and
    the card would be re-dispatched anyway."""
    svc = _FakeEphemeralService()
    state = _make_persisted_state(svc)
    docker = _DockerWithOneRunningContainer(healthy=True)

    await run_startup_reconciliation(state, docker, budget_seconds=10.0)

    # Adopted entry exists
    assert "uuid-from-prior-daemon" in svc._active_jobs
    job = svc._active_jobs["uuid-from-prior-daemon"]
    # Endpoint resolves to the real host port returned by
    # _DockerWithOneRunningContainer.port_of (55555 when healthy)
    assert ":0" not in job.endpoint, f"adopted client points at placeholder port: {job.endpoint}"
    assert job.endpoint == "http://127.0.0.1:55555"


@pytest.mark.asyncio
async def test_fr014_one_container_after_restart_when_prior_container_healthy() -> None:
    """When the prior container's job-runner responds healthily, the
    reconciliation pass MUST adopt it (FR-003).  The new daemon's
    _active_jobs MUST contain ONE entry keyed on the persisted
    session_id; the prior container is NOT stopped; no fresh dispatch
    occurs.

    Steady state: ONE container, same one, work preserved."""
    svc = _FakeEphemeralService()
    state = _make_persisted_state(svc)
    docker = _DockerWithOneRunningContainer(healthy=True)

    report = await run_startup_reconciliation(state, docker, budget_seconds=10.0)

    assert report.decisions["PVTI_TODAY"] == ReconciliationDecision.ADOPTED
    # Prior container NOT stopped
    assert "ctr-from-prior-daemon" not in docker.stopped
    # _active_jobs MUST contain the adopted session — exactly one entry
    assert "uuid-from-prior-daemon" in svc._active_jobs
    assert len(svc._active_jobs) == 1
    # agent_dispatch preserved — no re-dispatch will happen
    assert state["active_sessions"]["PVTI_TODAY"]["agent_dispatch"] == {
        "session_id": "uuid-from-prior-daemon",
    }


@pytest.mark.asyncio
async def test_fr014_never_two_containers_for_same_card_under_repeated_restarts() -> None:
    """Run the reconciliation pass repeatedly — simulating multiple
    daemon restarts.  After every restart, at most ONE container exists
    for the card, and the new daemon's _active_jobs has at most ONE
    entry for it.  Never zero (orphan still running but not in
    _active_jobs), never two."""
    svc = _FakeEphemeralService()
    state = _make_persisted_state(svc)
    docker = _DockerWithOneRunningContainer(healthy=True)

    # Simulate 3 back-to-back daemon restarts
    for _ in range(3):
        await run_startup_reconciliation(state, docker, budget_seconds=10.0)
        # Steady-state invariant after EVERY pass:
        # one matching entry in _active_jobs, prior container NOT stopped
        assert len(svc._active_jobs) == 1
        assert "uuid-from-prior-daemon" in svc._active_jobs
        # No accidental stop of the live container
        assert docker.stopped == []


@pytest.mark.asyncio
async def test_fr014_docker_unreachable_does_not_corrupt_state() -> None:
    """FR-012: when Docker is unreachable, reconciliation must NOT
    silently clear agent_dispatch (that would race with the prior
    container's still-running job)."""
    svc = _FakeEphemeralService()
    state = _make_persisted_state(svc)

    class _DownDocker:
        async def list_containers_by_label(self, *a, **kw):
            raise DockerUnreachableError("simulated")

        async def stop_container(self, *a, **kw):  # pragma: no cover
            return True

        async def port_of(self, *a, **kw):  # pragma: no cover
            return None

    report = await run_startup_reconciliation(state, _DownDocker(), budget_seconds=10.0)

    assert report.docker_unreachable is True
    # State MUST be untouched — agent_dispatch still references the prior session
    assert state["active_sessions"]["PVTI_TODAY"]["agent_dispatch"] == {
        "session_id": "uuid-from-prior-daemon",
    }
    assert state["active_sessions"]["PVTI_TODAY"]["phase"] == "monitoring_performer"
