"""#400: a forced env-cache regen must cost one bootstrap, not two.

Every env block on 2026-09-12/13 produced two full bootstraps (~20 minutes
each). A forced regen dispatches under ``forced_regen_cache_key(...)``, a key
deliberately unequal to the manifest SHA. While it is in flight the normal
check runs each cycle, computes the manifest SHA, sees it differs from what is
in flight, and queues it as ``pending_sha``. On completion ``pending !=
readme_sha`` is true (forced key vs manifest key), so ``readme_sha`` is reset
and the next cycle bootstraps again. The pending mechanism exists to catch a
README that changed mid-bootstrap; a forced regen trips it every time because
the forced key can never equal the manifest key.

The fix records the manifest SHA the forced run was built against and adopts
it on completion, so the next cycle compares like with like. A manifest that
genuinely changes during the forced run still triggers the second bootstrap.

The first draft also skipped queueing pending_sha during a forced run when
the manifest was unchanged. Mutation testing showed that branch had no
observable effect either way: once completion adopts the manifest SHA, the next
cycle's ordinary ``current_sha != readme_sha`` comparison handles both the
unchanged and the changed manifest. It was removed. The fix is two moves: record
the manifest at forced dispatch, adopt it at completion.

MUTATIONS THAT MUST FAIL A TEST HERE:
  M1  do not record the manifest SHA at forced dispatch
      -> test_a_forced_regen_costs_one_bootstrap
  M2  adopt the forced key (not the manifest SHA) on completion
      -> test_a_forced_regen_costs_one_bootstrap
  M3  never mark the run as forced (bookkeeping absent)
      -> test_forced_bookkeeping_is_cleared_on_completion
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from coordinare.services.env_cache import EnvCacheService
from tests.unit.services.test_env_cache_phase4 import _make_cfg, _seed_state
from tests.unit.test_060_env_cache import _seed_toolchain


def _github(blob_sha: str) -> AsyncMock:
    gh = AsyncMock()
    gh.get_file_blob_sha = AsyncMock(return_value=blob_sha)

    async def _content(_o: str, _r: str, _p: str) -> str | None:
        return "x"

    gh.get_file_content = AsyncMock(side_effect=_content)
    return gh


async def _cycle(svc, sym_name, sym, gh, state, dispatched):
    async def dispatch_fn(performer_id, payload):
        dispatched.append(performer_id)

    await svc.check_and_trigger(sym_name, sym, gh, state, dispatch_fn)


@pytest.mark.asyncio
async def test_a_forced_regen_costs_one_bootstrap(tmp_path: Path) -> None:
    """M1/M2: forced dispatch, an in-flight cycle with an unchanged manifest,
    completion, another cycle. One dispatch, not two."""
    sym_name = "my-project"
    state, _cache_dir, cs = _seed_state(tmp_path, sym_name)
    cfg, sym = _make_cfg(sym_name, tmp_path)
    svc = EnvCacheService(cfg)
    gh = _github("sealed-sha")
    dispatched: list = []

    svc.mark_runtime_health_failed(sym_name, state)
    await _cycle(svc, sym_name, sym, gh, state, dispatched)
    assert dispatched == [sym.env_bootstrap_performer_id], "the forced regen itself"
    assert cs.bootstrap_in_flight is True

    await _cycle(svc, sym_name, sym, gh, state, dispatched)  # in flight, manifest unchanged
    assert len(dispatched) == 1

    _seed_toolchain(_cache_dir)  # a real bootstrap leaves a toolchain; an empty dir reads as a failed run
    svc.on_bootstrap_complete(sym_name, True, state)
    assert cs.bootstrap_in_flight is False

    await _cycle(svc, sym_name, sym, gh, state, dispatched)
    assert len(dispatched) == 1, (
        "a second bootstrap ran for an unchanged manifest: the forced key was "
        "compared against the manifest SHA"
    )


@pytest.mark.asyncio
async def test_a_manifest_change_during_a_forced_regen_still_rebootstraps(tmp_path: Path) -> None:
    """M3: the pending mechanism's real purpose survives. A README that changes
    while the forced run is in flight is a new manifest; it must be built."""
    sym_name = "my-project"
    state, _cache_dir, _cs = _seed_state(tmp_path, sym_name)
    cfg, sym = _make_cfg(sym_name, tmp_path)
    svc = EnvCacheService(cfg)
    gh = _github("sealed-sha")
    dispatched: list = []

    svc.mark_runtime_health_failed(sym_name, state)
    await _cycle(svc, sym_name, sym, gh, state, dispatched)
    assert len(dispatched) == 1

    gh.get_file_blob_sha = AsyncMock(return_value="changed-sha")  # README edited mid-run
    await _cycle(svc, sym_name, sym, gh, state, dispatched)
    assert len(dispatched) == 1, "still in flight; the change is queued, not dispatched"

    _seed_toolchain(_cache_dir)  # a real bootstrap leaves a toolchain; an empty dir reads as a failed run
    svc.on_bootstrap_complete(sym_name, True, state)
    await _cycle(svc, sym_name, sym, gh, state, dispatched)
    assert len(dispatched) == 2, "the changed manifest must be built"


@pytest.mark.asyncio
async def test_forced_bookkeeping_is_cleared_on_completion(tmp_path: Path) -> None:
    sym_name = "my-project"
    state, _cache_dir, cs = _seed_state(tmp_path, sym_name)
    cfg, sym = _make_cfg(sym_name, tmp_path)
    svc = EnvCacheService(cfg)
    dispatched: list = []

    svc.mark_runtime_health_failed(sym_name, state)
    await _cycle(svc, sym_name, sym, _github("sealed-sha"), state, dispatched)
    assert cs.bootstrap_forced is True
    _seed_toolchain(_cache_dir)  # a real bootstrap leaves a toolchain; an empty dir reads as a failed run
    svc.on_bootstrap_complete(sym_name, True, state)
    assert cs.bootstrap_forced is False
    assert cs.forced_manifest_sha is None


@pytest.mark.asyncio
async def test_a_forced_dispatch_that_never_starts_leaves_no_forced_markers(tmp_path: Path) -> None:
    """Copilot review on #407: ``_do_dispatch`` returns early when a spec file
    cannot be fetched, before ``bootstrap_in_flight`` is set. The forced markers
    must not outlive that no-op, or the next ORDINARY completion is read as
    forced and its ``readme_sha`` is overwritten with the stale manifest SHA."""
    sym_name = "my-project"
    state, _cache_dir, cs = _seed_state(tmp_path, sym_name)
    cfg, sym = _make_cfg(sym_name, tmp_path)
    svc = EnvCacheService(cfg)
    dispatched: list = []

    gh = _github("old-sha")
    gh.get_file_content = AsyncMock(side_effect=RuntimeError("github down"))
    svc.mark_runtime_health_failed(sym_name, state)
    await _cycle(svc, sym_name, sym, gh, state, dispatched)
    assert dispatched == [], "the fetch failed; nothing was dispatched"
    assert cs.bootstrap_in_flight is False
    assert cs.bootstrap_forced is False, "forced marker stranded after a no-op dispatch"
    assert cs.forced_manifest_sha is None

    # Later, an ordinary bootstrap for a changed manifest runs and completes.
    gh = _github("new-sha")
    await _cycle(svc, sym_name, sym, gh, state, dispatched)
    assert len(dispatched) == 1
    recorded = cs.readme_sha  # the manifest SHA the ordinary dispatch was keyed on
    assert recorded is not None
    _seed_toolchain(_cache_dir)  # a real bootstrap leaves a toolchain; an empty dir reads as a failed run
    svc.on_bootstrap_complete(sym_name, True, state)
    assert cs.readme_sha == recorded, "ordinary completion adopted a stale forced SHA"
    await _cycle(svc, sym_name, sym, gh, state, dispatched)
    assert len(dispatched) == 1, "a second bootstrap ran for an unchanged manifest"
