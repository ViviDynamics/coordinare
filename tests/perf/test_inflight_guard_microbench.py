"""Spec 076 T141 — in-flight guard microbenchmark.

Validates the spec's ≤5 ms-per-call hot-path budget for
``check_inflight``.  Uses an in-memory stub service (no I/O) so the
benchmark measures pure Python overhead.
"""
from __future__ import annotations

import time

import pytest

from coordinare.services.dispatch_guard import check_inflight

pytestmark = pytest.mark.benchmark


class _LiveSvc:
    def has_live_session(self, session_id: str) -> bool:
        return True


@pytest.mark.asyncio
async def test_check_inflight_microbench_under_5ms_p95() -> None:
    """In-flight guard MUST add ≤5 ms p95 to the dispatch hot path."""
    state: dict = {
        "agent_dispatch": {"session_id": "uuid-x"},
        "performer_services": {"implementing": _LiveSvc()},
    }

    samples: list[float] = []
    iterations = 10_000
    for _ in range(iterations):
        started = time.perf_counter()
        await check_inflight(state, "PVTI_X", "implementing")
        samples.append((time.perf_counter() - started) * 1000.0)  # ms

    samples.sort()
    p95 = samples[int(0.95 * iterations)]
    p99 = samples[int(0.99 * iterations)]
    assert p95 < 5.0, f"check_inflight p95={p95:.3f}ms, expected <5ms"
    assert p99 < 10.0, f"check_inflight p99={p99:.3f}ms, expected <10ms"


@pytest.mark.asyncio
async def test_check_inflight_no_session_microbench() -> None:
    """Empty agent_dispatch path (cold-card dispatch) MUST be at least
    as fast as the live-session path — no allocations, no service call."""
    state: dict = {}

    samples: list[float] = []
    iterations = 10_000
    for _ in range(iterations):
        started = time.perf_counter()
        await check_inflight(state, "PVTI_FRESH", "implementing")
        samples.append((time.perf_counter() - started) * 1000.0)

    samples.sort()
    p95 = samples[int(0.95 * iterations)]
    assert p95 < 1.0, f"empty-state check_inflight p95={p95:.3f}ms, expected <1ms"
