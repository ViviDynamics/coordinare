from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from jsonschema import validate

from coordinare.daemon import CoordinareDaemon
from coordinare.health import create_health_app
from coordinare.metrics import CoordinareMetrics
from coordinare.resilience import CircuitBreaker, CircuitState
from coordinare.state_store import StateStore, WorkflowSnapshot

_SCHEMA_PATH = Path(__file__).resolve().parents[2] / "specs" / "005-resilience" / "contracts" / "health-response.schema.json"


class _Graph:
    async def ainvoke(self, state):
        return state


def _make_snapshot(**overrides) -> WorkflowSnapshot:
    defaults = {
        "snapshot_at": datetime.now(UTC),
        "phase": "idle",
    }
    defaults.update(overrides)
    return WorkflowSnapshot(**defaults)


def _make_circuit_breakers() -> dict[str, CircuitBreaker]:
    """Create a default set of closed circuit breakers for testing."""
    services = ["github", "slack", "smtp", "anthropic", "agent"]
    return {
        name: CircuitBreaker(
            service_name=name,
            failure_threshold=3,
            recovery_window=120.0,
            observation_window=300.0,
        )
        for name in services
    }


# --- T024: Contract test — phase and snapshot_at present in response ---


def test_health_response_contains_phase_and_snapshot_at() -> None:
    """GET /health response contains phase (string) and snapshot_at (ISO 8601 or null)."""
    daemon = CoordinareDaemon(_Graph(), max_cycles=1)
    client = TestClient(create_health_app(daemon))
    response = client.get("/health")

    data = response.json()
    assert "phase" in data
    assert "snapshot_at" in data
    # When no state_store, phase defaults to "idle", snapshot_at is None
    assert data["phase"] == "idle"
    assert data["snapshot_at"] is None


def test_health_response_phase_is_valid_workflow_phase(tmp_path: Path) -> None:
    """phase field must be a valid WorkflowPhase string or null."""
    valid_phases = {
        "idle", "dispatching", "monitoring_agent", "monitoring_pr",
        "merging", "relay_feedback", "blocked", "recovery", None,
    }
    metrics = CoordinareMetrics()
    store = StateStore(path=tmp_path / "state.json", metrics=metrics)
    store.last_snapshot = _make_snapshot(phase="monitoring_agent")

    daemon = CoordinareDaemon(_Graph(), max_cycles=1, state_store=store)
    client = TestClient(create_health_app(daemon))
    response = client.get("/health")

    data = response.json()
    assert data["phase"] in valid_phases


def test_health_response_snapshot_at_is_iso8601(tmp_path: Path) -> None:
    """snapshot_at field must be a valid ISO 8601 string when present."""
    metrics = CoordinareMetrics()
    store = StateStore(path=tmp_path / "state.json", metrics=metrics)
    now = datetime.now(UTC)
    store.last_snapshot = _make_snapshot(phase="idle", snapshot_at=now)

    daemon = CoordinareDaemon(_Graph(), max_cycles=1, state_store=store)
    client = TestClient(create_health_app(daemon))
    response = client.get("/health")

    data = response.json()
    assert data["snapshot_at"] is not None
    # Should be parseable as ISO 8601
    parsed = datetime.fromisoformat(data["snapshot_at"])
    assert parsed.year == now.year


# --- T025: Contract test — phase and snapshot_at reflect last saved snapshot ---


@pytest.mark.asyncio
async def test_health_reflects_last_snapshot_before_first_poll(tmp_path: Path) -> None:
    """phase and snapshot_at reflect the last saved snapshot immediately after daemon start."""
    metrics = CoordinareMetrics()
    store = StateStore(path=tmp_path / "state.json", metrics=metrics)
    snapshot = _make_snapshot(
        phase="monitoring_agent",
        active_card_id="PVT_HEALTH",
        active_card_title="Health Card",
        active_card_column="In Progress",
    )
    await store.save(snapshot)

    daemon = CoordinareDaemon(_Graph(), max_cycles=1, state_store=store)
    client = TestClient(create_health_app(daemon))

    response = client.get("/health")
    data = response.json()

    assert data["phase"] == "monitoring_agent"
    assert data["snapshot_at"] is not None


def test_health_default_phase_when_no_snapshot() -> None:
    """With no snapshot, phase defaults to 'idle' and snapshot_at is null."""
    daemon = CoordinareDaemon(_Graph(), max_cycles=1)
    client = TestClient(create_health_app(daemon))

    response = client.get("/health")
    data = response.json()

    assert data["phase"] == "idle"
    assert data["snapshot_at"] is None


