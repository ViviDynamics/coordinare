"""Spec 219, re-review: case-variant overlap, the candidate global write, mount wiring.

Three findings from the re-review. A root spelled with other case than a performer volume is
the same directory on a case-insensitive filesystem, so config load must refuse it. The
dashboard global save must validate the whole candidate config before writing, so an overlap
is refused with a 422 and nothing is written. And the env-cache mount list must come from the
loaded config, with the same clear refusal everywhere it is missing, and with ``~`` expanded
the same way the validator expands it.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pydantic
import pytest
import yaml
from fastapi.testclient import TestClient

from coordinare.config import CoordinareConfiguration, ProjectConfiguration
from coordinare.config_validation import wrap_legacy_config
from coordinare.daemon import CoordinareDaemon
from coordinare.dashboard import DashboardStore, create_dashboard_app
from coordinare.models.env_cache import EnvCacheState
from coordinare.models.performer_endpoint import PerformerEndpointConfig, VolumeMount
from coordinare.services.mounted_file import performer_mount_hosts
from coordinare.services.run_transcript import _case_insensitive

_BASE: dict[str, object] = {
    "project_name": "Demo",
    "github_org": "acme",
    "github_project_number": 1,
    "github_token": "tok",
    "human_reviewers": ["alice"],
}


def _ephemeral(volume: Path) -> PerformerEndpointConfig:
    return PerformerEndpointConfig(
        id="p1",
        mode="ephemeral",
        image="performer:full",
        roles=["implementer"],
        volumes=[
            VolumeMount(
                host_path=volume,
                container_path=PurePosixPath("/work"),
                mode="rw",
            ),
        ],
    )


def _raw(env_root: Path, volume: Path) -> dict[str, object]:
    """A config file as the dashboard reads it: one endpoint with one volume."""
    return {
        "project_name": "Demo",
        "github_org": "acme",
        "github_project_number": 1,
        "github_token": "tok",
        "human_reviewers": ["alice"],
        "env_cache_root": str(env_root),
        "performer_endpoints": [
            {
                "id": "p1",
                "mode": "ephemeral",
                "image": "performer:full",
                "roles": ["implementer"],
                "volumes": [
                    {"host_path": str(volume), "container_path": "/work", "mode": "rw"},
                ],
            },
        ],
    }


# ---------------------------------------------------------------------------
# BLOCK 1: a root spelled with other case than a volume, on a case-folding filesystem.
# ---------------------------------------------------------------------------


def test_case_variant_root_is_refused_where_the_filesystem_folds_case(tmp_path: Path) -> None:
    """The branch is chosen by a runtime probe of this filesystem, not by a skip marker.

    Case-folding filesystem: ``WS/caches`` is ``ws/caches``, so the load must refuse it.
    Case-sensitive filesystem: they are different directories, so the load must accept it.
    """
    ws = tmp_path / "ws"
    ws.mkdir()
    spelled = tmp_path / ws.name.swapcase() / "caches"

    if _case_insensitive(tmp_path):
        with pytest.raises(pydantic.ValidationError, match="overlaps"):
            ProjectConfiguration(
                **_BASE, env_cache_root=spelled, performer_endpoints=[_ephemeral(ws)],
            )
    else:
        cfg = ProjectConfiguration(
            **_BASE,
            env_cache_root=spelled,
            performer_endpoints=[_ephemeral(ws)],
        )
        assert cfg.env_cache_root == spelled


def test_case_variant_log_dir_is_refused_where_the_filesystem_folds_case(tmp_path: Path) -> None:
    log_dir = tmp_path / "perf-log"
    log_dir.mkdir()
    spelled = tmp_path / log_dir.name.swapcase()

    if _case_insensitive(tmp_path):
        with pytest.raises(pydantic.ValidationError, match="performer_log_dir"):
            ProjectConfiguration(**_BASE, env_cache_root=spelled, performer_log_dir=log_dir)
    else:
        cfg = ProjectConfiguration(**_BASE, env_cache_root=spelled, performer_log_dir=log_dir)
        assert cfg.performer_log_dir == log_dir


# ---------------------------------------------------------------------------
# BLOCK 2: the dashboard global save validates the whole candidate before writing.
# ---------------------------------------------------------------------------


def _http_client(config_path: Path) -> TestClient:
    raw = yaml.safe_load(config_path.read_text())
    raw["github_token"] = "ghp_fixturetoken"
    config_path.write_text(yaml.safe_dump(raw, sort_keys=False))
    cfg = CoordinareConfiguration(**wrap_legacy_config(raw))
    daemon = MagicMock()
    daemon.state = {"coordinare_config": cfg, "config_version": 7}
    daemon.running = True
    app = create_dashboard_app(
        DashboardStore(),
        daemon,
        MagicMock(),
        MagicMock(),
        config_path=config_path,
    )
    return TestClient(app, base_url="http://127.0.0.1:8090")


def test_global_save_that_overlaps_a_volume_is_refused_and_writes_nothing(tmp_path: Path) -> None:
    volume = tmp_path / "junie"
    volume.mkdir()
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(_raw(tmp_path / "env-caches", volume)))
    client = _http_client(config_path)
    before = config_path.read_bytes()
    etag = client.get("/api/config/global").headers["etag"]

    # The proposed root lies inside the performer volume, so the saved file would not load.
    resp = client.put(
        "/api/config/global",
        json={"env_cache_root": str(volume / "caches"), "expected_hash": etag},
    )

    assert resp.status_code == 422
    assert "overlaps" in resp.json()["error"]
    assert config_path.read_bytes() == before, "an invalid config was written to disk"


def test_global_save_that_does_not_overlap_still_saves(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump(_raw(tmp_path / "env-caches", ws / "junie")))
    client = _http_client(config_path)
    etag = client.get("/api/config/global").headers["etag"]

    resp = client.put(
        "/api/config/global",
        json={"env_cache_root": str(tmp_path / "other-caches"), "expected_hash": etag},
    )

    assert resp.status_code == 200
    assert "other-caches" in config_path.read_text()


# ---------------------------------------------------------------------------
# Mount list: the loaded config is required, and ~ is expanded as the validator does.
# ---------------------------------------------------------------------------


def test_daemon_refuses_to_resolve_mounts_without_a_loaded_config() -> None:
    daemon = CoordinareDaemon(AsyncMock(), max_cycles=1, sleep_func=AsyncMock())

    with pytest.raises(RuntimeError, match="no loaded config"):
        daemon._performer_mounts()


def test_performer_mounts_expand_a_home_relative_volume(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    cfg = SimpleNamespace(
        performer_log_dir=None,
        performer_endpoints=[
            SimpleNamespace(volumes=[SimpleNamespace(host_path=Path("~/junie"))]),
        ],
        env_cache_root=tmp_path / "env",
    )

    assert home / "junie" in performer_mount_hosts(cfg)


# ---------------------------------------------------------------------------
# Wiring: the daemon's bootstrap path with a real config that has a performer volume.
# ---------------------------------------------------------------------------


def test_bootstrap_volume_path_uses_the_real_config_mounts(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    ws.mkdir()
    env_root = tmp_path / "env-caches"
    cfg = CoordinareConfiguration(**wrap_legacy_config(_raw(env_root, ws / "junie")))
    daemon = CoordinareDaemon(AsyncMock(), max_cycles=1, sleep_func=AsyncMock())
    daemon._state["coordinare_config"] = cfg
    cache = env_root / "sym"
    cache.mkdir(parents=True)
    daemon._state["env_cache"] = {
        "sym": EnvCacheState(symphony_name="sym", sanitised_name="sym", cache_dir=cache),
    }

    ec_result, _kw = daemon._resolve_bootstrap_volumes("p1", MagicMock(), "sym")
    assert ec_result is not None
    assert ec_result[0].host_path == cache

    # A link planted at the cache dir is refused by the walk built from the real config.
    victim = tmp_path / "victim"
    victim.mkdir()
    shutil.rmtree(cache)
    os.symlink(victim, cache)

    ec_result, _kw = daemon._resolve_bootstrap_volumes("p1", MagicMock(), "sym")
    assert ec_result is None
    assert list(victim.iterdir()) == []
