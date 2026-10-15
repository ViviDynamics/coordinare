"""Spec 234 second review: manifest cap, background full pass, minor race and attribution fixes.

- The manifest cap is derived from ``max_entries`` with a per-entry budget, so a cache at the default bound fits.
- A refusal names ``env_cache.provenance_max_entries``, and a byte-bound refusal names the byte knob even when a
  path contains the word "entries".
- The hourly full pass runs in the background. Dispatch does the stat pass only, unless the first pass of a process
  or a stat mismatch needs the hash, and a drift found by the background pass refuses dispatch.
- The digest is re-read after the payload build, and a full-pass clock is set only for the digest that was checked.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from coordinare.services import env_provenance
from coordinare.services.env_cache import EnvCacheService
from coordinare.services.env_provenance import (
    PROVENANCE_MAX_PATH_BYTES,
    ProvenanceEntry,
    ProvenanceLimits,
    ProvenanceManifest,
    ProvenanceRefusedError,
    ProvenanceScope,
    freeze_cache,
    manifest_cap_bytes,
    save_manifest,
)
from tests.unit.env_mount_stub import mount_config
from tests.unit.test_234_honest_path import ACTIVATE, _bootstrapped, _state_map


def _service(tmp_path: Path, **env_cache: int) -> EnvCacheService:
    extra = {"coordinare_manages_services": False, **env_cache}
    return EnvCacheService(mount_config(
        env_cache_root=str(tmp_path / "caches"),
        state_file_path=str(tmp_path / "state" / "coordinare.state.json"),
        env_cache=SimpleNamespace(**extra),
    ))


def test_the_manifest_cap_is_derived_from_the_entry_bound() -> None:
    assert manifest_cap_bytes(1_000_000) == 1_000_000 * (PROVENANCE_MAX_PATH_BYTES + 256)
    assert manifest_cap_bytes(2_000) > manifest_cap_bytes(1_000)


def test_a_cache_at_the_default_bound_with_realistic_paths_is_saved(tmp_path: Path) -> None:
    """1 000 000 entries with paths of about 110 characters, the reviewer's measured worst realistic case."""
    entries = []
    for i in range(1_000_000):
        path = f"node_modules/some-package-name/lib/deep/dir/{'x' * 50}/file{i:07d}.js"
        entries.append(ProvenanceEntry(
            path=path, kind="file", mode=0o644, size=12345, ino=123456789 + i,
            ctime_ns=1728000000000000000 + i, sha256=hashlib.sha256(path.encode()).hexdigest(),
        ))
    manifest = ProvenanceManifest(
        slug="sym-abcdef", symphony="s", cache_key="k", frozen_at=datetime.now(UTC),
        activate_sh="x", entries=entries,
    )
    digest = save_manifest(
        tmp_path / "state" / "big.json", manifest, performer_mounts=(tmp_path / "caches",),
        max_bytes=manifest_cap_bytes(env_provenance.DEFAULT_LIMITS.max_entries),
    )
    assert len(digest) == 64


def test_a_manifest_over_its_cap_names_the_entry_knob(tmp_path: Path) -> None:
    manifest = ProvenanceManifest(
        slug="s", symphony="s", cache_key="k", frozen_at=datetime.now(UTC), activate_sh="x", entries=[],
    )
    with pytest.raises(ProvenanceRefusedError, match="provenance_max_entries"):
        save_manifest(tmp_path / "state" / "m.json", manifest, performer_mounts=(tmp_path / "c",), max_bytes=10)


def test_a_path_longer_than_the_per_entry_budget_is_refused_at_freeze(tmp_path: Path) -> None:
    root = tmp_path / "caches"
    cache = root / "sym-abc"
    # Three 200-byte directories make a 600-byte relative path, over the 512-byte budget, and each name fits a file system.
    deep = cache / ("d" * 200) / ("e" * 200) / ("f" * 200)
    deep.mkdir(parents=True)
    (deep / "file.js").write_text("x")
    assert len(f"{'d' * 200}/{'e' * 200}/{'f' * 200}/file.js".encode()) > PROVENANCE_MAX_PATH_BYTES
    (cache / "activate.sh").write_text(ACTIVATE)
    with pytest.raises(ProvenanceRefusedError, match="path longer"):
        freeze_cache(cache, symphony="s", cache_key="k", activate_sh=ACTIVATE, scope=ProvenanceScope((root,)))


