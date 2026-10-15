"""Spec 234 review round 4: the stat digest, shared and windowed stat passes, and the worker process.

- The stat digest is one sha256 over the records in walk order. Reordering, swapping or dropping a record changes it.
- Concurrent dispatches for one symphony share one walk in flight. A dispatch inside the freshness window causes no walk.
  A dispatch after the window walks again.
- The freshness window is a config knob: default 30 seconds, validated to 0 to 3600.
- The stat pass runs off the event loop, in a thread, and a dispatch works end to end through it.
"""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from coordinare.config import EnvCacheConfig
from coordinare.services import env_cache
from coordinare.services.env_cache import EnvCacheService
from coordinare.services.env_provenance import ProvenanceScope, _StatDigest, stat_digest_of
from coordinare.services.mounted_file import TreeEntry
from tests.unit.env_mount_stub import mount_config
from tests.unit.test_234_honest_path import _bootstrapped, _state_map


def _record(rel: str, ino: int = 1) -> TreeEntry:
    return TreeEntry(rel, "file", 0o644, 3, ino, 1_700_000_000_000_000_000, "ab" * 32)


def _digest(entries: list[TreeEntry]) -> str:
    stat = _StatDigest()
    for entry in entries:
        stat.add(entry)
    return stat.hexdigest()


def test_the_same_records_in_the_same_order_give_the_same_digest() -> None:
    records = [_record("a"), _record("b", 2)]
    assert _digest(records) == _digest(list(records))


def test_reordering_records_changes_the_digest() -> None:
    a, b = _record("a"), _record("b", 2)
    assert _digest([a, b]) != _digest([b, a])


def test_dropping_a_record_changes_the_digest() -> None:
    a, b = _record("a"), _record("b", 2)
    assert _digest([a, b]) != _digest([a])


def test_field_boundaries_cannot_be_moved_to_collide() -> None:
    # "ab" with target "c" and "a" with target "bc" carry the same characters in a different split.
    one = TreeEntry("ab", "link", 0o777, 1, 1, 1, None, "c")
    two = TreeEntry("a", "link", 0o777, 1, 1, 1, None, "bc")
    assert _digest([one]) != _digest([two])


def test_swapping_two_names_in_a_real_tree_changes_the_digest(tmp_path: Path) -> None:
    root = tmp_path / "caches"
    cache = root / "sym-abc"
    cache.mkdir(parents=True)
    (cache / "a").write_text("one")
    (cache / "b").write_text("two")
    scope = ProvenanceScope((root,))
    before = stat_digest_of(cache, scope)
    os.rename(cache / "a", cache / "tmp")
    os.rename(cache / "b", cache / "a")
    os.rename(cache / "tmp", cache / "b")
    assert stat_digest_of(cache, scope) != before


def test_the_freshness_window_defaults_to_thirty_seconds_and_is_validated() -> None:
    assert EnvCacheConfig().provenance_stat_fresh_s == 30.0
    assert EnvCacheConfig(provenance_stat_fresh_s=0).provenance_stat_fresh_s == 0.0
    with pytest.raises(ValidationError):
        EnvCacheConfig(provenance_stat_fresh_s=-1)
    with pytest.raises(ValidationError):
        EnvCacheConfig(provenance_stat_fresh_s=3601)


class _WalkCounter:
    """Counts the stat walks the service starts, by wrapping the walk it calls."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.calls = 0
        real = env_cache.stat_digest_of

        def counting(*args: Any, **kwargs: Any) -> str:
            self.calls += 1
            return real(*args, **kwargs)

        monkeypatch.setattr(env_cache, "stat_digest_of", counting)


def _service(tmp_path: Path, **kwargs: Any) -> EnvCacheService:
    return EnvCacheService(mount_config(
        env_cache_root=str(tmp_path / "caches"),
        state_file_path=str(tmp_path / "state" / "coordinare.state.json"),
    ), **kwargs)


def _frozen_and_checked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[EnvCacheService, Any, _WalkCounter]:
    state, _cache = _bootstrapped(tmp_path)
    service = _service(tmp_path)
    asyncio.run(service.freeze_provenance("sym", _state_map(state)))
    # The first dispatch of a process runs the content check, which records the stat digest and a passing check.
    asyncio.run(service.resolve_provenance("sym", state, container_path="/devenv/sym-abc"))
    counter = _WalkCounter(monkeypatch)
    return service, state, counter


def test_two_concurrent_dispatches_cause_one_walk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service, state, counter = _frozen_and_checked(tmp_path, monkeypatch)
    service._stat_pass_at.pop("sym", None)  # the window has lapsed, so the dispatches must walk

    async def both() -> list[Any]:
        return await asyncio.gather(
            service.resolve_provenance("sym", state, container_path="/devenv/sym-abc"),
            service.resolve_provenance("sym", state, container_path="/devenv/sym-abc"),
        )

    results = asyncio.run(both())
    assert all(check.ok for check, _payload in results)
    assert counter.calls == 1, "N concurrent dispatches share one walk in flight"


def test_a_dispatch_inside_the_window_causes_no_walk(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service, state, counter = _frozen_and_checked(tmp_path, monkeypatch)
    check, _payload = asyncio.run(service.resolve_provenance("sym", state, container_path="/devenv/sym-abc"))
    assert check.ok
    assert counter.calls == 0, "a passing check younger than the window is reused"


def test_a_dispatch_after_the_window_walks_again(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service, state, counter = _frozen_and_checked(tmp_path, monkeypatch)
    digest = state.provenance_digest
    assert digest is not None
    service._stat_pass_at["sym"] = (digest, time.monotonic() - 3600)  # older than the 30 s window
    check, _payload = asyncio.run(service.resolve_provenance("sym", state, container_path="/devenv/sym-abc"))
    assert check.ok
    assert counter.calls == 1


def test_the_stat_pass_end_to_end_through_the_shared_walk(tmp_path: Path) -> None:
    state, _cache = _bootstrapped(tmp_path)
    service = _service(tmp_path)
    asyncio.run(service.freeze_provenance("sym", _state_map(state)))
    check, payload = asyncio.run(service.resolve_provenance("sym", state, container_path="/devenv/sym-abc"))
    assert check.ok, check.reason
    assert payload is not None
