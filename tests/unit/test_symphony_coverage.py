"""Coverage tests for symphony management code paths in daemon.py and dashboard.py.

These tests exercise the actual code in _conduct_single_symphony,
_handle_config_reload, and the /api/symphonies/* endpoints, which were
missing coverage after the 057 implementation.
"""
from __future__ import annotations

import asyncio
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from coordinare.config import (
    CoordinareConfiguration,
    OrchestraConfig,
    ProjectConfiguration,
    SymphonyConfig,
)
from coordinare.daemon import CoordinareDaemon
from coordinare.dashboard import DashboardStore, create_dashboard_app
from coordinare.graph.state import SymphonyRuntimeState

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _base_project_config() -> ProjectConfiguration:
    return ProjectConfiguration(
        project_name="Demo",
        github_org="acme",
        github_project_number=1,
        github_token="token",
        human_reviewers=["alice"],
    )


def _make_coordinare_config(num: int = 2) -> CoordinareConfiguration:
    global_cfg = _base_project_config()
    symphonies = [
        SymphonyConfig(name=f"sym-{i}", github_project_number=100 + i)
        for i in range(num)
    ]
    return CoordinareConfiguration(
        global_config=global_cfg,
        symphonies=symphonies,
        orchestra=OrchestraConfig(mode="shared_pool"),
    )


async def _no_sleep(_: int) -> None:
    return None


def _make_daemon(**kwargs) -> CoordinareDaemon:
    graph = MagicMock()
    graph.ainvoke = AsyncMock(return_value={})
    return CoordinareDaemon(
        graph,
        poll_interval_seconds=1,
        heartbeat_interval_seconds=1,
        max_cycles=1,
        sleep_func=_no_sleep,
        **kwargs,
    )


def _make_mock_daemon_with_symphonies(
    coordinare_cfg: CoordinareConfiguration | None = None,
) -> MagicMock:
    """Create a mock daemon with symphony state populated."""
    cfg = coordinare_cfg or _make_coordinare_config(2)
    daemon = MagicMock()
    daemon._config_reload_trigger = asyncio.Event()

    sym_configs = {s.name: s for s in cfg.symphonies}
    sym_states = {
        s.name: SymphonyRuntimeState(name=s.name, cycle_count=3, error_count=0)
        for s in cfg.symphonies
    }
    daemon.state = {
        "symphony_configs": sym_configs,
        "symphony_states": sym_states,
        "config": cfg.global_config,
        "coordinare_config": cfg,
        "config_version": 1,
        "config_mode": "multi_symphony",
    }
    return daemon


def _make_dashboard_client(daemon: MagicMock | None = None) -> TestClient:
    store = DashboardStore()
    d = daemon or _make_mock_daemon_with_symphonies()
    metrics = MagicMock()
    metrics.cycles_completed_total._value.get.return_value = 0
    metrics.build_info.labels.return_value._value.get.return_value = {
        "started_at": "2026-01-01T00:00:00+00:00"
    }
    health = MagicMock()
    health.snapshot.return_value.probes = []
    app = create_dashboard_app(store, d, metrics, health)
    return TestClient(app, base_url="http://127.0.0.1:8090")


# ===========================================================================
# Tests: _conduct_single_symphony — daemon.py lines 821-879
# ===========================================================================