def test_a_byte_refusal_names_the_byte_knob_even_when_a_path_says_entries(tmp_path: Path) -> None:
    state_ec, cache = _bootstrapped(tmp_path)
    (cache / "entries-report.bin").write_bytes(b"0123456789")
    service = _service(tmp_path, provenance_max_entries=1_000_000, provenance_max_bytes=4)
    ok, reason = asyncio.run(service.freeze_provenance("sym", _state_map(state_ec)))
    assert ok is False
    assert "provenance_max_bytes" in (reason or "")
    assert "provenance_max_entries" not in (reason or "")


def test_dispatch_does_not_run_the_hourly_hash_pass_inline(tmp_path: Path) -> None:
    state_ec, _cache = _bootstrapped(tmp_path)
    service = _service(tmp_path)
    asyncio.run(service.freeze_provenance("sym", _state_map(state_ec)))
    # The first pass of the process is full (it rebuilds the baseline). Then the clock is due.
    first_check, _ = asyncio.run(service.resolve_provenance("sym", state_ec, container_path="/devenv/sym-abc"))
    assert first_check.hashed
    service._provenance_full_at["sym"] = time.monotonic() - 2 * 3600
    check, payload = asyncio.run(service.resolve_provenance("sym", state_ec, container_path="/devenv/sym-abc"))
    assert check.ok and payload is not None
    assert check.hashed is False, "the hourly pass is not run on the dispatch path"


def test_the_background_pass_finds_drift_and_the_next_dispatch_refuses(tmp_path: Path) -> None:
    state_ec, cache = _bootstrapped(tmp_path)
    service = _service(tmp_path)
    asyncio.run(service.freeze_provenance("sym", _state_map(state_ec)))
    asyncio.run(service.resolve_provenance("sym", state_ec, container_path="/devenv/sym-abc"))
    # A same-size rewrite with ctime restored is the case only the hash pass can see.
    tool = cache / "bin" / "ruby"
    st = tool.stat()
    tool.write_bytes(tool.read_bytes().replace(b"ruby", b"RUBY"))  # same size, different bytes
    os.utime(tool, ns=(st.st_atime_ns, st.st_mtime_ns))
    asyncio.run(service.run_full_pass("sym", state_ec))
    check, payload = asyncio.run(service.resolve_provenance("sym", state_ec, container_path="/devenv/sym-abc"))
    assert check.ok is False
    assert payload is None
    assert "ruby" in (check.reason or "")


def test_a_refreshed_freeze_clears_a_recorded_drift(tmp_path: Path) -> None:
    state_ec, cache = _bootstrapped(tmp_path)
    service = _service(tmp_path)
    asyncio.run(service.freeze_provenance("sym", _state_map(state_ec)))
    (cache / "bin" / "planted").write_text("x")
    asyncio.run(service.run_full_pass("sym", state_ec))
    (cache / "bin" / "planted").unlink()
    asyncio.run(service.freeze_provenance("sym", _state_map(state_ec)))
    check, _payload = asyncio.run(service.resolve_provenance("sym", state_ec, container_path="/devenv/sym-abc"))
    assert check.ok


def test_a_digest_change_during_the_payload_build_refuses(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state_ec, _cache = _bootstrapped(tmp_path)
    service = _service(tmp_path)
    asyncio.run(service.freeze_provenance("sym", _state_map(state_ec)))
    original = service._payload_for

    async def _racing(*args: Any, **kwargs: Any) -> Any:
        result = await original(*args, **kwargs)
        state_ec.provenance_digest = "f" * 64  # a regeneration clears or replaces the digest mid-await
        return result

    monkeypatch.setattr(service, "_payload_for", _racing)
    check, payload = asyncio.run(service.resolve_provenance("sym", state_ec, container_path="/devenv/sym-abc"))
    assert check.ok is False
    assert payload is None


def test_the_full_clock_is_not_set_for_a_digest_that_changed(tmp_path: Path) -> None:
    state_ec, _cache = _bootstrapped(tmp_path)
    service = _service(tmp_path)
    asyncio.run(service.freeze_provenance("sym", _state_map(state_ec)))
    service._provenance_full_at.pop("sym", None)
    manifest = service._provenance["sym"][1]
    original_digest = state_ec.provenance_digest
    assert original_digest is not None

    async def _change_then_check() -> None:
        # The digest changes between the start of the check and the moment the clock is written.
        task = asyncio.create_task(service._check_manifest("sym", state_ec, original_digest, manifest))
        await asyncio.sleep(0)
        state_ec.provenance_digest = "e" * 64
        await task

    asyncio.run(_change_then_check())
    assert "sym" not in service._provenance_full_at


def test_scope_reads_limits_from_the_scope_value() -> None:
    scope = ProvenanceScope((Path("/tmp/x"),), ProvenanceLimits(max_entries=5, max_bytes=6))
    assert scope.limits.max_entries == 5
