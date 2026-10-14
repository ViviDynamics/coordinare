"""Spec 219: every daemon-side env-cache write is mount-anchored.

The env-cache root is mounted writable into performers, so each attack below plants a link,
a hard link, a FIFO or a wrong-type entry where the daemon writes or reads, then checks the
host file the performer pointed at is untouched (content and mode) and the env-cache step
refuses cleanly. Calls run on a daemon thread with a bound, so a regression that blocks
(a FIFO opened for writing) fails the test instead of hanging the suite.
"""

from __future__ import annotations

import asyncio
import os
import stat
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from coordinare_service_inference.schema import ServiceEntry, ServiceInit, ServicesManifest
from coordinare_service_inference.templater import render
from structlog.testing import capture_logs

from coordinare.models.env_cache import EnvCacheState
from coordinare.services import env_cache
from coordinare.services.env_cache import (
    EnvCacheService,
    get_env_volume_for_symphony,
    write_service_scripts,
)
from coordinare.services.mounted_file import (
    anchored_mkdir,
    mounted_dir_refusal,
    read_mounted_dir_names,
)

HOST_TEXT = "HOST SECRET\n"
HOST_MODE = 0o640
SCRIPT_NAMES = ("services-start.sh", "services-stop.sh", "services-health.sh")
MANIFEST_NAMES = ("verify.sh", "activate.sh", "manifest.json")


