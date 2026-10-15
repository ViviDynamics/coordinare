"""Spec 234 review round: ctime-only churn does not force a full re-hash on every dispatch.

A passing full pass records the stat baseline it observed, so the next stat pass is clean. A later content change
is still caught, because the baseline records the stat values of the bytes the full pass hashed. The payload is
built once per manifest and reused, so a dispatch does not re-serialise the manifest on the event loop.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path

from coordinare.services.env_cache import EnvCacheService
from coordinare.services.env_provenance import ProvenanceScope, check_cache, freeze_cache
from tests.unit.env_mount_stub import mount_config
from tests.unit.test_234_honest_path import ACTIVATE, _bootstrapped, _state_map


def _tree(tmp_path: Path) -> tuple[Path, tuple[Path, ...]]:
    root = tmp_path / "caches"
    cache = root / "sym-abc"
    (cache / "bin").mkdir(parents=True)
    (cache / "bin" / "tool").write_text("#!/bin/sh\necho ok\n")
    os.chmod(cache / "bin" / "tool", 0o755)  # the mode the freeze records, so a later chmod is ctime-only churn
    (cache / "activate.sh").write_text(ACTIVATE)
    return cache, (root,)


def test_a_ctime_bump_with_identical_bytes_is_hashed_once_then_not_again(tmp_path: Path) -> None:
    cache, mounts = _tree(tmp_path)
    manifest = freeze_cache(cache, symphony="sym", cache_key="k", activate_sh=ACTIVATE, scope=ProvenanceScope(mounts))
    tool = cache / "bin" / "tool"
    os.chmod(tool, 0o755)  # ctime moves, the bytes do not

    first = check_cache(cache, manifest, full=False, scope=ProvenanceScope(mounts))
    assert first.ok and first.hashed, "the stat mismatch triggers one full hash"
    assert first.stats is not None

    second = check_cache(cache, manifest, full=False, scope=ProvenanceScope(mounts), baseline=first.stats)
    assert second.ok
    assert second.hashed is False, "the refreshed baseline makes the next pass stat-only"


def test_a_content_change_after_the_baseline_is_still_refused(tmp_path: Path) -> None:
    cache, mounts = _tree(tmp_path)
    manifest = freeze_cache(cache, symphony="sym", cache_key="k", activate_sh=ACTIVATE, scope=ProvenanceScope(mounts))
    tool = cache / "bin" / "tool"
    os.chmod(tool, 0o755)
    baseline = check_cache(cache, manifest, full=False, scope=ProvenanceScope(mounts)).stats
    tool.write_bytes(b"#!/bin/sh\necho no\n")  # a rewrite after the baseline
    after = check_cache(cache, manifest, full=False, scope=ProvenanceScope(mounts), baseline=baseline)
    assert after.ok is False
    assert "tool" in (after.reason or "")


def test_the_service_stops_hashing_after_one_churn_pass(tmp_path: Path) -> None:
    state_ec, cache = _bootstrapped(tmp_path)
    service = EnvCacheService(mount_config(
        env_cache_root=str(tmp_path / "caches"),
        state_file_path=str(tmp_path / "state" / "coordinare.state.json"),
    ))
    asyncio.run(service.freeze_provenance("sym", _state_map(state_ec)))
    os.chmod(cache / "bin" / "ruby", 0o755)  # ctime-only churn
    first = asyncio.run(service.check_provenance("sym", state_ec))
    assert first.ok and first.hashed
    second = asyncio.run(service.check_provenance("sym", state_ec))
    assert second.ok
    assert second.hashed is False


def test_the_payload_is_built_once_per_manifest_and_matches_the_digest(tmp_path: Path) -> None:
    state_ec, _cache = _bootstrapped(tmp_path)
    service = EnvCacheService(mount_config(
        env_cache_root=str(tmp_path / "caches"),
        state_file_path=str(tmp_path / "state" / "coordinare.state.json"),
    ))
    asyncio.run(service.freeze_provenance("sym", _state_map(state_ec)))
    check1, payload1 = asyncio.run(service.resolve_provenance("sym", state_ec, container_path="/devenv/sym-abc"))
    check2, payload2 = asyncio.run(service.resolve_provenance("sym", state_ec, container_path="/devenv/sym-abc"))
    assert check1.ok and check2.ok
    assert payload1 is payload2, "the second dispatch reuses the serialised payload"
    assert payload1 is not None and payload1["digest"] == state_ec.provenance_digest


def test_resolve_refuses_when_nothing_is_frozen(tmp_path: Path) -> None:
    state_ec, _cache = _bootstrapped(tmp_path)
    service = EnvCacheService(mount_config(
        env_cache_root=str(tmp_path / "caches"),
        state_file_path=str(tmp_path / "state" / "coordinare.state.json"),
    ))
    check, payload = asyncio.run(service.resolve_provenance("sym", state_ec, container_path="/devenv/sym-abc"))
    assert check.ok is False
    assert payload is None