def test_health_response_required_fields_present(tmp_path: Path) -> None:
    """Response contains all required fields from the health-response v3 schema."""
    metrics = CoordinareMetrics()
    store = StateStore(path=tmp_path / "state.json", metrics=metrics)
    store.last_snapshot = _make_snapshot(phase="idle")
    cbs = _make_circuit_breakers()

    daemon = CoordinareDaemon(_Graph(), max_cycles=1, state_store=store)
    client = TestClient(create_health_app(daemon, circuit_breakers=cbs))
    response = client.get("/health")

    data = response.json()
    # Required fields per v3 schema
    assert "status" in data
    assert "phase" in data
    assert "snapshot_at" in data
    assert "circuit_breakers" in data
    assert "uptime_seconds" in data
    # Circuit breakers structure
    cb_data = data["circuit_breakers"]
    for svc in ("github", "slack", "smtp", "anthropic", "agent"):
        assert svc in cb_data
        assert cb_data[svc]["state"] == "closed"
        assert cb_data[svc]["opened_at"] is None
        assert cb_data[svc]["failure_count"] == 0


# --- Coverage: status derivation, ready, metrics ---


def test_health_status_ok_when_all_circuits_closed() -> None:
    """status is 'ok' when daemon running and all circuits closed."""
    daemon = CoordinareDaemon(_Graph(), max_cycles=1)
    daemon._running = True
    cbs = _make_circuit_breakers()
    client = TestClient(create_health_app(daemon, circuit_breakers=cbs))
    response = client.get("/health")

    data = response.json()
    assert data["status"] == "ok"


def test_health_status_degraded_when_core_circuit_open() -> None:
    """status is 'degraded' when a core circuit (github) is open."""
    daemon = CoordinareDaemon(_Graph(), max_cycles=1)
    daemon._running = True
    cbs = _make_circuit_breakers()
    # Force github circuit open
    for _ in range(3):
        cbs["github"].record_failure()
    assert cbs["github"].state == CircuitState.OPEN

    client = TestClient(create_health_app(daemon, circuit_breakers=cbs))
    response = client.get("/health")

    data = response.json()
    assert data["status"] == "degraded"
    assert data["circuit_breakers"]["github"]["state"] == "open"


def test_health_status_ok_when_only_notification_circuit_open() -> None:
    """status stays 'ok' when only notification circuits (slack/smtp) are open."""
    daemon = CoordinareDaemon(_Graph(), max_cycles=1)
    daemon._running = True
    cbs = _make_circuit_breakers()
    # Force slack circuit open
    for _ in range(3):
        cbs["slack"].record_failure()
    assert cbs["slack"].state == CircuitState.OPEN

    client = TestClient(create_health_app(daemon, circuit_breakers=cbs))
    response = client.get("/health")

    data = response.json()
    assert data["status"] == "ok"


def test_health_status_unhealthy_when_not_running() -> None:
    """status is 'unhealthy' when daemon is not running."""
    daemon = CoordinareDaemon(_Graph(), max_cycles=1)
    # daemon._running defaults to False
    client = TestClient(create_health_app(daemon, circuit_breakers=_make_circuit_breakers()))
    response = client.get("/health")

    assert response.status_code == 503
    assert response.json()["status"] == "unhealthy"


def test_ready_endpoint_returns_503_when_not_running() -> None:
    """Required subsystem degraded → 503."""
    from coordinare.observability import HealthRegistry, HealthStatus

    registry = HealthRegistry()
    registry.register("github", required=True)
    registry.update("github", HealthStatus.degraded, details="test")

    daemon = CoordinareDaemon(_Graph(), max_cycles=1)
    client = TestClient(create_health_app(daemon, health_registry=registry))
    response = client.get("/ready")

    assert response.status_code == 503
    assert response.json()["status"] == "degraded"


def test_metrics_endpoint_returns_text() -> None:
    daemon = CoordinareDaemon(_Graph(), max_cycles=1)
    client = TestClient(create_health_app(daemon))
    response = client.get("/metrics")

    assert response.status_code == 200
    assert "text/plain" in response.headers["content-type"]