class TestConductSingleSymphony:
    """Exercises _conduct_single_symphony for coverage."""

    @pytest.mark.asyncio
    async def test_success_path_increments_cycle_count(self) -> None:
        """Normal run: cycle_count increments, last_poll_at is set."""
        daemon = _make_daemon()
        cfg = _make_coordinare_config(1)
        sym_name = "sym-0"
        sym_cfg = cfg.symphonies[0]
        sym_state = SymphonyRuntimeState(name=sym_name, cycle_count=0)
        daemon._state["symphony_configs"] = {sym_name: sym_cfg}
        daemon._state["symphony_states"] = {sym_name: sym_state}
        daemon._state["config"] = cfg.global_config

        # graph.ainvoke returns minimal state
        daemon._graph.ainvoke = AsyncMock(return_value=dict(daemon._state))

        await daemon._conduct_single_symphony(sym_name, sym_cfg)

        assert sym_state.cycle_count == 1
        assert sym_state.last_poll_at is not None

    @pytest.mark.asyncio
    async def test_success_path_captures_board_snapshot(self) -> None:
        """Board snapshot from state is saved to symphony state."""
        daemon = _make_daemon()
        cfg = _make_coordinare_config(1)
        sym_name = "sym-0"
        sym_cfg = cfg.symphonies[0]
        sym_state = SymphonyRuntimeState(name=sym_name)
        daemon._state["symphony_configs"] = {sym_name: sym_cfg}
        daemon._state["symphony_states"] = {sym_name: sym_state}
        daemon._state["config"] = cfg.global_config

        snap = {"todo": 2, "in_progress": 1, "done": 5}
        result_state = dict(daemon._state)
        result_state["board_snapshot"] = snap
        daemon._graph.ainvoke = AsyncMock(return_value=result_state)

        await daemon._conduct_single_symphony(sym_name, sym_cfg)

        assert sym_state.board_snapshot == snap

    @pytest.mark.asyncio
    async def test_at_capacity_ticks_existing_sessions(self) -> None:
        """When at capacity, new dispatch is skipped but existing sessions are ticked."""
        daemon = _make_daemon()
        cfg = _make_coordinare_config(1)
        sym_name = "sym-0"
        sym_cfg = cfg.symphonies[0]

        # Set up state: 1 active session in the per-symphony state, limit = 1
        sym_state = SymphonyRuntimeState(name=sym_name, active_sessions={"sess-1": {}})
        config = _base_project_config()
        config.max_concurrent_cards = 1

        daemon._state["symphony_configs"] = {sym_name: sym_cfg}
        daemon._state["symphony_states"] = {sym_name: sym_state}
        daemon._state["config"] = config

        result_state = dict(daemon._state)
        result_state["active_sessions"] = {"sess-1": {}}
        daemon._graph.ainvoke = AsyncMock(return_value=result_state)

        await daemon._conduct_single_symphony(sym_name, sym_cfg)

        # graph IS called — existing in-flight sessions must still be ticked
        daemon._graph.ainvoke.assert_called_once()
        # cycle count increments even at capacity
        assert sym_state.cycle_count == 1

    @pytest.mark.asyncio
    async def test_exception_increments_error_count(self) -> None:
        """Exception inside cycle increments error_count and does NOT re-raise."""
        daemon = _make_daemon()
        cfg = _make_coordinare_config(1)
        sym_name = "sym-0"
        sym_cfg = cfg.symphonies[0]
        sym_state = SymphonyRuntimeState(name=sym_name, error_count=0)
        daemon._state["symphony_configs"] = {sym_name: sym_cfg}
        daemon._state["symphony_states"] = {sym_name: sym_state}
        daemon._state["config"] = cfg.global_config

        daemon._graph.ainvoke = AsyncMock(side_effect=RuntimeError("boom"))

        # Should not raise
        await daemon._conduct_single_symphony(sym_name, sym_cfg)

        assert sym_state.error_count == 1
        assert "boom" in (sym_state.last_error or "")

    @pytest.mark.asyncio
    async def test_cancelled_error_is_reraised(self) -> None:
        """CancelledError must propagate out of _conduct_single_symphony."""
        daemon = _make_daemon()
        cfg = _make_coordinare_config(1)
        sym_name = "sym-0"
        sym_cfg = cfg.symphonies[0]
        daemon._state["symphony_configs"] = {sym_name: sym_cfg}
        daemon._state["symphony_states"] = {}
        daemon._state["config"] = cfg.global_config

        daemon._graph.ainvoke = AsyncMock(side_effect=asyncio.CancelledError())

        with pytest.raises(asyncio.CancelledError):
            await daemon._conduct_single_symphony(sym_name, sym_cfg)

    @pytest.mark.asyncio
    async def test_current_symphony_cleared_in_finally(self) -> None:
        """current_symphony is cleared in the finally block even on error."""
        daemon = _make_daemon()
        cfg = _make_coordinare_config(1)
        sym_name = "sym-0"
        sym_cfg = cfg.symphonies[0]
        daemon._state["symphony_configs"] = {sym_name: sym_cfg}
        daemon._state["symphony_states"] = {}
        daemon._state["config"] = cfg.global_config

        daemon._graph.ainvoke = AsyncMock(side_effect=RuntimeError("fail"))

        await daemon._conduct_single_symphony(sym_name, sym_cfg)

        assert daemon._state.get("current_symphony") is None

    @pytest.mark.asyncio
    async def test_no_symphony_state_success_path(self) -> None:
        """Works correctly when symphony_states dict does not have the name."""
        daemon = _make_daemon()
        cfg = _make_coordinare_config(1)
        sym_name = "sym-0"
        sym_cfg = cfg.symphonies[0]
        # No sym_state for this symphony
        daemon._state["symphony_configs"] = {sym_name: sym_cfg}
        daemon._state["symphony_states"] = {}
        daemon._state["config"] = cfg.global_config

        daemon._graph.ainvoke = AsyncMock(return_value=dict(daemon._state))

        # Should not raise
        await daemon._conduct_single_symphony(sym_name, sym_cfg)


