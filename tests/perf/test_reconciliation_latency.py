"""Spec 076 T140 — reconciliation pass latency benchmark.

Validates SC-002:
- p95 wall-clock ≤ 30 s with 5 in-flight cards
- zero-card path ≤ 500 ms

Subprocess-free: uses a mocked DockerExecutor that returns canned
containers without spawning real docker processes.  Suitable for CI.
"""
from __future__ import annotations

import time
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from coordinare.services.docker_executor import ContainerInfo, DockerUnreachableError
from coordinare.services.reconciliation import run_startup_reconciliation

pytestmark = pytest.mark.benchmark


@pytest.fixture(autouse=True)
def runner_status(monkeypatch: pytest.MonkeyPatch) -> None:
    """Model the healthy runner's read-only identity API without network calls."""
    monkeypatch.setattr(
        "coordinare.transport.http_transport.PerformerHTTPClient.get_status",
        AsyncMock(return_value=SimpleNamespace(current_job_id="restored-runner-job")),
    )


class _LatencyMockDocker:
    """Returns containers immediately; no real docker process."""

    def __init__(self, *, containers: list[ContainerInfo], healthy: bool = True) -> None:
        self._containers = list(containers)
        self._healthy = healthy

    async def list_containers_by_label(self, label_filters, *, timeout=5.0):
        return [
            c for c in self._containers
            if all(c.labels.get(k) == v for k, v in label_filters.items())
        ]

    async def stop_container(self, container_id, *, timeout=5.0):
        return True

    async def port_of(self, container_id, *, internal_port=8088):
        return 55555 if self._healthy else None

    async def probe_healthz(self, container_id, *, timeout=15.0, **kw):
        return self._healthy


class _Svc:
    """Minimal performer service shape for the benchmark."""

    def __init__(self) -> None:
        self._active_jobs: dict = {}

        class _C:
            mode: str = "ephemeral"

        self._config = _C()

    def _auth_token(self):
        return None


def _container(session_id: str, container_id: str = "ctr") -> ContainerInfo:
    return ContainerInfo(
        container_id=container_id,
        name=f"n-{container_id}",
        image="coordinare-performer:full",
        started_at=datetime(2026, 5, 28, 22, 0, 0, tzinfo=UTC),
        labels={
            "coordinare.performer.id": "ephemeral",
            "coordinare.session_id": session_id,
            "coordinare.card_id": f"PVTI_{session_id[:4]}",
            "coordinare.performer_stage": "implementing",
            "coordinare.daemon_started_at": "2026-05-28T21:00:00Z",
            "coordinare.spec_version": "076",
        },
    )


@pytest.mark.asyncio
async def test_sc002_zero_in_flight_fast_path_under_500ms() -> None:
    """SC-002: snapshot with zero in-flight sessions MUST add no more
    than 500 ms to startup time.  Achieved via the fast-path that
    skips Docker enumeration entirely when active_sessions is empty."""
    state = {"active_sessions": {}}
    executor = _LatencyMockDocker(containers=[])

    samples: list[float] = []
    for _ in range(20):
        started = time.perf_counter()
        await run_startup_reconciliation(state, executor, budget_seconds=30.0)
        samples.append(time.perf_counter() - started)

    samples.sort()
    p95 = samples[int(0.95 * len(samples))]
    assert p95 < 0.5, f"zero-card fast path p95={p95:.4f}s, expected <0.5s"


@pytest.mark.asyncio
async def test_sc002_five_in_flight_cards_under_30s_p95() -> None:
    """SC-002: snapshot with up to max_concurrent_cards (5) sessions
    MUST complete reconciliation within 30 s p95."""
    sessions: dict = {}
    containers: list[ContainerInfo] = []
    for i in range(5):
        card_id = f"PVTI_X{i}"
        session_id = f"uuid-{i}"
        sessions[card_id] = {
            "phase": "monitoring_performer",
            "performer_stage": "implementing",
            "agent_dispatch": {"session_id": session_id},
        }
        containers.append(_container(session_id, container_id=f"ctr-{i}"))

    state = {
        "active_sessions": sessions,
        "performer_services": {"implementing": _Svc()},
    }
    executor = _LatencyMockDocker(containers=containers, healthy=True)

    samples: list[float] = []
    for _ in range(20):
        # Reset per-iteration so we measure each pass fresh
        for s in sessions.values():
            s["agent_dispatch"] = {"session_id": s["agent_dispatch"]["session_id"]}
        started = time.perf_counter()
        await run_startup_reconciliation(state, executor, budget_seconds=30.0)
        samples.append(time.perf_counter() - started)

    samples.sort()
    p95 = samples[int(0.95 * len(samples))]
    assert p95 < 30.0, f"5-card reconciliation p95={p95:.4f}s, expected <30s"


@pytest.mark.asyncio
async def test_docker_unreachable_completes_quickly() -> None:
    """FR-012: Docker-down failure mode MUST not block — pass returns
    quickly with docker_unreachable=True."""

    class _Down:
        async def list_containers_by_label(self, *a, **kw):
            raise DockerUnreachableError("simulated")

        async def stop_container(self, *a, **kw):
            return True

        async def port_of(self, *a, **kw):
            return None

        async def probe_healthz(self, *a, **kw):
            return False

    state = {
        "active_sessions": {
            "PVTI_X": {
                "phase": "monitoring_performer",
                "performer_stage": "implementing",
                "agent_dispatch": {"session_id": "uuid-x"},
            },
        },
        "performer_services": {"implementing": _Svc()},
    }
    started = time.perf_counter()
    report = await run_startup_reconciliation(state, _Down(), budget_seconds=30.0)
    elapsed = time.perf_counter() - started
    assert report.docker_unreachable is True
    assert elapsed < 2.0, f"docker-down path took {elapsed}s; expected <2s"