def test_ready_endpoint_returns_200_when_running() -> None:
    """All required subsystems healthy → 200 with status='ready'."""
    from coordinare.observability import HealthRegistry, HealthStatus

    registry = HealthRegistry()
    registry.register("github", required=True)
    registry.update("github", HealthStatus.healthy)

    daemon = CoordinareDaemon(_Graph(), max_cycles=1)
    client = TestClient(create_health_app(daemon, health_registry=registry))
    response = client.get("/ready")

    assert response.status_code == 200
    assert response.json()["status"] == "ready"


# --- T026a: Additional status derivation tests ---


def test_health_status_degraded_when_anthropic_circuit_open() -> None:
    """status is 'degraded' when anthropic circuit is open."""
    daemon = CoordinareDaemon(_Graph(), max_cycles=1)
    daemon._running = True
    cbs = _make_circuit_breakers()
    for _ in range(3):
        cbs["anthropic"].record_failure()
    assert cbs["anthropic"].state == CircuitState.OPEN

    client = TestClient(create_health_app(daemon, circuit_breakers=cbs))
    data = client.get("/health").json()

    assert data["status"] == "degraded"
    assert data["circuit_breakers"]["anthropic"]["state"] == "open"


def test_health_status_degraded_when_agent_circuit_open() -> None:
    """status is 'degraded' when agent circuit is open."""
    daemon = CoordinareDaemon(_Graph(), max_cycles=1)
    daemon._running = True
    cbs = _make_circuit_breakers()
    for _ in range(3):
        cbs["agent"].record_failure()
    assert cbs["agent"].state == CircuitState.OPEN

    client = TestClient(create_health_app(daemon, circuit_breakers=cbs))
    data = client.get("/health").json()

    assert data["status"] == "degraded"
    assert data["circuit_breakers"]["agent"]["state"] == "open"


def test_health_status_degraded_when_all_circuits_open() -> None:
    """status is 'degraded' (not 'unhealthy') when all circuits open but daemon running."""
    daemon = CoordinareDaemon(_Graph(), max_cycles=1)
    daemon._running = True
    cbs = _make_circuit_breakers()
    for name in cbs:
        for _ in range(3):
            cbs[name].record_failure()
        assert cbs[name].state == CircuitState.OPEN

    client = TestClient(create_health_app(daemon, circuit_breakers=cbs))
    data = client.get("/health").json()

    assert data["status"] == "degraded"


def test_health_status_ok_when_only_smtp_circuit_open() -> None:
    """status stays 'ok' when only SMTP circuit is open (non-core)."""
    daemon = CoordinareDaemon(_Graph(), max_cycles=1)
    daemon._running = True
    cbs = _make_circuit_breakers()
    for _ in range(3):
        cbs["smtp"].record_failure()
    assert cbs["smtp"].state == CircuitState.OPEN

    client = TestClient(create_health_app(daemon, circuit_breakers=cbs))
    data = client.get("/health").json()

    assert data["status"] == "ok"


# --- T029: Contract test — JSON Schema validation ---


def _load_schema() -> dict:
    with open(_SCHEMA_PATH) as f:
        return json.load(f)


def test_health_ok_response_validates_against_schema(tmp_path: Path) -> None:
    """Health response with all circuits closed validates against health-response.schema.json."""
    metrics = CoordinareMetrics()
    store = StateStore(path=tmp_path / "state.json", metrics=metrics)
    store.last_snapshot = _make_snapshot(phase="idle")

    daemon = CoordinareDaemon(_Graph(), max_cycles=1, state_store=store)
    daemon._running = True
    cbs = _make_circuit_breakers()
    client = TestClient(create_health_app(daemon, circuit_breakers=cbs))
    data = client.get("/health").json()

    schema = _load_schema()
    validate(instance=data, schema=schema)


def test_health_degraded_response_validates_against_schema(tmp_path: Path) -> None:
    """Health response with core circuit open validates against health-response.schema.json."""
    metrics = CoordinareMetrics()
    store = StateStore(path=tmp_path / "state.json", metrics=metrics)
    store.last_snapshot = _make_snapshot(phase="monitoring_agent")

    daemon = CoordinareDaemon(_Graph(), max_cycles=1, state_store=store)
    daemon._running = True
    cbs = _make_circuit_breakers()
    for _ in range(3):
        cbs["github"].record_failure()

    client = TestClient(create_health_app(daemon, circuit_breakers=cbs))
    data = client.get("/health").json()

    schema = _load_schema()
    validate(instance=data, schema=schema)
    assert data["status"] == "degraded"
