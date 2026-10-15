"""Spec 234: the honest path end to end with fakes.

A bootstrap writes a cache, the daemon verifies and freezes it, a consumer dispatch checks it, the
payload carries the frozen steps, and a restart re-reads the manifest by its digest. Nothing here
runs docker. The daemon's own decisions are the real code paths.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from coordinare.graph.nodes import dispatch_performer
from coordinare.models.env_cache import EnvCacheState
from coordinare.services.env_cache import EnvCacheService
from coordinare.state_store import CURRENT_SCHEMA_VERSION, EnvCacheStateSnapshot
from tests.unit.env_mount_stub import mount_config

ACTIVATE = "# daemon-rendered activate\nexport PATH=$DEVENV/bin:$PATH\n"


def _service(tmp_path: Path) -> EnvCacheService:
    config = mount_config(
        env_cache_root=str(tmp_path / "caches"),
        state_file_path=str(tmp_path / "state" / "coordinare.state.json"),
    )
    return EnvCacheService(config)


def _bootstrapped(tmp_path: Path) -> tuple[EnvCacheState, Path]:
    cache = tmp_path / "caches" / "sym-abc"
    (cache / "bin").mkdir(parents=True)
    (cache / "bin" / "ruby").write_text("#!/bin/sh\necho ruby 3.4.2\n")
    (cache / "bin" / "ruby").chmod(0o755)
    (cache / "activate.sh").write_text(ACTIVATE)
    (cache / "debs").mkdir()
    (cache / "debs" / "libicu.deb").write_bytes(b"deb")
    state = EnvCacheState(
        symphony_name="sym",
        sanitised_name="sym-abc",
        cache_dir=cache,
        readme_sha="spec-sha",
        rendered_activate_sh=ACTIVATE,
        last_bootstrap_succeeded=True,
        cache_dir_ready=True,
        last_seen_spec_sha="spec-sha",
    )
    return state, cache


def _state_map(state: EnvCacheState) -> dict[str, Any]:
    return {"env_cache": {"sym": state}}


def test_freeze_then_check_then_dispatch_passes(tmp_path: Path) -> None:
    state, _cache = _bootstrapped(tmp_path)
    service = _service(tmp_path)

    ok, reason = asyncio.run(service.freeze_provenance("sym", _state_map(state)))
    assert ok, reason
    assert state.provenance_digest is not None
    assert state.provenance_frozen_at is not None

    check = asyncio.run(service.check_provenance("sym", state))
    assert check.ok, check.reason

    released: list[bool] = []
    terminal, payload = asyncio.run(
        dispatch_performer._env_provenance_gate(
            {"env_cache_service": service}, "card-1", "qa", "sym", state, False,
            lambda: released.append(True), container_path="/devenv/sym-abc",
        ),
    )
    assert terminal is None
    assert payload is not None and payload["slug"] == "sym-abc"
    assert released == []


def test_the_payload_carries_the_rendered_steps_and_the_frozen_debs(tmp_path: Path) -> None:
    state, cache = _bootstrapped(tmp_path)
    service = _service(tmp_path)
    asyncio.run(service.freeze_provenance("sym", _state_map(state)))

    check, payload = asyncio.run(service.resolve_provenance("sym", state, container_path="/devenv/sym-abc"))
    assert check.ok
    assert payload is not None
    assert payload["slug"] == "sym-abc"
    assert payload["cache_path"] == "/devenv/sym-abc"
    assert payload["activate_sh"] == ACTIVATE
    assert payload["services_start_sh"] is None
    assert set(payload["debs"]) == {"debs/libicu.deb"}
    assert payload["digest"] == state.provenance_digest
    assert cache.exists()


def test_a_restart_rereads_the_manifest_by_digest_and_still_passes(tmp_path: Path) -> None:
    state, _cache = _bootstrapped(tmp_path)
    asyncio.run(_service(tmp_path).freeze_provenance("sym", _state_map(state)))

    restarted = _service(tmp_path)
    check = asyncio.run(restarted.check_provenance("sym", state))
    assert check.ok, check.reason
    _check, payload = asyncio.run(restarted.resolve_provenance("sym", state, container_path="/devenv/sym-abc"))
    assert payload is not None


def test_a_tampered_manifest_file_is_not_trusted_after_restart(tmp_path: Path) -> None:
    state, _cache = _bootstrapped(tmp_path)
    asyncio.run(_service(tmp_path).freeze_provenance("sym", _state_map(state)))
    manifest_path = tmp_path / "state" / "env-provenance" / "sym-abc.json"
    manifest_path.write_text(manifest_path.read_text().replace(ACTIVATE[:6], "PWNED!"))

    restarted = _service(tmp_path)
    check = asyncio.run(restarted.check_provenance("sym", state))
    assert check.ok is False
    _check, payload = asyncio.run(restarted.resolve_provenance("sym", state, container_path="/devenv/sym-abc"))
    assert payload is None


def test_a_cache_without_frozen_provenance_is_refused(tmp_path: Path) -> None:
    state, _cache = _bootstrapped(tmp_path)
    service = _service(tmp_path)
    # A v36 snapshot has no digest. The first consumer dispatch must not run it.
    check = asyncio.run(service.check_provenance("sym", state))
    assert check.ok is False
    assert "no frozen provenance" in (check.reason or "")


def test_snapshot_carries_the_digest_and_v36_loads_without_it() -> None:
    assert CURRENT_SCHEMA_VERSION == 37
    snap = EnvCacheStateSnapshot(
        symphony_name="sym",
        sanitised_name="sym-abc",
        cache_dir="/x",
        provenance_digest="a" * 64,
    )
    assert snap.provenance_digest == "a" * 64
    old = EnvCacheStateSnapshot(symphony_name="sym", sanitised_name="sym-abc", cache_dir="/x")
    assert old.provenance_digest is None
    assert old.provenance_frozen_at is None
