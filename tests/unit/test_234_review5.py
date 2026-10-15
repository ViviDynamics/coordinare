"""Spec 234 review round 5: coalesced walks cannot pass on an older state, hung walks time out, transient failures are
not drift, and an unreadable directory is reported for its cause.

- A caller joins a walk in flight only if the walk started at or after the caller arrived. The window reuse is bounded by
  the walk's start time, not its completion time.
- A walk that outlives its timeout refuses the dispatches waiting on it, clears the in-flight entry, and the next dispatch
  starts fresh.
- An I/O failure in the content check is retried once and never recorded as drift. A mismatch still is.
"""

from __future__ import annotations

import asyncio
import errno
import os
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from coordinare.config import EnvCacheConfig
from coordinare.models.env_cache import EnvCacheState
from coordinare.services import env_cache
from coordinare.services.env_cache import EnvCacheService
from coordinare.services.env_provenance import (
    ProvenanceLimits,
    ProvenanceRefusedError,
    ProvenanceScope,
    freeze_cache,
)
from tests.unit.env_mount_stub import mount_config
from tests.unit.test_234_honest_path import ACTIVATE, _bootstrapped, _state_map

CONTAINER = "/devenv/sym-abc"


def _service(tmp_path: Path, **env_cache_knobs: float) -> EnvCacheService:
    env = SimpleNamespace(coordinare_manages_services=False, **env_cache_knobs)
    return EnvCacheService(mount_config(
        env_cache_root=str(tmp_path / "caches"),
        state_file_path=str(tmp_path / "state" / "coordinare.state.json"),
        env_cache=env,
    ))


def _frozen(tmp_path: Path, **knobs: float) -> tuple[EnvCacheService, EnvCacheState, Path]:
    state, cache = _bootstrapped(tmp_path)
    service = _service(tmp_path, **knobs)
    asyncio.run(service.freeze_provenance("sym", _state_map(state)))
    # The first dispatch of a process runs the content check, which records the stat digest and the full clock.
    asyncio.run(service.resolve_provenance("sym", state, container_path=CONTAINER))
    return service, state, cache