def _bounded(fn: Any, *args: Any, seconds: float = 5.0) -> tuple[bool, Any]:
    """Run *fn* on a daemon thread. Returns (finished in time, result or the raised error)."""
    box: dict[str, Any] = {}

    def run() -> None:
        try:
            box["value"] = fn(*args)
        except Exception as exc:
            box["value"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(seconds)
    return (not thread.is_alive()), box.get("value")


def _host_file(tmp: Path) -> Path:
    host = tmp / "host-secret"
    host.write_text(HOST_TEXT)
    host.chmod(HOST_MODE)
    return host


def _assert_host_file_untouched(host: Path) -> None:
    assert host.read_text() == HOST_TEXT
    assert stat.S_IMODE(host.stat().st_mode) == HOST_MODE


def _cache(tmp: Path) -> Path:
    """A cache dir directly under an env-cache root, as the daemon lays them out."""
    cache = tmp / "env-caches" / "sym"
    cache.mkdir(parents=True)
    return cache


def _service_models() -> list[ServiceEntry]:
    pg = ServiceEntry(
        name="postgres",
        binary="postgres",
        version=None,
        data_dir="/tmp/pg",
        port=45432,
        why_needed="db",
        sources=["config/database.yml"],
        kind="postgres",
        init=ServiceInit(
            superuser="systemuser",
            databases=["website_test"],
            password_env_var="POSTGRESQL_PASSWORD",
        ),
    )
    redis = ServiceEntry(
        name="redis",
        binary="redis-server",
        version=None,
        data_dir="/tmp/redis",
        port=46379,
        why_needed="cache",
        sources=[".env.test"],
        kind="redis",
        start_args=["redis-server", "--port", "46379", "--dir", "/tmp/redis"],
    )
    return [pg, redis]


def _manifest(cache: Path) -> tuple[str | None, bool, bool]:
    """Run the manifest artifact writer for *cache*, the way the bootstrap does."""
    svc = EnvCacheService(MagicMock())
    files = {
        ".ruby-version": "3.4.2\n",
        "Gemfile": 'gem "rails"\n',
        "Gemfile.lock": "BUNDLED WITH\n   2.5.6\n",
    }

    async def fake_get(org: str, repo: str, path: str) -> str | None:
        return files.get(path)

    github = MagicMock()
    github.get_file_content = fake_get
    return asyncio.run(
        svc._build_manifest_artifacts(
            performer_mounts=(),
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
# Writes under the cache dir: a link planted at each written file.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", MANIFEST_NAMES)
def test_link_at_a_manifest_file_is_refused_and_host_untouched(tmp_path: Path, name: str) -> None:
    host = _host_file(tmp_path)
    cache = _cache(tmp_path)
    os.symlink(host, cache / name)

    finished, result = _bounded(_manifest, cache)

    assert finished is True
    assert result[1] is False  # verify_provided
    _assert_host_file_untouched(host)


@pytest.mark.parametrize("name", SCRIPT_NAMES)
def test_link_at_a_service_script_is_refused_and_host_untouched(tmp_path: Path, name: str) -> None:
    host = _host_file(tmp_path)
    cache = _cache(tmp_path)
    (cache / "services").mkdir()
    os.symlink(host, cache / "services" / name)

    assert (
        write_service_scripts(cache, _service_models(), symphony="sym", performer_mounts=())
        is False
    )
    _assert_host_file_untouched(host)


def test_link_at_services_dir_is_refused_and_host_dir_untouched(tmp_path: Path) -> None:
    host_dir = tmp_path / "host-dir"
    host_dir.mkdir()
    (host_dir / "keep.txt").write_text("keep\n")
    cache = _cache(tmp_path)
    os.symlink(host_dir, cache / "services")

    assert (
        write_service_scripts(cache, _service_models(), symphony="sym", performer_mounts=())
        is False
    )
    assert sorted(p.name for p in host_dir.iterdir()) == ["keep.txt"]


def test_link_at_cache_dir_is_refused_and_host_dir_untouched(tmp_path: Path) -> None:
    """The cache dir itself is a link (a child of the env-cache root) to a host directory."""
    host_dir = tmp_path / "host-dir"
    host_dir.mkdir()
    (host_dir / "keep.txt").write_text("keep\n")
    root = tmp_path / "env-caches"
    root.mkdir()
    os.symlink(host_dir, root / "sym")

    finished, result = _bounded(_manifest, root / "sym")

    assert finished is True
    assert result[1] is False
    assert (
        write_service_scripts(root / "sym", _service_models(), symphony="sym", performer_mounts=())
        is False
    )
    assert sorted(p.name for p in host_dir.iterdir()) == ["keep.txt"]


def test_initialise_refuses_a_linked_cache_dir_and_skips_the_symphony(tmp_path: Path) -> None:
    host_dir = tmp_path / "host-dir"
    host_dir.mkdir()
    root = tmp_path / "env-caches"
    root.mkdir()
    name = "sym"
    os.symlink(host_dir, root / env_cache.sanitise_symphony_name(name))
    cfg = SimpleNamespace(
        global_config=SimpleNamespace(
            env_cache_root=root, performer_log_dir=None, performer_endpoints=[],
        ),
        symphonies=[SimpleNamespace(name=name, env_bootstrap_performer_id="performer")],
    )
    states: dict[str, Any] = {}

    with capture_logs() as logs:
        asyncio.run(EnvCacheService(cfg).initialise(states, {}))

    assert states == {}
    assert list(host_dir.iterdir()) == []
    assert any(
        entry["event"] == "env_cache.mkdir_refused" and entry["log_level"] == "error"
        for entry in logs
    )


# ---------------------------------------------------------------------------
# Hard links, FIFOs and wrong-type entries at a target or a directory.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", MANIFEST_NAMES)
def test_hard_link_at_a_manifest_file_is_refused(tmp_path: Path, name: str) -> None:
    host = _host_file(tmp_path)
    cache = _cache(tmp_path)
    os.link(host, cache / name)

    finished, result = _bounded(_manifest, cache)

    assert finished is True
    assert result[1] is False
    _assert_host_file_untouched(host)


def test_hard_link_at_a_service_script_is_refused(tmp_path: Path) -> None:
    host = _host_file(tmp_path)
    cache = _cache(tmp_path)
    (cache / "services").mkdir()
    os.link(host, cache / "services" / "services-start.sh")

    assert (
        write_service_scripts(cache, _service_models(), symphony="sym", performer_mounts=())
        is False
    )
    _assert_host_file_untouched(host)


@pytest.mark.parametrize("name", MANIFEST_NAMES)
def test_fifo_at_a_manifest_file_does_not_block_and_is_kept(tmp_path: Path, name: str) -> None:
    cache = _cache(tmp_path)
    os.mkfifo(cache / name)

    finished, result = _bounded(_manifest, cache)

    assert finished is True
    assert result[1] is False
    assert stat.S_ISFIFO((cache / name).lstat().st_mode)


def test_fifo_at_a_service_script_does_not_block(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    (cache / "services").mkdir()
    os.mkfifo(cache / "services" / "services-health.sh")

    finished, result = _bounded(
        lambda: write_service_scripts(
            cache, _service_models(), symphony="sym", performer_mounts=(),
        ),
    )

    assert finished is True
    assert result is False
    assert stat.S_ISFIFO((cache / "services" / "services-health.sh").lstat().st_mode)


def test_regular_file_where_services_dir_is_expected_is_refused(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    (cache / "services").write_text("keep\n")

    assert (
        write_service_scripts(cache, _service_models(), symphony="sym", performer_mounts=())
        is False
    )
    assert (cache / "services").read_text() == "keep\n"


def test_regular_file_where_cache_dir_is_expected_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "env-caches"
    root.mkdir()
    (root / "sym").write_text("keep\n")

    finished, result = _bounded(_manifest, root / "sym")

    assert finished is True
    assert result[1] is False
    assert (root / "sym").read_text() == "keep\n"


# ---------------------------------------------------------------------------
# Reads that feed a decision: a link must not count as a toolchain or a cache.
# ---------------------------------------------------------------------------


def test_linked_activate_sh_is_not_a_toolchain(tmp_path: Path) -> None:
    host = _host_file(tmp_path)
    cache = _cache(tmp_path)
    os.symlink(host, cache / "activate.sh")

    assert (
        env_cache._cache_dir_has_activate(
            cache, performer_mounts=(), expected_activate=host.read_text(),
        )
        is False
    )
    _assert_host_file_untouched(host)


def test_linked_cache_dir_is_not_populated(tmp_path: Path) -> None:
    host_dir = tmp_path / "host-dir"
    (host_dir / "rbenv").mkdir(parents=True)
    root = tmp_path / "env-caches"
    root.mkdir()
    os.symlink(host_dir, root / "sym")

    assert env_cache._cache_dir_is_populated(root / "sym", performer_mounts=()) is False


def test_bootstrap_mount_is_not_offered_for_a_linked_cache_dir(tmp_path: Path) -> None:
    host_dir = tmp_path / "host-dir"
    host_dir.mkdir()
    root = tmp_path / "env-caches"
    root.mkdir()
    os.symlink(host_dir, root / "sym")
    state = EnvCacheState(
        symphony_name="sym",
        sanitised_name="sym",
        cache_dir=root / "sym",
    )

    mount = get_env_volume_for_symphony(
        "sym", {"sym": state}, is_bootstrap=True, performer_mounts=(),
    )

    assert mount is None


def test_anchored_mkdir_refuses_a_link_in_the_middle(tmp_path: Path) -> None:
    host_dir = tmp_path / "host-dir"
    host_dir.mkdir()
    root = tmp_path / "env-caches"
    (root / "sym").mkdir(parents=True)
    os.symlink(host_dir, root / "sym" / "services")

    with pytest.raises(OSError):
        anchored_mkdir(root / "sym" / "services" / "deeper", mounts=(root,))
    assert list(host_dir.iterdir()) == []


def test_dir_names_refuse_a_link_and_mount_check_reports_it(tmp_path: Path) -> None:
    host_dir = tmp_path / "host-dir"
    host_dir.mkdir()
    root = tmp_path / "env-caches"
    root.mkdir()
    os.symlink(host_dir, root / "sym")

    assert read_mounted_dir_names(root / "sym", mounts=(root,)) is None
    assert mounted_dir_refusal(root / "sym", mounts=(root,)) is not None
    assert mounted_dir_refusal(root / "absent", mounts=(root,)) == "missing"


# ---------------------------------------------------------------------------
# Happy path: the same bytes and modes as before the change.
# ---------------------------------------------------------------------------


def test_service_scripts_keep_bytes_and_mode(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    models = _service_models()
    manifest = ServicesManifest(
        services=models,
        cache_inputs=["score.json"],
        agent_version="coordinare-owned-108",
    )
    expected = render(manifest)

    assert write_service_scripts(cache, models, symphony="sym", performer_mounts=()) is True

    bodies = {
        "services-start.sh": expected.start,
        "services-stop.sh": expected.stop,
        "services-health.sh": expected.health,
    }
    for name, body in bodies.items():
        path = cache / "services" / name
        assert path.read_bytes() == body.encode()
        assert stat.S_IMODE(path.stat().st_mode) == 0o755
    assert not (cache / "services" / "services.json").exists()


def test_service_scripts_overwrite_an_existing_script_with_mode_755(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    (cache / "services").mkdir()
    old = cache / "services" / "services-start.sh"
    old.write_text("old\n")
    old.chmod(0o600)

    assert (
        write_service_scripts(cache, _service_models(), symphony="sym", performer_mounts=()) is True
    )

    assert "initdb" in old.read_text()
    assert stat.S_IMODE(old.stat().st_mode) == 0o755


def test_manifest_artifacts_happy_path_modes_and_contents(tmp_path: Path) -> None:
    cache = tmp_path / "env-caches" / "sym"

    checklist, verify_provided, activate_provided = _manifest(cache)

    assert checklist is not None
    assert verify_provided is True
    assert activate_provided is True
    assert "RUBY_VERSION" in (cache / "verify.sh").read_text()
    assert "$DEVENV/.rbenv/versions/3.4.2" in (cache / "activate.sh").read_text()
    assert "rails" in (cache / "manifest.json").read_text()
    assert stat.S_IMODE((cache / "verify.sh").stat().st_mode) == 0o755
    assert stat.S_IMODE((cache / "activate.sh").stat().st_mode) == 0o755
    assert stat.S_IMODE((cache / "manifest.json").stat().st_mode) == 0o644


def test_manifest_json_keeps_the_mode_of_an_existing_file(tmp_path: Path) -> None:
    cache = _cache(tmp_path)
    (cache / "manifest.json").write_text("{}")
    (cache / "manifest.json").chmod(0o600)

    _manifest(cache)

    assert stat.S_IMODE((cache / "manifest.json").stat().st_mode) == 0o600
    assert "rails" in (cache / "manifest.json").read_text()


def test_refused_write_logs_a_named_error(tmp_path: Path) -> None:
    host = _host_file(tmp_path)
    cache = _cache(tmp_path)
    os.symlink(host, cache / "verify.sh")

    with capture_logs() as logs:
        _manifest(cache)

    assert any(
        entry["event"] == "env_cache.write_refused" and entry["log_level"] == "error"
        for entry in logs
    )
    _assert_host_file_untouched(host)
