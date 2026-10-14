"""Spec 219, review round: the env-cache root under a performer mount, and the follow-ups.

The review found that a root sitting under a performer endpoint volume can be replaced, by the
performer, with a link to a host directory. Every env-cache read and write now walks from the
outermost performer mount, so the planted link is refused. Config load also rejects a root that
overlaps a performer mount. The remaining items cover the inherited mode, the bootstrap
diagnostic and the test-env host read.
"""

from __future__ import annotations

import asyncio
import os
import stat
from pathlib import Path, PurePosixPath
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pydantic
import pytest
from structlog.testing import capture_logs

from coordinare.config import ProjectConfiguration, TestEnvConfig
from coordinare.models.env_cache import EnvCacheState
from coordinare.models.performer_endpoint import PerformerEndpointConfig, VolumeMount
from coordinare.services import env_cache
from coordinare.services.env_cache import EnvCacheService, get_env_volume_for_symphony
from coordinare.services.mounted_file import replace_mounted_file
from coordinare.services.test_env_loader import TestEnvFileError, load_test_env

VICTIM_TEXT = "HOST SECRET\n"


def _victim(tmp: Path) -> tuple[Path, Path]:
    """A host directory the attacker wants written into, and a file inside it."""
    victim = tmp / "host" / "victim"
    (victim / "sym").mkdir(parents=True)
    secret = victim / "secret.env"
    secret.write_text(VICTIM_TEXT)
    secret.chmod(0o640)
    return victim, secret


def _performer_root(tmp: Path) -> tuple[Path, Path, Path]:
    """A performer volume ``ws`` with env_cache_root ``ws/caches`` replaced by a link to the victim.

    Returns ``(victim, ws, root)``. The root is what EnvCacheService is configured with.
    """
    victim, _secret = _victim(tmp)
    ws = tmp / "ws"
    ws.mkdir()
    root = ws / "caches"
    os.symlink(victim, root)
    return victim, ws, root


def _manifest(cache: Path, performer_mounts: tuple[Path, ...]) -> tuple[str | None, bool, bool]:
    svc = EnvCacheService(MagicMock())
    files = {".ruby-version": "3.4.2\n", "Gemfile": 'gem "rails"\n'}

    async def fake_get(org: str, repo: str, path: str) -> str | None:
        return files.get(path)

    github = MagicMock()
    github.get_file_content = fake_get
    return asyncio.run(
        svc._build_manifest_artifacts(
            performer_mounts=performer_mounts,
            symphony_name="sym",
            repo="sym",
            github_org="org",
            github_service=github,
            readme_contents={"README.md": "Rails app."},
            current_sha="abc123",
            cache_dir=cache,
            cache_mount_path="/devenv/sym",
            llm_chat=None,
        ),
    )


# ---------------------------------------------------------------------------
# The block: a root that is a link under a performer volume.
# ---------------------------------------------------------------------------


def test_root_link_under_performer_volume_writes_are_refused(tmp_path: Path) -> None:
    victim, ws, root = _performer_root(tmp_path)
    secret = victim / "secret.env"

    checklist, verify_provided, activate_provided = _manifest(root / "sym", (ws,))

    assert verify_provided is False
    assert activate_provided is False
    assert sorted(p.name for p in (victim / "sym").iterdir()) == []
    assert secret.read_text() == VICTIM_TEXT
    assert stat.S_IMODE(secret.stat().st_mode) == 0o640
    assert checklist is not None


def test_root_link_under_performer_volume_is_not_offered_as_a_bootstrap_mount(
    tmp_path: Path,
) -> None:
    _victim_dir, ws, root = _performer_root(tmp_path)
    state = EnvCacheState(
        symphony_name="sym",
        sanitised_name="sym",
        cache_dir=root / "sym",
    )

    mount = get_env_volume_for_symphony(
        "sym",
        {"sym": state},
        is_bootstrap=True,
        performer_mounts=(ws,),
    )

    assert mount is None


def test_root_link_under_performer_volume_is_not_a_consumer_toolchain(tmp_path: Path) -> None:
    victim, ws, root = _performer_root(tmp_path)
    (victim / "sym" / "activate.sh").write_text("HOST\n")

    assert env_cache._cache_dir_has_activate(root / "sym", performer_mounts=(ws,)) is False


# ---------------------------------------------------------------------------
# Config load: the root may not overlap a performer-visible host path.
# ---------------------------------------------------------------------------

_BASE: dict[str, Any] = {
    "project_name": "Demo",
    "github_org": "acme",
    "github_project_number": 1,
    "github_token": "tok",
    "human_reviewers": ["alice"],
}


def _ephemeral(volume_host: Path) -> PerformerEndpointConfig:
    return PerformerEndpointConfig(
        id="p1",
        mode="ephemeral",
        image="performer:full",
        roles=["implementer"],
        volumes=[
            VolumeMount(
                host_path=volume_host,
                container_path=PurePosixPath("/work"),
                mode="rw",
            ),
        ],
    )


