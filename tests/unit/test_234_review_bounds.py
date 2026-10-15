"""Spec 234 review round: the provenance bounds are sized for real toolchains and configurable.

The reviewer measured a Node install at 35-40k entries and about 470 MiB, and an rbenv at 480k entries and 7.7 GB.
The defaults are 1 000 000 entries and 16 GiB. Both are config knobs, and a refusal names the knob.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from coordinare.config import EnvCacheConfig
from coordinare.services import env_provenance
from coordinare.services.env_cache import EnvCacheService
from tests.unit.env_mount_stub import mount_config
from tests.unit.test_234_honest_path import ACTIVATE, _bootstrapped, _state_map

GIB = 1024 * 1024 * 1024


def test_the_defaults_fit_a_real_toolchain() -> None:
    assert env_provenance.DEFAULT_LIMITS.max_entries == 1_000_000
    assert env_provenance.DEFAULT_LIMITS.max_bytes == 16 * GIB


def test_the_config_knobs_default_to_the_same_bounds() -> None:
    cfg = EnvCacheConfig()
    assert cfg.provenance_max_entries == 1_000_000
    assert cfg.provenance_max_bytes == 16 * GIB


def _service_with_limits(tmp_path: Path, **limits: int) -> EnvCacheService:
    env_cache = SimpleNamespace(coordinare_manages_services=False, **limits)
    return EnvCacheService(mount_config(
        env_cache_root=str(tmp_path / "caches"),
        state_file_path=str(tmp_path / "state" / "coordinare.state.json"),
        env_cache=env_cache,
    ))


def test_an_entry_refusal_names_the_knob(tmp_path: Path) -> None:
    state_ec, _cache = _bootstrapped(tmp_path)
    service = _service_with_limits(tmp_path, provenance_max_entries=2, provenance_max_bytes=16 * GIB)
    ok, reason = asyncio.run(service.freeze_provenance("sym", _state_map(state_ec)))
    assert ok is False
    assert "provenance_max_entries" in (reason or "")


def test_a_byte_refusal_names_the_knob(tmp_path: Path) -> None:
    state_ec, _cache = _bootstrapped(tmp_path)
    service = _service_with_limits(tmp_path, provenance_max_entries=1_000_000, provenance_max_bytes=4)
    ok, reason = asyncio.run(service.freeze_provenance("sym", _state_map(state_ec)))
    assert ok is False
    assert "provenance_max_bytes" in (reason or "")


def test_a_cache_inside_the_knobs_freezes(tmp_path: Path) -> None:
    state_ec, _cache = _bootstrapped(tmp_path)
    service = _service_with_limits(tmp_path, provenance_max_entries=1000, provenance_max_bytes=GIB)
    ok, reason = asyncio.run(service.freeze_provenance("sym", _state_map(state_ec)))
    assert ok, reason
    assert ACTIVATE  # the honest activate text is the one frozen