# ===========================================================================
# Tests: _handle_config_reload — daemon.py lines 881-930
# ===========================================================================


class TestHandleConfigReload:
    """Exercises _handle_config_reload for coverage."""

    @pytest.mark.asyncio
    async def test_no_config_path_returns_early(self) -> None:
        """When config_path is None, method returns without error."""
        daemon = _make_daemon()
        daemon._state["config_path"] = None

        # Should not raise
        await daemon._handle_config_reload()

    @pytest.mark.asyncio
    async def test_adds_new_symphony_to_state(self) -> None:
        """Newly added symphony gets a SymphonyRuntimeState."""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False
        ) as f:
            f.write(
                "github_org: acme\n"
                "github_token: token\n"
                "human_reviewers: [alice]\n"
                "symphonies:\n"
                "  - name: sym-0\n"
                "    github_project_number: 100\n"
                "  - name: sym-new\n"
                "    github_project_number: 200\n"
                "orchestra:\n"
                "  mode: shared_pool\n"
            )
            config_file = Path(f.name)

        try:
            daemon = _make_daemon()
            # Pre-existing: only sym-0
            daemon._state["config_path"] = config_file
            daemon._state["symphony_configs"] = {
                "sym-0": SymphonyConfig(name="sym-0", github_project_number=100)
            }
            daemon._state["symphony_states"] = {
                "sym-0": SymphonyRuntimeState(name="sym-0")
            }

            await daemon._handle_config_reload()

            sym_states = daemon._state.get("symphony_states") or {}
            assert "sym-new" in sym_states
            assert "sym-0" in sym_states
        finally:
            config_file.unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_removes_deleted_symphony_from_state(self) -> None:
        """Symphonies no longer in config are removed from state."""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False
        ) as f:
            f.write(
                "github_org: acme\n"
                "github_token: token\n"
                "human_reviewers: [alice]\n"
                "symphonies:\n"
                "  - name: sym-0\n"
                "    github_project_number: 100\n"
                "orchestra:\n"
                "  mode: shared_pool\n"
            )
            config_file = Path(f.name)

        try:
            daemon = _make_daemon()
            daemon._state["config_path"] = config_file
            daemon._state["symphony_configs"] = {
                "sym-0": SymphonyConfig(name="sym-0", github_project_number=100),
                "sym-old": SymphonyConfig(name="sym-old", github_project_number=999),
            }
            daemon._state["symphony_states"] = {
                "sym-0": SymphonyRuntimeState(name="sym-0"),
                "sym-old": SymphonyRuntimeState(name="sym-old"),
            }

            await daemon._handle_config_reload()

            sym_states = daemon._state.get("symphony_states") or {}
            assert "sym-old" not in sym_states
            assert "sym-0" in sym_states
        finally:
            config_file.unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_increments_config_version(self) -> None:
        """config_version increments on each successful reload."""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False
        ) as f:
            f.write(
                "github_org: acme\n"
                "github_token: token\n"
                "human_reviewers: [alice]\n"
                "symphonies:\n"
                "  - name: sym-0\n"
                "    github_project_number: 100\n"
                "orchestra:\n"
                "  mode: shared_pool\n"
            )
            config_file = Path(f.name)

        try:
            daemon = _make_daemon()
            daemon._state["config_path"] = config_file
            daemon._state["symphony_configs"] = {}
            daemon._state["symphony_states"] = {}
            daemon._state["config_version"] = 5

            await daemon._handle_config_reload()

            assert daemon._state.get("config_version") == 6
        finally:
            config_file.unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_exception_does_not_propagate(self) -> None:
        """Exception during reload is caught and logged, does not propagate."""
        daemon = _make_daemon()
        daemon._state["config_path"] = Path("/nonexistent/path/config.yaml")
        daemon._state["symphony_configs"] = {}
        daemon._state["symphony_states"] = {}

        # Should not raise
        await daemon._handle_config_reload()

    @pytest.mark.asyncio
    async def test_handles_legacy_config_format(self) -> None:
        """Legacy flat config is auto-wrapped as single 'default' symphony."""
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".yaml", delete=False
        ) as f:
            # Legacy format: flat keys, no 'symphonies' key
            f.write(
                "project_name: Demo\n"
                "github_org: acme\n"
                "github_project_number: 99\n"
                "github_token: token\n"
                "human_reviewers: [alice]\n"
            )
            config_file = Path(f.name)

        try:
            daemon = _make_daemon()
            daemon._state["config_path"] = config_file
            daemon._state["symphony_configs"] = {}
            daemon._state["symphony_states"] = {}
            daemon._state["config_version"] = 0

            await daemon._handle_config_reload()

            sym_configs = daemon._state.get("symphony_configs") or {}
            assert "default" in sym_configs
        finally:
            config_file.unlink(missing_ok=True)


