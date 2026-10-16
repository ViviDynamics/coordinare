from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError
from scripts import smoke_docker_setup

from coordinare.config import ProjectConfiguration
from coordinare.init_files import configure_containers
from coordinare.models.performer_endpoint import PerformerEndpointConfig, VolumeMount
from coordinare.routing_config_service import locate_routing_file
from coordinare.services import performer_lifecycle
from coordinare.services.mounted_file import performer_mount_hosts


@pytest.mark.parametrize("source", [
    {}, {"volume_name": "valid-name", "host_path": "/tmp/host"},
    {"volume_name": "../escape"}, {"volume_name": "a:b"},
    {"volume_name": "-option"}, {"volume_name": "has space"}, {"volume_name": ""},
])
def test_mount_requires_one_valid_source(source: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        VolumeMount.model_validate({**source, "container_path": "/workspace"})


def test_named_mount_roundtrip_and_read_only_default() -> None:
    mount = VolumeMount.model_validate({"volume_name": "coordinare-workspaces_a.1", "container_path": "/workspace"})
    assert mount.host_path is None
    assert mount.mode == "ro"
    assert VolumeMount.model_validate(mount.model_dump()) == mount


def test_init_generates_volume_without_host_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    raw = {"preset": "containers", "harness": "codex", "github_org": "example", "github_repo": "test"}
    configure_containers(raw, "image:test", {})
    mounts = raw["performer_endpoints"][0]["volumes"]
    assert mounts[0]["volume_name"].startswith("coordinare-workspaces-")
    assert "host_path" not in mounts[0]
    assert mounts[0]["mode"] == "rw"
    assert mounts[1]["host_path"] == str(tmp_path / "data/coordinare/artifacts")
    assert not (tmp_path / "data/coordinare/workspaces").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["ro", "rw"])
async def test_runtime_named_mount_args(mode: str, monkeypatch: pytest.MonkeyPatch) -> None:
    commands = []

    async def run(*args: str, **kwargs: object) -> tuple[int, str, str]:
        commands.append(args)
        return (0, "container", "") if args[0] == "run" else (0, "127.0.0.1:1234", "")

    monkeypatch.setattr(performer_lifecycle, "_run_docker", run)
    endpoint = PerformerEndpointConfig.model_validate({
        "id": "named", "mode": "ephemeral", "roles": ["implementer"], "image": "test",
        "volumes": [{"volume_name": "test-workspaces", "container_path": "/workspace", "mode": mode},
                    {"host_path": "/host/artifacts", "container_path": "/artifacts", "mode": "rw"}],
    })
    await performer_lifecycle.start_ephemeral(endpoint)
    assert f"test-workspaces:/workspace:{mode}" in commands[0]
    assert "/host/artifacts:/artifacts:rw" in commands[0]


def test_host_readers_skip_named_volumes() -> None:
    config = ProjectConfiguration(
        github_token="test", github_org="example", github_repo="test", github_project_number=1, human_reviewers=["reviewer"],
        performer_endpoints=[PerformerEndpointConfig.model_validate({
            "id": "named", "mode": "ephemeral", "roles": ["implementer"], "image": "test",
            "env": {"SELFHOSTED_ROUTING_CONFIG": "/workspace"},
            "volumes": [{"volume_name": "test-workspaces", "container_path": "/workspace"}],
        })],
    )
    assert config._performer_mount_entries() == []
    assert performer_mount_hosts(config) == [config.env_cache_root]
    assert locate_routing_file(config.performer_endpoints) is None


def test_smoke_detects_root_ownership_without_following_links(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "data"
    root.mkdir()
    (root / "file").write_text("owned")
    (root / "outside-link").symlink_to("/does-not-exist")
    original = Path.lstat

    def root_stat(path: Path):
        result = original(path)
        return MagicMock(st_uid=0 if path.name == "file" else 1000, st_mode=result.st_mode)

    monkeypatch.setattr(Path, "lstat", root_stat)
    with pytest.raises(AssertionError, match="root-owned"):
        smoke_docker_setup.assert_no_root_owned_files(root)


def test_volume_identity_is_stable_and_isolates_config_directories(tmp_path: Path) -> None:
    from coordinare.user_config import workspace_volume_name

    first = tmp_path / "first"
    first.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(first, target_is_directory=True)
    assert workspace_volume_name(first) == workspace_volume_name(alias)
    assert workspace_volume_name(first) != workspace_volume_name(tmp_path / "second")


def test_init_preserves_custom_named_workspace_and_other_endpoints(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    workspace = {"volume_name": "custom-workspace", "container_path": "/workspace", "mode": "ro"}
    other = {"id": "other", "mode": "ephemeral", "image": "other:test", "roles": ["reviewer"],
             "volumes": [{"host_path": str(tmp_path / "other"), "container_path": "/workspace", "mode": "rw"}]}
    raw = {"preset": "containers", "harness": "codex", "github_org": "example", "github_repo": "test",
           "performer_endpoints": [{"id": "starter", "mode": "ephemeral", "roles": ["implementer"],
                                    "volumes": [workspace]}, other]}
    configure_containers(raw, "image:test", {}, config_directory=tmp_path / "config")
    assert raw["performer_endpoints"][0]["volumes"] == [workspace]
    assert raw["performer_endpoints"][1]["volumes"] == other["volumes"]


@pytest.mark.asyncio
async def test_local_image_smoke_uses_named_workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from contextlib import asynccontextmanager
    from types import SimpleNamespace

    @asynccontextmanager
    async def fakes(directory: Path):
        yield SimpleNamespace(probe_environment={}, assert_probed=lambda: None)

    seen = []
    monkeypatch.setattr(smoke_docker_setup, "connectivity_fakes", fakes)
    monkeypatch.setattr(smoke_docker_setup.docker_setup, "smoke_container", lambda endpoint, **kwargs: seen.append(endpoint))
    await smoke_docker_setup.smoke_with_fakes(["--local-image", "local:test"], tmp_path)
    assert seen[0].env["TMPDIR"] == "/workspace"
    assert seen[0].volumes[0].volume_name.startswith("coordinare-workspaces-")
    assert seen[0].volumes[0].host_path is None


def test_main_checks_ownership_before_cleanup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from unittest.mock import AsyncMock

    directory = tmp_path / "smoke"
    data = directory / "data"
    data.mkdir(parents=True)
    bad = data / "root-file"
    bad.write_text("root")
    original = Path.lstat

    def fake_stat(path: Path):
        stat = original(path)
        return MagicMock(st_uid=0 if path == bad else 1000, st_mode=stat.st_mode)

    monkeypatch.setattr(Path, "lstat", fake_stat)
    monkeypatch.setattr(smoke_docker_setup.tempfile, "mkdtemp", lambda **kwargs: str(directory))
    monkeypatch.setattr(smoke_docker_setup, "smoke_with_fakes", AsyncMock(return_value="local:test"))
    monkeypatch.setattr(smoke_docker_setup.docker_setup, "run_docker", MagicMock())
    with pytest.raises(AssertionError, match="root-owned"):
        smoke_docker_setup.main(["--local-image", "local:test"])
    assert not directory.exists()
