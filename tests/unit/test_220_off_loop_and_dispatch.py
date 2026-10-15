"""Spec 220 items 3, 9 and 10: the reads leave the event loop, clone roots, the dispatch mount list.

Item 3: ``/api/config/all``, ``/api/config/routing`` and the persona classifier's CLAUDE.md and
AGENTS.md reads run in a worker thread (``asyncio.to_thread``). The tests record which blocking
callables the loop hands to ``to_thread``.

Item 9: clone roots are not a mount list entry. Coordinare never bind-mounts a clone into a
performer: the container receives a URL, a branch and a token, and the only host directory it is
given is one an operator declared (``performer_log_dir``, an endpoint volume, the env-cache root).
The test pins that: no ``-v`` argument built for a container carries a clone path.

Item 10: the dispatch env-cache mount list comes from the global config, so a symphony override
that drops a volume cannot shorten it.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
import yaml
from fastapi.testclient import TestClient

from coordinare.config import CoordinareConfiguration, ProjectConfiguration
from coordinare.config_validation import coerce_multi_symphony_raw
from coordinare.dashboard import DashboardStore, create_dashboard_app
from coordinare.graph.nodes.dispatch_performer import _performer_mounts
from coordinare.models.performer_endpoint import PerformerEndpointConfig, VolumeMount
from coordinare.services import persona_classifier

_BASE: dict[str, Any] = {
    "project_name": "Demo",
    "github_org": "acme",
    "github_project_number": 1,
    "github_token": "tok",
    "human_reviewers": ["alice"],
}


def _recording_to_thread(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record the name of every callable the code hands to ``asyncio.to_thread``."""
    names: list[str] = []
    real = asyncio.to_thread

    async def recording(func: Any, /, *args: Any, **kwargs: Any) -> Any:
        names.append(getattr(func, "__name__", repr(func)))
        return await real(func, *args, **kwargs)

    monkeypatch.setattr(asyncio, "to_thread", recording)
    return names


def _client(config_path: Path) -> TestClient:
    raw = yaml.safe_load(config_path.read_text())
    validated_raw = {**raw, "github_token": "ghp_fixturetoken"}
    coordinare_cfg = CoordinareConfiguration(**coerce_multi_symphony_raw(validated_raw))
    daemon = MagicMock()
    daemon.state = {"coordinare_config": coordinare_cfg, "config_version": 7}
    daemon.running = True
    app = create_dashboard_app(
        DashboardStore(), daemon, MagicMock(), MagicMock(), config_path=config_path,
    )
    return TestClient(app, base_url="http://127.0.0.1:8090")


def test_config_all_reads_its_inputs_off_the_event_loop(
    temp_config_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    names = _recording_to_thread(monkeypatch)

    resp = _client(temp_config_path).get("/api/config/all")

    assert resp.status_code == 200
    assert "_build_config_snapshot_inputs" in names


def test_config_routing_reads_the_table_off_the_event_loop(
    temp_config_with_routing: tuple[Path, Path], monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_path, _routing_host = temp_config_with_routing
    names = _recording_to_thread(monkeypatch)

    resp = _client(config_path).get("/api/config/routing")

    assert resp.status_code == 200
    assert "read_routing" in names


def test_project_context_is_read_off_the_event_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "CLAUDE.md").write_text("project rules\n")
    names = _recording_to_thread(monkeypatch)

    context = asyncio.run(persona_classifier._read_project_context_async(tmp_path))

    assert "_read_project_context" in names
    assert context["claude_md_head"] == "project rules\n"


def _endpoint(volume: Path) -> PerformerEndpointConfig:
    return PerformerEndpointConfig(
        id="p1",
        mode="ephemeral",
        image="performer:full",
        roles=["implementer"],
        volumes=[VolumeMount(host_path=volume, container_path="/devenv/extra", mode="rw")],
    )


def test_dispatch_mounts_come_from_the_global_config_not_the_symphony(tmp_path: Path) -> None:
    """Item 10: a symphony override that drops a volume must not shorten the mount list."""
    volume = tmp_path / "extra-volume"
    volume.mkdir()
    global_cfg = ProjectConfiguration(
        **_BASE,
        env_cache_root=tmp_path / "caches",
        performer_endpoints=[_endpoint(volume)],
    )
    effective = global_cfg.model_copy(update={"performer_endpoints": []})
    state = {
        "config": effective,
        "coordinare_config": SimpleNamespace(global_config=global_cfg),
    }

    assert volume.resolve() in {p.resolve() for p in _performer_mounts(state)}


def test_no_container_argument_carries_a_clone_path(tmp_path: Path) -> None:
    """Item 9: the performer's container arguments name declared mounts only, never a clone.

    The clone root is ``workspace_root``/coordinare-ws-*. ``performer_mount_hosts`` lists the
    log dir, endpoint volumes and the env-cache root, and the docker argv carries ``-v`` for
    exactly those. Nothing in the dispatch path adds the daemon's clone directory.
    """
    from coordinare.services.mounted_file import performer_mount_hosts

    workspace = tmp_path / "workspaces"
    workspace.mkdir()
    cfg = ProjectConfiguration(
        **_BASE,
        env_cache_root=tmp_path / "caches",
        workspace_root=workspace,
        performer_endpoints=[_endpoint(tmp_path / "extra-volume")],
    )

    hosts = {p.resolve() for p in performer_mount_hosts(cfg)}

    assert workspace.resolve() not in hosts
    assert not any(part.startswith("coordinare-ws-") for host in hosts for part in host.parts)