# ===========================================================================
# Tests: Symphony API endpoints — dashboard.py lines 2194-2325
# ===========================================================================


class TestGetSymphoniesApiEndpoint:
    """Tests for GET /api/symphonies."""

    def test_returns_200_with_symphonies_list(self) -> None:
        client = _make_dashboard_client()
        resp = client.get("/api/symphonies")
        assert resp.status_code == 200
        data = resp.json()
        assert "symphonies" in data
        assert len(data["symphonies"]) == 2

    def test_returns_config_version(self) -> None:
        client = _make_dashboard_client()
        resp = client.get("/api/symphonies")
        data = resp.json()
        assert "config_version" in data
        assert data["config_version"] == 1

    def test_symphony_entries_have_expected_fields(self) -> None:
        client = _make_dashboard_client()
        resp = client.get("/api/symphonies")
        data = resp.json()
        for sym in data["symphonies"]:
            assert "name" in sym
            assert "cycle_count" in sym
            assert "error_count" in sym

    def test_empty_symphonies_state(self) -> None:
        daemon = MagicMock()
        daemon.state = {
            "symphony_configs": {},
            "symphony_states": {},
            "config": _base_project_config(),
            "coordinare_config": None,
            "config_version": 0,
        }
        client = _make_dashboard_client(daemon)
        resp = client.get("/api/symphonies")
        assert resp.status_code == 200
        assert resp.json()["symphonies"] == []

    def test_includes_last_poll_at_isoformat(self) -> None:
        """last_poll_at is serialized as ISO string when set."""
        cfg = _make_coordinare_config(1)
        daemon = MagicMock()
        ts = datetime(2026, 4, 30, 12, 0, 0, tzinfo=UTC)
        sym_state = SymphonyRuntimeState(
            name="sym-0", cycle_count=1, last_poll_at=ts
        )
        daemon.state = {
            "symphony_configs": {"sym-0": cfg.symphonies[0]},
            "symphony_states": {"sym-0": sym_state},
            "config": cfg.global_config,
            "coordinare_config": cfg,
            "config_version": 2,
        }
        client = _make_dashboard_client(daemon)
        resp = client.get("/api/symphonies")
        data = resp.json()
        assert data["symphonies"][0]["last_poll_at"] is not None
        assert "2026" in data["symphonies"][0]["last_poll_at"]


class TestGetSymphonyDetailApiEndpoint:
    """Tests for GET /api/symphonies/{name}."""

    def test_returns_200_for_known_symphony(self) -> None:
        client = _make_dashboard_client()
        resp = client.get("/api/symphonies/sym-0")
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "sym-0"

    def test_returns_404_for_unknown_symphony(self) -> None:
        client = _make_dashboard_client()
        resp = client.get("/api/symphonies/nonexistent")
        assert resp.status_code == 404
        assert "not found" in resp.json()["error"].lower()

    def test_detail_includes_state_fields(self) -> None:
        client = _make_dashboard_client()
        resp = client.get("/api/symphonies/sym-0")
        data = resp.json()
        assert "github_project_number" in data
        # state is present (we have a SymphonyRuntimeState for sym-0)
        assert "state" in data

    def test_state_is_none_when_no_runtime_state(self) -> None:
        """When symphony_states has no entry for the name, state is null."""
        cfg = _make_coordinare_config(1)
        daemon = MagicMock()
        daemon.state = {
            "symphony_configs": {"sym-0": cfg.symphonies[0]},
            "symphony_states": {},  # no runtime state
            "config": cfg.global_config,
            "coordinare_config": cfg,
            "config_version": 1,
        }
        client = _make_dashboard_client(daemon)
        resp = client.get("/api/symphonies/sym-0")
        assert resp.status_code == 200
        assert resp.json()["state"] is None