def test_root_inside_an_endpoint_volume_is_rejected_at_load(tmp_path: Path) -> None:
    volume = tmp_path / "ws"
    with pytest.raises(pydantic.ValidationError) as excinfo:
        ProjectConfiguration(
            **_BASE,
            env_cache_root=volume / "caches",
            performer_endpoints=[_ephemeral(volume)],
        )
    message = str(excinfo.value)
    assert "env_cache_root" in message
    assert str((volume / "caches").resolve()) in message
    assert str(volume.resolve()) in message
    assert "performer_endpoints[p1].volumes[0].host_path" in message


def test_volume_inside_the_root_is_rejected_at_load(tmp_path: Path) -> None:
    root = tmp_path / "env-caches"
    with pytest.raises(pydantic.ValidationError, match="overlaps"):
        ProjectConfiguration(
            **_BASE,
            env_cache_root=root,
            performer_endpoints=[_ephemeral(root / "sym")],
        )


def test_root_equal_to_the_performer_log_dir_is_rejected_at_load(tmp_path: Path) -> None:
    shared = tmp_path / "shared"
    with pytest.raises(pydantic.ValidationError, match="performer_log_dir"):
        ProjectConfiguration(**_BASE, env_cache_root=shared, performer_log_dir=shared)


def test_disjoint_root_and_volume_load(tmp_path: Path) -> None:
    cfg = ProjectConfiguration(
        **_BASE,
        env_cache_root=tmp_path / "env-caches",
        performer_endpoints=[_ephemeral(tmp_path / "junie")],
    )
    assert cfg.env_cache_root == (tmp_path / "env-caches").resolve()


# ---------------------------------------------------------------------------
# Inherited mode is masked: no setuid, setgid, sticky or group/other write.
# ---------------------------------------------------------------------------


def test_planted_setuid_manifest_comes_back_without_special_bits(tmp_path: Path) -> None:
    cache = tmp_path / "env-caches" / "sym"
    cache.mkdir(parents=True)
    planted = cache / "manifest.json"
    planted.write_text("{}")
    planted.chmod(0o4777)

    replace_mounted_file(planted, b"{}\n", mounts=(tmp_path / "env-caches",))

    assert stat.S_IMODE(planted.stat().st_mode) == 0o755


def test_inherited_group_and_other_write_are_dropped(tmp_path: Path) -> None:
    target = tmp_path / "routing.yaml"
    target.write_text("x")
    target.chmod(0o666)

    replace_mounted_file(target, b"y", mounts=(tmp_path,))

    assert stat.S_IMODE(target.stat().st_mode) == 0o644


def test_explicit_mode_is_applied_as_given(tmp_path: Path) -> None:
    target = tmp_path / "script.sh"
    target.write_text("x")
    target.chmod(0o600)

    replace_mounted_file(target, b"y", mounts=(tmp_path,), mode=0o755)

    assert stat.S_IMODE(target.stat().st_mode) == 0o755


# ---------------------------------------------------------------------------
# The bootstrap diagnostic reports a refused cache dir, not "wrong location".
# ---------------------------------------------------------------------------


def test_refused_cache_dir_is_reported_as_refused_not_wrong_location(tmp_path: Path) -> None:
    victim, _ws, root = _performer_root(tmp_path)
    (victim / "sym" / "rbenv").mkdir()  # real toolchain content on the host side
    cfg = SimpleNamespace(
        symphonies=[],
        global_config=SimpleNamespace(
            env_bootstrap_max_attempts=3,
            performer_log_dir=None,
            performer_endpoints=[],
            env_cache_root=str(root.parent),
        ),
    )
    svc = EnvCacheService(cfg)
    state: dict[str, Any] = {
        "env_cache": {
            "sym": EnvCacheState(
                symphony_name="sym",
                sanitised_name="sym",
                cache_dir=root / "sym",
            ),
        },
    }

    with capture_logs():
        svc.on_bootstrap_complete("sym", True, state, None)

    error = state["env_cache"]["sym"].last_bootstrap_error or ""
    assert "refused" in error
    assert "wrong location" not in error


# ---------------------------------------------------------------------------
# A host_path test-env file inside a performer mount is read anchored.
# ---------------------------------------------------------------------------


def test_host_path_test_env_through_a_linked_mount_is_refused(tmp_path: Path) -> None:
    victim, ws, root = _performer_root(tmp_path)
    (victim / "sym" / "test.env").write_text("DB_PASSWORD=host-value\n")
    env_path = root / "sym" / "test.env"

    with pytest.raises(TestEnvFileError, match="refused"):
        load_test_env(
            TestEnvConfig(host_path=str(env_path)),
            repo_root=tmp_path,
            performer_mounts=(ws,),
        )


def test_host_path_test_env_regular_file_in_a_mount_still_loads(tmp_path: Path) -> None:
    ws = tmp_path / "ws"
    (ws / "sym").mkdir(parents=True)
    (ws / "sym" / "test.env").write_text("KEY=value\n")

    out = load_test_env(
        TestEnvConfig(host_path=str(ws / "sym" / "test.env")),
        repo_root=tmp_path,
        performer_mounts=(ws,),
    )

    assert out == {"KEY": "value"}
