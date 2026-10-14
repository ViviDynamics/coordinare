"""Spec 220 items 1 and 8: the state directory and ``workspace_root`` stay out of performer mounts.

Item 1: the state store loads and saves by path. A state directory inside a performer mount lets
a performer plant a link at the state file or a temporary name, so config load refuses it. The
rule is the 219 overlap rule: resolved paths, case folded where the filesystem folds case, NFC
before any fold, ``~`` expanded.

Item 8: a clone under ``workspace_root`` is reached by path-based git and ``rmtree``. A
``workspace_root`` inside a performer mount is refused at load. Without an explicit root the
clones go under the system temp directory, so the same rule applies to that directory.
"""

from __future__ import annotations

import tempfile
import unicodedata
from pathlib import Path
from typing import Any

import pydantic
import pytest

from coordinare.config import ProjectConfiguration
from coordinare.models.performer_endpoint import PerformerEndpointConfig, VolumeMount
from coordinare.services.run_transcript import _case_insensitive

_BASE: dict[str, Any] = {
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
        volumes=[VolumeMount(host_path=volume, container_path="/devenv/extra", mode="rw")],
    )


def test_state_file_inside_an_endpoint_volume_fails_to_load(tmp_path: Path) -> None:
    volume = tmp_path / "shared"
    volume.mkdir()
    with pytest.raises(pydantic.ValidationError, match="state_file_path"):
        ProjectConfiguration(
            **_BASE,
            env_cache_root=tmp_path / "caches",
            state_file_path=volume / "state" / "coordinare.state.json",
            performer_endpoints=[_ephemeral(volume)],
        )


def test_state_file_inside_the_performer_log_dir_fails_to_load(tmp_path: Path) -> None:
    log_dir = tmp_path / "perf-log"
    log_dir.mkdir()
    with pytest.raises(pydantic.ValidationError, match="state_file_path"):
        ProjectConfiguration(
            **_BASE,
            env_cache_root=tmp_path / "caches",
            performer_log_dir=log_dir,
            state_file_path=log_dir / "coordinare.state.json",
        )


def test_state_file_outside_every_mount_loads(tmp_path: Path) -> None:
    volume = tmp_path / "shared"
    volume.mkdir()
    state = tmp_path / "daemon" / "coordinare.state.json"
    cfg = ProjectConfiguration(
        **_BASE,
        env_cache_root=tmp_path / "caches",
        state_file_path=state,
        performer_endpoints=[_ephemeral(volume)],
    )
    assert cfg.state_file_path == state


def test_state_file_under_a_case_variant_of_a_volume_is_refused_where_case_folds(
    tmp_path: Path,
) -> None:
    volume = tmp_path / "vol"
    volume.mkdir()
    spelled = tmp_path / "VOL" / "coordinare.state.json"
    if _case_insensitive(tmp_path):
        with pytest.raises(pydantic.ValidationError, match="state_file_path"):
            ProjectConfiguration(
                **_BASE,
                env_cache_root=tmp_path / "caches",
                state_file_path=spelled,
                performer_endpoints=[_ephemeral(volume)],
            )
    else:
        cfg = ProjectConfiguration(
            **_BASE,
            env_cache_root=tmp_path / "caches",
            state_file_path=spelled,
            performer_endpoints=[_ephemeral(volume)],
        )
        assert cfg.state_file_path == spelled


def test_state_file_with_a_nfd_spelling_of_a_volume_is_refused(tmp_path: Path) -> None:
    """APFS treats NFC and NFD spellings as one name, so the NFD state path is inside the volume."""
    nfc = unicodedata.normalize("NFC", "café")
    nfd = unicodedata.normalize("NFD", "café")
    volume = tmp_path / nfc
    volume.mkdir()
    with pytest.raises(pydantic.ValidationError, match="state_file_path"):
        ProjectConfiguration(
            **_BASE,
            env_cache_root=tmp_path / "caches",
            state_file_path=tmp_path / nfd / "coordinare.state.json",
            performer_endpoints=[_ephemeral(volume)],
        )


def test_workspace_root_inside_an_endpoint_volume_fails_to_load(tmp_path: Path) -> None:
    volume = tmp_path / "shared"
    volume.mkdir()
    with pytest.raises(pydantic.ValidationError, match="workspace_root"):
        ProjectConfiguration(
            **_BASE,
            env_cache_root=tmp_path / "caches",
            workspace_root=volume / "workspaces",
            performer_endpoints=[_ephemeral(volume)],
        )


def test_workspace_root_outside_every_mount_loads(tmp_path: Path) -> None:
    volume = tmp_path / "shared"
    volume.mkdir()
    workspace = tmp_path / "workspaces"
    cfg = ProjectConfiguration(
        **_BASE,
        env_cache_root=tmp_path / "caches",
        workspace_root=workspace,
        performer_endpoints=[_ephemeral(volume)],
    )
    assert cfg.workspace_root == workspace


def test_default_workspace_under_a_mount_that_holds_the_temp_dir_fails_to_load() -> None:
    """With no ``workspace_root`` the clones go under the temp dir, so a mount over it is refused."""
    temp_dir = Path(tempfile.gettempdir()).resolve()
    # The env-cache root sits beside the volume, not inside it, so only the workspace rule fires.
    cache_root = temp_dir.parent.parent / "coordinare-env-cache-probe"
    with pytest.raises(pydantic.ValidationError, match="workspace_root"):
        ProjectConfiguration(
            **_BASE,
            env_cache_root=cache_root,
            performer_endpoints=[_ephemeral(temp_dir.parent)],
        )
