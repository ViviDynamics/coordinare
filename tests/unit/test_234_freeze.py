"""Spec 234: freezing the env cache into a streamed manifest file.

The freeze is the daemon's only full read of a cache the bootstrap wrote. These tests pin what it records, what it
refuses (links that leave the cache, hard links, special files, over-bound caches), that the manifest is trusted only
by its digest, and that the stat digest is O(1) to compute and order-independent.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from coordinare.services import env_provenance
from coordinare.services.env_provenance import (
    ProvenanceLimits,
    ProvenanceRefusedError,
    ProvenanceScope,
    freeze_cache,
    open_manifest,
    stat_digest_of,
    verify_content,
)

ACTIVATE = "# daemon-rendered activate\nexport PATH=$DEVENV/bin:$PATH\n"
GIB = 1024 * 1024 * 1024


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


def _freeze(
    cache: Path,
    mounts: tuple[Path, ...],
    out: Path,
    scope: ProvenanceScope | None = None,
) -> env_provenance.FreezeResult:
    return freeze_cache(
        cache,
        identity=("sym", "abc123"),
        activate_sh=ACTIVATE,
        scope=scope or ProvenanceScope(mounts),
        out_path=out,
    )


def _out(tmp_path: Path) -> Path:
    state = tmp_path / "state" / "env-provenance"
    state.mkdir(parents=True, exist_ok=True)
    return state / "sym-abc.json"


def test_honest_freeze_records_the_meta_and_every_row(tmp_path: Path) -> None:
    cache, mounts = _cache(tmp_path)
    result = _freeze(cache, mounts, _out(tmp_path))
    assert result.manifest.meta.activate_sh == ACTIVATE
    assert result.manifest.meta.services_start_sh is None
    assert result.entries == 5  # bin, bin/ruby, activate.sh, manifest.json, verify.sh
    assert result.manifest.path.exists()


def test_freeze_carries_service_scripts_as_frozen_bytes(tmp_path: Path) -> None:
    cache, mounts = _cache(tmp_path)
    (cache / "services").mkdir()
    (cache / "services" / "services-start.sh").write_text("echo started\n")
    (cache / "services" / "services-health.sh").write_text("echo healthy\n")
    result = _freeze(cache, mounts, _out(tmp_path))
    assert result.manifest.meta.services_start_sh == "echo started\n"
    assert result.manifest.meta.services_health_sh == "echo healthy\n"


def test_debs_are_listed_by_digest_in_the_metadata(tmp_path: Path) -> None:
    cache, mounts = _cache(tmp_path)
    (cache / "debs").mkdir()
    (cache / "debs" / "libfoo.deb").write_bytes(b"deb-bytes")
    result = _freeze(cache, mounts, _out(tmp_path))
    assert set(result.manifest.meta.debs) == {"debs/libfoo.deb"}
    assert len(result.manifest.meta.debs["debs/libfoo.deb"]) == 64


def test_relative_link_that_stays_inside_the_cache_is_accepted(tmp_path: Path) -> None:
    cache, mounts = _cache(tmp_path)
    (cache / "lib").mkdir()
    (cache / "lib" / "libssl.so.3").write_text("x")
    os.symlink("libssl.so.3", cache / "lib" / "libssl.so")
    _freeze(cache, mounts, _out(tmp_path))


def test_absolute_link_refuses_the_freeze(tmp_path: Path) -> None:
    cache, mounts = _cache(tmp_path)
    os.symlink("/bin/sh", cache / "bin" / "sh")
    with pytest.raises(ProvenanceRefusedError, match="link"):
        _freeze(cache, mounts, _out(tmp_path))


def test_link_that_climbs_out_of_the_cache_refuses_the_freeze(tmp_path: Path) -> None:
    cache, mounts = _cache(tmp_path)
    os.symlink("../../../etc", cache / "bin" / "escape")
    with pytest.raises(ProvenanceRefusedError, match="link"):
        _freeze(cache, mounts, _out(tmp_path))


def test_hard_link_refuses_the_freeze(tmp_path: Path) -> None:
    cache, mounts = _cache(tmp_path)
    os.link(cache / "bin" / "ruby", cache / "bin" / "ruby2")
    with pytest.raises(ProvenanceRefusedError, match="link"):
        _freeze(cache, mounts, _out(tmp_path))


def test_fifo_refuses_the_freeze(tmp_path: Path) -> None:
    cache, mounts = _cache(tmp_path)
    os.mkfifo(cache / "bin" / "pipe")
    with pytest.raises(ProvenanceRefusedError, match="regular"):
        _freeze(cache, mounts, _out(tmp_path))


def test_entry_bound_is_enforced_and_named(tmp_path: Path) -> None:
    cache, mounts = _cache(tmp_path)
    scope = ProvenanceScope(mounts, ProvenanceLimits(max_entries=3, max_bytes=GIB))
    with pytest.raises(ProvenanceRefusedError, match="provenance_max_entries"):
        _freeze(cache, mounts, _out(tmp_path), scope=scope)


def test_byte_bound_is_enforced_and_named(tmp_path: Path) -> None:
    cache, mounts = _cache(tmp_path)
    scope = ProvenanceScope(mounts, ProvenanceLimits(max_entries=1000, max_bytes=4))
    with pytest.raises(ProvenanceRefusedError, match="provenance_max_bytes"):
        _freeze(cache, mounts, _out(tmp_path), scope=scope)


def test_oversized_script_refuses_the_freeze(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    cache, mounts = _cache(tmp_path)
    (cache / "services").mkdir()
    (cache / "services" / "services-start.sh").write_text("x" * 200)
    # Above the rendered activate.sh (about 60 bytes), below the planted script.
    monkeypatch.setattr(env_provenance, "PROVENANCE_MAX_SCRIPT_BYTES", 100)
    with pytest.raises(ProvenanceRefusedError, match="services-start"):
        _freeze(cache, mounts, _out(tmp_path))


def test_manifest_is_trusted_only_by_its_digest(tmp_path: Path) -> None:
    cache, mounts = _cache(tmp_path)
    out = _out(tmp_path)
    result = _freeze(cache, mounts, out)
    opened = open_manifest(out, digest=result.manifest.digest, performer_mounts=mounts, max_bytes=10**9)
    assert opened is not None
    assert opened.meta.activate_sh == ACTIVATE
    out.write_text(out.read_text().replace("abc123", "abc124"))
    assert open_manifest(out, digest=result.manifest.digest, performer_mounts=mounts, max_bytes=10**9) is None


def test_manifest_inside_a_performer_mount_is_refused(tmp_path: Path) -> None:
    cache, mounts = _cache(tmp_path)
    inside = mounts[0] / "state" / "sym-abc.json"
    with pytest.raises(ProvenanceRefusedError, match="performer mount"):
        _freeze(cache, mounts, inside)


def test_the_stat_digest_is_order_independent_and_changes_with_ctime(tmp_path: Path) -> None:
    cache, mounts = _cache(tmp_path)
    scope = ProvenanceScope(mounts)
    first = stat_digest_of(cache, scope=scope)
    assert stat_digest_of(cache, scope=scope) == first, "the same tree gives the same digest"
    os.chmod(cache / "bin" / "ruby", 0o755)  # ctime moves
    assert stat_digest_of(cache, scope=scope) != first


def test_an_honest_tree_verifies_its_own_content(tmp_path: Path) -> None:
    cache, mounts = _cache(tmp_path)
    scope = ProvenanceScope(mounts)
    result = _freeze(cache, mounts, _out(tmp_path))
    manifest = open_manifest(result.manifest.path, digest=result.manifest.digest, performer_mounts=mounts, max_bytes=10**9)
    assert manifest is not None
    content = verify_content(cache, manifest, scope=scope)
    assert content.ok, content.reason
    assert content.stat_digest == result.stat_digest