class TestValidateSymphonyApiEndpoint:
    """Tests for POST /api/symphonies/{name}/validate."""

    def test_returns_valid_true_for_known_symphony(self) -> None:
        client = _make_dashboard_client()
        resp = client.post("/api/symphonies/sym-0/validate")
        assert resp.status_code == 200
        data = resp.json()
        assert data["valid"] is True
        assert data["symphony"] == "sym-0"

    def test_returns_404_for_unknown_symphony(self) -> None:
        client = _make_dashboard_client()
        resp = client.post("/api/symphonies/does-not-exist/validate")
        assert resp.status_code == 404

    def test_returns_500_when_no_coordinare_config(self) -> None:
        cfg = _make_coordinare_config(1)
        daemon = MagicMock()
        daemon.state = {
            "symphony_configs": {"sym-0": cfg.symphonies[0]},
            "symphony_states": {},
            "config": cfg.global_config,
            "coordinare_config": None,  # missing coordinare config
            "config_version": 1,
        }
        client = _make_dashboard_client(daemon)
        resp = client.post("/api/symphonies/sym-0/validate")
        assert resp.status_code == 500

    def test_effective_config_fields_returned(self) -> None:
        client = _make_dashboard_client()
        resp = client.post("/api/symphonies/sym-0/validate")
        data = resp.json()
        assert "effective_config" in data
        eff = data["effective_config"]
        assert "github_org" in eff
        assert "github_project_number" in eff


