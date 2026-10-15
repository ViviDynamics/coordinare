"""Spec 234: freezing the env cache into a digest manifest.

The freeze is the daemon's only read of a cache the bootstrap wrote. These tests pin what it records,
what it refuses (links that leave the cache, hard links, special files, over-bound caches), and that the
saved manifest is trusted only when it hashes to the digest the snapshot holds.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from coordinare.services import env_provenance
from coordinare.services.env_provenance import (
    ProvenanceRefusedError,
    freeze_cache,
    load_manifest,
    save_manifest,
)

ACTIVATE = "# daemon-rendered activate\nexport PATH=$DEVENV/bin:$PATH\n"


def _cache(tmp_path: Path) -> tuple[Path, tuple[Path, ...]]:
    """A cache dir under an env-cache root, which is itself the performer mount."""
    root = tmp_path / "caches"
    cache = root / "sym-abc"
    (cache / "bin").mkdir(parents=True)
    (cache / "bin" / "ruby").write_text("#!/bin/sh\necho ruby 3.4.2\n")
    (cache / "bin" / "ruby").chmod(0o755)
    (cache / "activate.sh").write_text(ACTIVATE)
    (cache / "manifest.json").write_text("{}")
    (cache / "verify.sh").write_text("#!/bin/sh\n")
    return cache, (root,)


def _freeze(cache: Path, mounts: tuple[Path, ...]) -> env_provenance.ProvenanceManifest:
    return freeze_cache(
        cache,
        symphony="sym",
        cache_key="abc123",
        activate_sh=ACTIVATE,
        performer_mounts=mounts,
    )


def test_honest_freeze_records_every_entry_with_digests(tmp_path: Path) -> None:
    cache, mounts = _cache(tmp_path)
    manifest = _freeze(cache, mounts)
    by_path = {e.path: e for e in manifest.entries}
    assert by_path["bin/ruby"].kind == "file"
    assert by_path["bin/ruby"].sha256 is not None
    assert by_path["bin"].kind == "dir"
    assert manifest.activate_sh == ACTIVATE
    assert manifest.services_start_sh is None


def test_freeze_carries_service_scripts_as_frozen_bytes(tmp_path: Path) -> None:
    cache, mounts = _cache(tmp_path)
    (cache / "services").mkdir()
    (cache / "services" / "services-start.sh").write_text("echo started\n")
    (cache / "services" / "services-health.sh").write_text("echo healthy\n")
    manifest = _freeze(cache, mounts)
    assert manifest.services_start_sh == "echo started\n"
    assert manifest.services_health_sh == "echo healthy\n"


def test_debs_are_listed_by_digest(tmp_path: Path) -> None:
    cache, mounts = _cache(tmp_path)
    (cache / "debs").mkdir()
    (cache / "debs" / "libfoo.deb").write_bytes(b"deb-bytes")
    manifest = _freeze(cache, mounts)
    assert set(manifest.debs) == {"debs/libfoo.deb"}
    assert len(manifest.debs["debs/libfoo.deb"]) == 64


def test_relative_link_that_stays_inside_the_cache_is_recorded(tmp_path: Path) -> None:
    cache, mounts = _cache(tmp_path)
    (cache / "lib").mkdir()
    (cache / "lib" / "libssl.so.3").write_text("x")
    os.symlink("libssl.so.3", cache / "lib" / "libssl.so")
    manifest = _freeze(cache, mounts)
    link = next(e for e in manifest.entries if e.path == "lib/libssl.so")
    assert link.kind == "link"
    assert link.target == "libssl.so.3"


def test_absolute_link_refuses_the_freeze(tmp_path: Path) -> None:
    cache, mounts = _cache(tmp_path)
    os.symlink("/bin/sh", cache / "bin" / "sh")
    with pytest.raises(ProvenanceRefusedError, match="link"):
        _freeze(cache, mounts)


def test_link_that_climbs_out_of_the_cache_refuses_the_freeze(tmp_path: Path) -> None:
    cache, mounts = _cache(tmp_path)
    os.symlink("../../../etc", cache / "bin" / "escape")
    with pytest.raises(ProvenanceRefusedError, match="link"):
        _freeze(cache, mounts)


def test_hard_link_refuses_the_freeze(tmp_path: Path) -> None:
    cache, mounts = _cache(tmp_path)
    os.link(cache / "bin" / "ruby", cache / "bin" / "ruby2")
    with pytest.raises(ProvenanceRefusedError, match="link"):
        _freeze(cache, mounts)


def test_fifo_refuses_the_freeze(tmp_path: Path) -> None:
    cache, mounts = _cache(tmp_path)
    os.mkfifo(cache / "bin" / "pipe")
    with pytest.raises(ProvenanceRefusedError, match="regular"):
        _freeze(cache, mounts)


def test_entry_bound_is_enforced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cache, mounts = _cache(tmp_path)
    monkeypatch.setattr(env_provenance, "PROVENANCE_MAX_ENTRIES", 3)
    with pytest.raises(ProvenanceRefusedError, match="entries"):
        _freeze(cache, mounts)


def test_byte_bound_is_enforced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cache, mounts = _cache(tmp_path)
    monkeypatch.setattr(env_provenance, "PROVENANCE_MAX_BYTES", 4)
    with pytest.raises(ProvenanceRefusedError, match="bytes"):
        _freeze(cache, mounts)


def test_oversized_script_refuses_the_freeze(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cache, mounts = _cache(tmp_path)
    (cache / "services").mkdir()
    (cache / "services" / "services-start.sh").write_text("x" * 200)
    # Above the rendered activate.sh (about 60 bytes), below the planted script.
    monkeypatch.setattr(env_provenance, "PROVENANCE_MAX_SCRIPT_BYTES", 100)
    with pytest.raises(ProvenanceRefusedError, match="services-start"):
        _freeze(cache, mounts)


def test_manifest_round_trips_and_is_trusted_only_by_digest(tmp_path: Path) -> None:
    cache, mounts = _cache(tmp_path)
    manifest = _freeze(cache, mounts)
    daemon_dir = tmp_path / "state" / "env-provenance"
    path = daemon_dir / "sym-abc.json"
    digest = save_manifest(path, manifest, performer_mounts=mounts)
    loaded = load_manifest(path, digest=digest, performer_mounts=mounts)
    assert loaded is not None
    assert loaded.activate_sh == ACTIVATE
    assert [e.path for e in loaded.entries] == [e.path for e in manifest.entries]
    # A manifest that no longer hashes to the recorded digest is not trusted.
    path.write_text(path.read_text().replace("abc123", "abc124"))
    assert load_manifest(path, digest=digest, performer_mounts=mounts) is None


def test_manifest_inside_a_performer_mount_is_refused(tmp_path: Path) -> None:
    cache, mounts = _cache(tmp_path)
    manifest = _freeze(cache, mounts)
    # The state directory sits under the env-cache root, which is a performer mount.
    path = mounts[0] / "state" / "sym-abc.json"
    with pytest.raises(ProvenanceRefusedError, match="performer mount"):
        save_manifest(path, manifest, performer_mounts=mounts)
    assert load_manifest(path, digest="0" * 64, performer_mounts=mounts) is None
