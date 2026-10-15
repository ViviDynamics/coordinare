from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from prometheus_client import CollectorRegistry, generate_latest

from coordinare.graph.nodes import dispatch_performer as dispatch_node
from coordinare.graph.nodes import monitor_performer as monitor_node
from coordinare.health import create_health_app
from coordinare.mcp.client import DashboardClient
from coordinare.mcp.config import McpServerConfig
from coordinare.mcp.tools.state_tools import get_metrics_handler
from coordinare.services.event_store import EventStore
from coordinare.services.export_metrics import ExportMetrics
from tests.unit.graph.nodes.test_dispatch_performer import _base_state as dispatch_fixture
from tests.unit.graph.nodes.test_dispatch_performer import _Service as DispatchService
from tests.unit.graph.nodes.test_dispatch_performer import (
    _ServiceHealthError as DispatchHealthError,
)


@pytest.mark.asyncio
async def test_metrics_route_requires_opt_in() -> None:
    daemon: Any = SimpleNamespace()
    for enabled in (False, True):
        app = create_health_app(daemon, metrics_enabled=enabled)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://health",
        ) as client:
            response = await client.get("/metrics")
        assert response.status_code == (200 if enabled else 404)


def test_lifecycle_and_bootstrap_metrics_use_bounded_labels() -> None:
    registry = CollectorRegistry()
    metrics = ExportMetrics(registry)
    metrics.run_started("implementing")
    metrics.run_finished("implementing", "completed")
    metrics.run_finished("attacker-stage", "attacker-outcome")
    metrics.observe({"source": "bootstrap", "kind": "env_cache.bootstrap_complete", "data": '{"success":true}'})
    metrics.observe({"source": "bootstrap", "kind": "env_cache.bootstrap_complete", "data": '{"success":false}'})
    output = generate_latest(registry).decode()
    assert 'coordinare_runs_started_total{stage="implementing"} 1.0' in output
    assert 'coordinare_runs_finished_total{outcome="completed",stage="implementing"} 1.0' in output
    assert 'coordinare_bootstrap_outcomes_total{outcome="succeeded"} 1.0' in output
    assert 'coordinare_bootstrap_outcomes_total{outcome="failed"} 1.0' in output
    assert "attacker" not in output


@pytest.mark.asyncio
async def test_store_metrics_track_writes_drops_and_pruning(tmp_path: Path) -> None:
    registry = CollectorRegistry()
    metrics = ExportMetrics(registry)
    with patch("coordinare.services.event_store.EXPORT_METRICS", metrics):
        store = EventStore(tmp_path / "events.db", queue_size=1)
        assert store.emit({"ts": "2000-01-01T00:00:00+00:00"})
        assert not store.emit({})
        await store.start()
        await store.flush()
        assert await store.prune() == 1
        await store.close()
    output = generate_latest(registry).decode()
    for name in ("written", "dropped", "pruned"):
        assert f"coordinare_event_store_{name}_total 1.0" in output


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("phase", "outcome"),
    [
        ("monitoring_pr", "completed"),
        ("blocked", "blocked"),
        ("system_error", "system_error"),
        ("dispatching", "redispatched"),
        ("monitoring_performer", None),
    ],
)
async def test_monitor_counts_original_stage_once(
    monkeypatch: pytest.MonkeyPatch, phase: str, outcome: str | None,
) -> None:
    registry = CollectorRegistry()
    metrics = ExportMetrics(registry)

    async def body(state: dict[str, Any]) -> dict[str, Any]:
        state.update(phase=phase, performer_stage="reviewing")
        return state

    monkeypatch.setattr(monitor_node, "_monitor_performer_body", body)
    monkeypatch.setattr(monitor_node, "EXPORT_METRICS", metrics)
    state: Any = {"phase": "monitoring_performer", "performer_stage": "implementing"}
    await monitor_node.monitor_performer(state)
    output = generate_latest(registry).decode()
    if outcome is None:
        assert 'stage="implementing"' not in output
    else:
        assert (
            f'coordinare_runs_finished_total{{outcome="{outcome}",stage="implementing"}} 1.0'
            in output
        )


@pytest.mark.asyncio
@pytest.mark.parametrize("accepted", [False, True])
async def test_dispatch_counter_only_after_acceptance(
    monkeypatch: pytest.MonkeyPatch, accepted: bool,
) -> None:
    registry = CollectorRegistry()
    metrics = ExportMetrics(registry)
    monkeypatch.setattr(dispatch_node, "EXPORT_METRICS", metrics)
    state = dispatch_fixture(
        performer_stage="implementing", lifecycle_sequence=["implementing", "reviewing"],
    )
    service = DispatchService() if accepted else DispatchHealthError()
    state["performer_services"] = {"implementing": service}
    await dispatch_node.dispatch_performer(state)
    output = generate_latest(registry).decode()
    assert ('coordinare_runs_started_total{stage="implementing"} 1.0' in output) is accepted


@pytest.mark.asyncio
async def test_default_metrics_route_and_mcp_handler() -> None:
    app = create_health_app(SimpleNamespace())
    transport = httpx.ASGITransport(app=app)
    client = DashboardClient(McpServerConfig(agent_token='a' * 32, dashboard_token='d' * 32), transport=transport)
    result = await get_metrics_handler(client)
    assert result.ok is True
    assert result.data is not None
    assert 'coordinare_' in result.data['text']