def test_the_reviewers_join_scenario_refuses_the_dispatch_that_arrived_after_the_tamper(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The window is zero here, so this exercises the join rule alone. (With a window, a passing result is reused within
    # the window by design, and the staleness is bounded by it: see the window test below.)
    service, state, cache = _frozen(tmp_path, provenance_stat_fresh_s=0.0)
    real = env_cache.stat_digest_of

    def slow(cache_dir: Path, scope: ProvenanceScope) -> str:
        digest = real(cache_dir, scope)  # the walk observes the state at its start
        time.sleep(0.5)  # and returns late
        return digest

    monkeypatch.setattr(env_cache, "stat_digest_of", slow)

    async def scenario() -> tuple[Any, Any]:
        a = asyncio.create_task(service.resolve_provenance("sym", state, container_path=CONTAINER))
        await asyncio.sleep(0.1)  # A's walk has started
        tamper_at = time.monotonic()
        (cache / "bin" / "ruby").write_text("#!/bin/sh\necho EVIL!!\n")
        await asyncio.sleep(0.1)
        b = await service.resolve_provenance("sym", state, container_path=CONTAINER)  # arrives after the tamper
        a_result = await a
        assert service._stat_pass_at["sym"][1] < tamper_at, "A's pass is stamped with its walk's start"
        return b, a_result

    (b_check, b_payload), (a_check, _a_payload) = asyncio.run(scenario())
    assert b_check.ok is False, "B cannot join a walk that started before it arrived"
    assert b_payload is None
    assert a_check.ok, "A's walk observed the state before the tamper, so A's own result stands"


def test_the_reuse_window_is_bounded_from_the_walk_start(tmp_path: Path) -> None:
    service, state, cache = _frozen(tmp_path, provenance_stat_fresh_s=30.0)
    (cache / "bin" / "ruby").write_text("#!/bin/sh\necho EVIL!!\n")
    digest = state.provenance_digest
    assert digest is not None
    # Inside the window of the last passing walk's start, the reuse is the designed bound.
    service._stat_pass_at["sym"] = (digest, time.monotonic() - 10)
    reused, _ = asyncio.run(service.resolve_provenance("sym", state, container_path=CONTAINER))
    assert reused.ok and reused.hashed is False
    # Past the window measured from the walk's start, the dispatch walks, and the tamper is refused.
    service._stat_pass_at["sym"] = (digest, time.monotonic() - 31)
    refused, _ = asyncio.run(service.resolve_provenance("sym", state, container_path=CONTAINER))
    assert refused.ok is False


def test_a_hung_walk_refuses_its_dispatch_and_the_next_one_starts_fresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, state, _cache = _frozen(tmp_path, provenance_stat_fresh_s=0.0)
    monkeypatch.setattr(EnvCacheService, "_stat_walk_timeout", lambda self: 0.2)
    real = env_cache.stat_digest_of
    hang = {"on": True}

    def maybe_hung(cache_dir: Path, scope: ProvenanceScope) -> str:
        if hang["on"]:
            time.sleep(1.0)
        return real(cache_dir, scope)

    monkeypatch.setattr(env_cache, "stat_digest_of", maybe_hung)
    hung, _ = asyncio.run(service.resolve_provenance("sym", state, container_path=CONTAINER))
    assert hung.ok is False and hung.transient
    assert "did not finish within" in (hung.reason or "")
    assert "sym" not in service._drift, "a timeout is not drift"
    assert "sym" not in service._stat_inflight, "the in-flight entry is cleared on timeout"
    hang["on"] = False
    fresh, _ = asyncio.run(service.resolve_provenance("sym", state, container_path=CONTAINER))
    assert fresh.ok, fresh.reason


def test_forgetting_a_manifest_clears_the_in_flight_walk(tmp_path: Path) -> None:
    service, _state, _cache = _frozen(tmp_path)

    async def park() -> None:
        loop = asyncio.get_running_loop()
        service._stat_inflight["sym"] = (time.monotonic(), loop.create_future())

    asyncio.run(park())
    service._forget_derived("sym")
    assert "sym" not in service._stat_inflight


def test_a_transient_io_failure_is_retried_once_and_is_not_drift(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service, state, _cache = _frozen(tmp_path)
    real = env_cache.content_pass
    calls = {"n": 0}

    def flaky(*args: Any) -> Any:
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError(errno.EIO, "Input/output error")
        return real(*args)

    service._drift.clear()
    service._provenance_full_at.pop("sym", None)  # force the content check
    monkeypatch.setattr(env_cache, "content_pass", flaky)
    check, _payload = asyncio.run(service.resolve_provenance("sym", state, container_path=CONTAINER))
    assert check.ok, check.reason
    assert calls["n"] == 2, "the transient failure was retried once"


def test_a_persistent_io_failure_refuses_only_that_dispatch(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service, state, _cache = _frozen(tmp_path)
    real = env_cache.content_pass

    def broken(*args: Any) -> Any:
        raise OSError(errno.EIO, "Input/output error")

    service._provenance_full_at.pop("sym", None)
    monkeypatch.setattr(env_cache, "content_pass", broken)
    check, payload = asyncio.run(service.resolve_provenance("sym", state, container_path=CONTAINER))
    assert check.ok is False and check.transient
    assert payload is None
    assert "sym" not in service._drift, "an I/O failure is not recorded as drift"
    monkeypatch.setattr(env_cache, "content_pass", real)
    again, _ = asyncio.run(service.resolve_provenance("sym", state, container_path=CONTAINER))
    assert again.ok, "the next dispatch, once the I/O works, passes"


def test_a_real_mismatch_is_still_drift(tmp_path: Path) -> None:
    service, state, cache = _frozen(tmp_path)
    (cache / "bin" / "planted").write_text("x")
    service._provenance_full_at.pop("sym", None)
    check, _payload = asyncio.run(service.resolve_provenance("sym", state, container_path=CONTAINER))
    assert check.ok is False and not check.transient
    assert "sym" in service._drift


def test_an_unreadable_directory_is_named_for_its_cause(tmp_path: Path) -> None:
    root = tmp_path / "caches"
    cache = root / "sym-abc"
    locked = cache / "locked"
    locked.mkdir(parents=True)
    (cache / "activate.sh").write_text(ACTIVATE)
    os.chmod(locked, 0)
    try:
        with pytest.raises(ProvenanceRefusedError, match="unreadable"):
            freeze_cache(
                cache, ("sym", "k"), ACTIVATE,
                ProvenanceScope((root,), ProvenanceLimits(max_entries=1000, max_bytes=10**9)),
                tmp_path / "state" / "m.json",
            )
    finally:
        os.chmod(locked, 0o755)


def test_the_stat_walk_timeout_knob_defaults_to_five_minutes_and_is_validated() -> None:
    assert EnvCacheConfig().provenance_stat_walk_timeout_s == 300.0
    assert EnvCacheConfig(provenance_stat_walk_timeout_s=10).provenance_stat_walk_timeout_s == 10.0
    with pytest.raises(ValidationError):
        EnvCacheConfig(provenance_stat_walk_timeout_s=9)
    with pytest.raises(ValidationError):
        EnvCacheConfig(provenance_stat_walk_timeout_s=3601)


def test_the_window_knob_is_read_from_config(tmp_path: Path) -> None:
    service = _service(tmp_path, provenance_stat_fresh_s=7.5)
    assert service._stat_fresh_seconds() == 7.5
