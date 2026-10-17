"""#151: the dashboard snapshot carries system_health, built from the recorder, the config and the disk."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from coordinare.dashboard import DashboardStore
from coordinare.models.env_cache import EnvCacheState
from coordinare.services.system_health import DEGRADED, NOT_MONITORED, SYSTEM_HEALTH
from tests.unit.env_manifest_fixture import reviewed_cache
from tests.unit.test_dashboard import _make_mock_daemon, _make_mock_health, _make_mock_metrics


@pytest.fixture(autouse=True)
def _fresh_recorder() -> None:
    SYSTEM_HEALTH.reset()


def _rows(snapshot: dict) -> dict[str, dict]:
    return {row["key"]: row for row in snapshot["system_health"]["rows"]}


def _config(tmp_path: Path, *, webhooks: bool = False, endpoints: tuple[str, ...] = ()) -> MagicMock:
    config = MagicMock()
    config.poll_interval_seconds = 30
    config.state_file_path = tmp_path / "coordinare.state.json"
    config.webhooks = SimpleNamespace(enabled=webhooks)
    config.performer_endpoints = [SimpleNamespace(mode=mode) for mode in endpoints]
    return config


def test_the_snapshot_carries_one_row_per_subsystem_and_the_overall_status() -> None:
    snapshot = DashboardStore().build_snapshot(_make_mock_daemon(), _make_mock_metrics(), _make_mock_health([]))
    assert set(snapshot["system_health"]) == {"overall", "rows"}
    assert len(snapshot["system_health"]["rows"]) == 6
    # Nothing has been observed and no config is loaded, so nothing is green.
    assert snapshot["system_health"]["overall"] != "healthy"


def test_without_a_config_the_disk_is_unknown_and_the_optional_subsystems_are_not_monitored() -> None:
    rows = _rows(DashboardStore().build_snapshot(_make_mock_daemon(), _make_mock_metrics(), _make_mock_health([])))
    assert rows["disk_env_cache"]["status"] == "unknown"
    assert rows["github_webhooks"]["status"] == NOT_MONITORED
    assert rows["performer_runtime"]["status"] == NOT_MONITORED


def test_with_a_config_the_disk_is_read_from_the_state_volume(tmp_path: Path) -> None:
    daemon = _make_mock_daemon()
    daemon.state["config"] = _config(tmp_path, webhooks=True, endpoints=("ephemeral",))
    rows = _rows(DashboardStore().build_snapshot(daemon, _make_mock_metrics(), _make_mock_health([])))
    assert "% of disk free" in rows["disk_env_cache"]["summary"]
    assert rows["github_webhooks"]["status"] == "unknown"
    assert rows["performer_runtime"]["status"] == "unknown"


def test_a_symphony_whose_env_cache_failed_degrades_the_disk_and_env_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The disk reading is the host's own. Pinned here, so the row's status is the env failure's alone.
    monkeypatch.setattr("coordinare.dashboard.store._disk_free_fraction", lambda config: 0.6)
    daemon = _make_mock_daemon()
    daemon.state["config"] = _config(tmp_path)
    ready = reviewed_cache(EnvCacheState(
        symphony_name="beta", sanitised_name="beta", cache_dir=tmp_path / "beta",
        cache_dir_ready=True, last_bootstrap_succeeded=True,
    ))
    daemon.state["env_cache"] = {
        "alpha": SimpleNamespace(bootstrap_exhausted=True, last_bootstrap_succeeded=False),
        "beta": ready,
    }
    row = _rows(DashboardStore().build_snapshot(daemon, _make_mock_metrics(), _make_mock_health([])))["disk_env_cache"]
    assert row["status"] == DEGRADED
    # 266: the server row names each failing symphony; the browser scopes it to the symphony the page shows.
    assert "env cache bootstrap failed for 1 symphony" in row["reason"]
    assert row["env_failures"] == [{"symphony": "alpha", "reason": "the reason was not recorded"}]
    assert "env cache ready for 1 of 2" in row["summary"]


def test_the_snapshot_serialises_and_carries_no_credential_material(tmp_path: Path) -> None:
    daemon = _make_mock_daemon()
    daemon.state["config"] = _config(tmp_path, webhooks=True, endpoints=("ephemeral",))
    snapshot = DashboardStore().build_snapshot(daemon, _make_mock_metrics(), _make_mock_health([]))
    payload = json.dumps(snapshot["system_health"], default=str)
    assert "ghp_" not in payload
    assert "Bearer" not in payload