class TestPutSymphonyApiEndpoint:
    """Tests for PUT /api/symphonies/{name}."""

    def test_returns_200_and_updates_overrides(self) -> None:
        daemon = _make_mock_daemon_with_symphonies()
        daemon._cycle_active = False
        client = _make_dashboard_client(daemon)
        resp = client.put("/api/symphonies/sym-0", json={"overrides": {"poll_interval_seconds": 90}})
        assert resp.status_code == 200
        data = resp.json()
        assert data["name"] == "sym-0"
        assert data["overrides"] == {"poll_interval_seconds": 90}

    def test_increments_config_version(self) -> None:
        daemon = _make_mock_daemon_with_symphonies()
        daemon._cycle_active = False
        version_before = daemon.state["config_version"]
        client = _make_dashboard_client(daemon)
        client.put("/api/symphonies/sym-0", json={"overrides": {}})
        assert daemon.state["config_version"] == version_before + 1

    def test_sets_reload_trigger(self) -> None:
        daemon = _make_mock_daemon_with_symphonies()
        daemon._cycle_active = False
        daemon._config_reload_trigger.clear()
        client = _make_dashboard_client(daemon)
        client.put("/api/symphonies/sym-0", json={"overrides": {}})
        assert daemon._config_reload_trigger.is_set()

    def test_returns_404_for_unknown_symphony(self) -> None:
        daemon = _make_mock_daemon_with_symphonies()
        daemon._cycle_active = False
        client = _make_dashboard_client(daemon)
        resp = client.put("/api/symphonies/nonexistent", json={"overrides": {}})
        assert resp.status_code == 404

    def test_returns_409_when_cycle_active(self) -> None:
        daemon = _make_mock_daemon_with_symphonies()
        daemon._cycle_active = True
        client = _make_dashboard_client(daemon)
        resp = client.put("/api/symphonies/sym-0", json={"overrides": {}})
        assert resp.status_code == 409

    def test_returns_400_for_invalid_json(self) -> None:
        daemon = _make_mock_daemon_with_symphonies()
        daemon._cycle_active = False
        client = _make_dashboard_client(daemon)
        resp = client.put(
            "/api/symphonies/sym-0",
            content=b"not-json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status_code == 400


class TestDeleteSymphonyApiEndpoint:
    """Tests for DELETE /api/symphonies/{name}."""

    def test_returns_200_and_deletes(self) -> None:
        daemon = _make_mock_daemon_with_symphonies()
        daemon._cycle_active = False
        client = _make_dashboard_client(daemon)
        resp = client.delete("/api/symphonies/sym-0")
        assert resp.status_code == 200
        data = resp.json()
        assert data["deleted"] == "sym-0"
        assert "sym-0" not in daemon.state["symphony_configs"]

    def test_increments_config_version(self) -> None:
        daemon = _make_mock_daemon_with_symphonies()
        daemon._cycle_active = False
        version_before = daemon.state["config_version"]
        client = _make_dashboard_client(daemon)
        client.delete("/api/symphonies/sym-0")
        assert daemon.state["config_version"] == version_before + 1

    def test_sets_reload_trigger(self) -> None:
        daemon = _make_mock_daemon_with_symphonies()
        daemon._cycle_active = False
        daemon._config_reload_trigger.clear()
        client = _make_dashboard_client(daemon)
        client.delete("/api/symphonies/sym-0")
        assert daemon._config_reload_trigger.is_set()

    def test_returns_404_for_unknown_symphony(self) -> None:
        daemon = _make_mock_daemon_with_symphonies()
        daemon._cycle_active = False
        client = _make_dashboard_client(daemon)
        resp = client.delete("/api/symphonies/nonexistent")
        assert resp.status_code == 404

    def test_returns_409_when_cycle_active(self) -> None:
        daemon = _make_mock_daemon_with_symphonies()
        daemon._cycle_active = True
        client = _make_dashboard_client(daemon)
        resp = client.delete("/api/symphonies/sym-0")
        assert resp.status_code == 409

    def test_returns_409_for_last_symphony(self) -> None:
        daemon = _make_mock_daemon_with_symphonies(_make_coordinare_config(1))
        daemon._cycle_active = False
        client = _make_dashboard_client(daemon)
        resp = client.delete("/api/symphonies/sym-0")
        assert resp.status_code == 409

    def test_removes_symphony_state(self) -> None:
        daemon = _make_mock_daemon_with_symphonies()
        daemon._cycle_active = False
        client = _make_dashboard_client(daemon)
        client.delete("/api/symphonies/sym-0")
        assert "sym-0" not in daemon.state["symphony_states"]


class TestGetEffectiveConfigApiEndpoint:
    """Tests for GET /api/config/effective."""

    def test_returns_200_with_config(self) -> None:
        client = _make_dashboard_client()
        resp = client.get("/api/config/effective")
        assert resp.status_code == 200
        data = resp.json()
        assert "github_org" in data
        assert "github_project_number" in data

    def test_returns_mode_multi_symphony(self) -> None:
        client = _make_dashboard_client()
        resp = client.get("/api/config/effective")
        data = resp.json()
        assert data["mode"] == "multi_symphony"

    def test_returns_500_when_no_config(self) -> None:
        daemon = MagicMock()
        daemon.state = {"config": None, "coordinare_config": None}
        client = _make_dashboard_client(daemon)
        resp = client.get("/api/config/effective")
        assert resp.status_code == 500

    def test_returns_legacy_mode_without_coordinare_config(self) -> None:
        daemon = MagicMock()
        daemon.state = {
            "config": _base_project_config(),
            "coordinare_config": None,  # no multi-symphony wrapper
        }
        client = _make_dashboard_client(daemon)
        resp = client.get("/api/config/effective")
        data = resp.json()
        assert data["mode"] == "legacy"


class TestPostConfigReloadApiEndpoint:
    """Tests for POST /api/config/reload."""

    def test_returns_202_and_triggers_reload(self) -> None:
        daemon = _make_mock_daemon_with_symphonies()
        # Ensure the event starts unset
        daemon._config_reload_trigger.clear()
        client = _make_dashboard_client(daemon)
        resp = client.post("/api/config/reload")
        assert resp.status_code == 202
        data = resp.json()
        assert data["status"] == "reload_triggered"
        # Event should have been set
        assert daemon._config_reload_trigger.is_set()

    def test_returns_501_when_no_trigger_attribute(self) -> None:
        daemon = MagicMock(spec=[])  # no attributes at all
        daemon.state = {
            "symphony_configs": {},
            "symphony_states": {},
            "config": _base_project_config(),
            "coordinare_config": None,
            "config_version": 0,
        }
        client = _make_dashboard_client(daemon)
        resp = client.post("/api/config/reload")
        assert resp.status_code == 501
