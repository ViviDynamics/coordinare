"""Spec 063 Phase 3 T021/T022: end-to-end env-cache invalidation via cache_inputs.

These tests drive ``EnvCacheService.check_and_trigger`` with a sealed env-cache
that has a prior ``services.json`` on disk, and prove:

  T021a — editing a path *listed* in cache_inputs makes the next cycle dispatch
          a bootstrap (cache invalidates).
  T021b — editing a path *not* listed in cache_inputs makes the next cycle skip
          (cache reuses).
  T022  — deleting the prior services.json drops the inference suffix entirely
          and the cycle behaves like a pre-063 cache (first-run fallback).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from coordinare.models.env_cache import EnvCacheState
from coordinare.services.env_cache import EnvCacheService, sanitise_symphony_name


def _good_manifest(cache_inputs: list[str]) -> dict:
    return {
        "services": [
            {
                "name": "postgres",
                "binary": "postgres",
                "version": "16",
                "data_dir": "/tmp/pg",
                "port": 5432,
                "why_needed": "test",
                "sources": cache_inputs[:1] or ["Gemfile"],
            },
        ],
        "cache_inputs": cache_inputs,
        "agent_version": "test-v1",
    }


def _make_cfg(symphony_name: str, cache_root: Path) -> tuple[MagicMock, MagicMock]:
    global_cfg = MagicMock()
    global_cfg.env_cache_root = cache_root
    global_cfg.github_org = "myorg"

    sym = MagicMock()
    sym.name = symphony_name
    sym.env_bootstrap_performer_id = "bootstrap-env"
    sym.env_spec_files = ["README.md"]
    sym.test_env = None  # 092: no configured test-env block by default.

    eff = MagicMock()
    eff.github_org = "myorg"
    eff.project_name = "my-project"
    sym.effective_config = MagicMock(return_value=eff)

    cfg = MagicMock()
    cfg.global_config = global_cfg
    cfg.symphonies = [sym]
    return cfg, sym


def _seed_state(
    tmp_path: Path,
    symphony_name: str,
    manifest: dict | None,
    seal_with_sha: str = "stable-readme-sha",
) -> tuple[dict, Path]:
    sanitised = sanitise_symphony_name(symphony_name)
    cache_dir = tmp_path / sanitised
    cache_dir.mkdir(parents=True, exist_ok=True)
    if manifest is not None:
        services_dir = cache_dir / "services"
        services_dir.mkdir(parents=True, exist_ok=True)
        (services_dir / "services.json").write_text(json.dumps(manifest))

    cache_state = EnvCacheState(
        symphony_name=symphony_name,
        sanitised_name=sanitised,
        cache_dir=cache_dir,
        readme_sha=seal_with_sha,
        cache_dir_ready=True,
    )
    state = {"env_cache": {symphony_name: cache_state}}
    return state, cache_dir


def _gh_with_content(file_contents: dict[str, str]) -> MagicMock:
    """Build an AsyncMock GitHubService whose get_file_content reads from a dict."""
    gh = AsyncMock()

    # Stable blob SHA for README.md so the env_spec_files prefix never shifts;
    # we want the *inference* suffix to be the only varying input.
    gh.get_file_blob_sha = AsyncMock(return_value="readme-blob-sha")

    async def _content(_org: str, _repo: str, path: str) -> str | None:
        return file_contents.get(path)

    gh.get_file_content = AsyncMock(side_effect=_content)
    return gh


def _seal_to_match_current(
    cache_state: EnvCacheState,
    spec_files_hash: str,
    inference_suffix: str,
) -> None:
    cache_state.readme_sha = f"{spec_files_hash}:{inference_suffix}"


def _spec_files_prefix() -> str:
    serialised = json.dumps({"README.md": "readme-blob-sha"}, separators=(",", ":"))
    return hashlib.sha256(serialised.encode()).hexdigest()[:12]


async def _compute_current_inference_suffix(
    manifest: dict, file_contents: dict[str, str],
) -> str:
    from coordinare_service_inference.cache_key import (
        compute_inference_cache_key,
    )
    from coordinare_service_inference.schema import ServicesManifest

    async def _fetch(p: str) -> str | None:
        return file_contents.get(p)

    key = await compute_inference_cache_key(
        agent_version=manifest["agent_version"],
        prior_manifest=ServicesManifest.model_validate(manifest),
        content_fetcher=_fetch,
    )
    return key[:12]


# ---------------------------------------------------------------------------
# T021a: editing a listed cache_input invalidates the cache
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_edit_listed_cache_input_dispatches_bootstrap(tmp_path: Path) -> None:
    sym_name = "my-project"
    manifest = _good_manifest(["Gemfile"])
    state, _cache_dir = _seed_state(tmp_path, sym_name, manifest)
    cfg, sym = _make_cfg(sym_name, tmp_path)
    svc = EnvCacheService(cfg)

    # Initial content — seal the cache to a state that matches.
    file_contents = {"Gemfile": "gem 'rails'\n"}
    initial_suffix = await _compute_current_inference_suffix(manifest, file_contents)
    _seal_to_match_current(
        state["env_cache"][sym_name], _spec_files_prefix(), initial_suffix,
    )

    dispatched: list = []

    async def dispatch_fn(performer_id, payload):
        dispatched.append((performer_id, payload))

    gh = _gh_with_content(file_contents)

    # No-change cycle should NOT dispatch.
    await svc.check_and_trigger(sym_name, sym, gh, state, dispatch_fn)
    assert dispatched == []

    # Mutate listed input → next cycle MUST dispatch.
    file_contents["Gemfile"] = "gem 'rails'\ngem 'pg'\n"
    gh = _gh_with_content(file_contents)
    await svc.check_and_trigger(sym_name, sym, gh, state, dispatch_fn)
    assert len(dispatched) == 1


# ---------------------------------------------------------------------------
# T021b: editing an UNLISTED file does NOT invalidate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_edit_unlisted_path_reuses_cache(tmp_path: Path) -> None:
    sym_name = "my-project"
    manifest = _good_manifest(["Gemfile"])
    state, _cache_dir = _seed_state(tmp_path, sym_name, manifest)
    cfg, sym = _make_cfg(sym_name, tmp_path)
    svc = EnvCacheService(cfg)

    file_contents = {"Gemfile": "gem 'rails'\n", "README.md": "first"}
    initial_suffix = await _compute_current_inference_suffix(manifest, file_contents)
    _seal_to_match_current(
        state["env_cache"][sym_name], _spec_files_prefix(), initial_suffix,
    )

    dispatched: list = []

    async def dispatch_fn(performer_id, payload):
        dispatched.append((performer_id, payload))

    gh = _gh_with_content(file_contents)
    await svc.check_and_trigger(sym_name, sym, gh, state, dispatch_fn)
    assert dispatched == []

    # Mutate a path NOT in cache_inputs — content_fetcher for cache_key only
    # reads Gemfile, so this change is invisible to the inference suffix.
    # (The env_spec_files prefix is also stable — README.md blob sha is mocked.)
    file_contents["README.md"] = "drastically different content"
    gh = _gh_with_content(file_contents)
    await svc.check_and_trigger(sym_name, sym, gh, state, dispatch_fn)
    assert dispatched == [], "cache must be reused when only unlisted paths change"


# ---------------------------------------------------------------------------
# T022: deleting prior services.json engages the first-run fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_missing_prior_manifest_engages_fallback(tmp_path: Path) -> None:
    """No prior services.json → suffix is empty → key is just the spec-files hash.

    Sealing the cache at that bare key proves the cycle reuses on no-change
    AND that subsequent ingestion of a new services.json will tighten the key.
    """
    sym_name = "my-project"
    state, cache_dir = _seed_state(tmp_path, sym_name, manifest=None)
    cfg, sym = _make_cfg(sym_name, tmp_path)
    svc = EnvCacheService(cfg)

    # Seal with no inference suffix — this matches pre-063 readme_sha format.
    state["env_cache"][sym_name].readme_sha = _spec_files_prefix()

    dispatched: list = []

    async def dispatch_fn(performer_id, payload):
        dispatched.append((performer_id, payload))

    gh = _gh_with_content({"Gemfile": "gem 'rails'\n"})
    await svc.check_and_trigger(sym_name, sym, gh, state, dispatch_fn)
    assert dispatched == [], "first-run fallback must reuse when spec files unchanged"

    # Drop a freshly-inferred services.json into the cache dir — the next
    # cycle reads it back, computes a non-empty suffix, and dispatches because
    # the sealed readme_sha (bare prefix) no longer matches.
    services_dir = cache_dir / "services"
    services_dir.mkdir(parents=True, exist_ok=True)
    (services_dir / "services.json").write_text(json.dumps(_good_manifest(["Gemfile"])))

    await svc.check_and_trigger(sym_name, sym, gh, state, dispatch_fn)
    assert len(dispatched) == 1, "tighter key from new manifest must trigger rebuild"


# ---------------------------------------------------------------------------
# T025: Phase 4 self-heal — runtime health failure forces regen in one cycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runtime_health_failure_self_heals(tmp_path: Path) -> None:
    """Spec 063 Phase 4 T025: simulating a non-zero services-health.sh during a
    consumer run flips the env-cache's runtime_health_failed flag; the next
    check_and_trigger cycle dispatches a forced regen with a brand-new key
    (so the cache rebuilds in one cycle) even if spec files are unchanged.
    """
    sym_name = "my-project"
    manifest = _good_manifest(["Gemfile"])
    state, _cache_dir = _seed_state(tmp_path, sym_name, manifest)
    cfg, sym = _make_cfg(sym_name, tmp_path)
    svc = EnvCacheService(cfg)

    # Seal cache to match current content — without the health-failure flag,
    # this state would NOT dispatch.
    file_contents = {"Gemfile": "gem 'rails'\n"}
    initial_suffix = await _compute_current_inference_suffix(manifest, file_contents)
    sealed_key = f"{_spec_files_prefix()}:{initial_suffix}"
    _seal_to_match_current(
        state["env_cache"][sym_name], _spec_files_prefix(), initial_suffix,
    )

    dispatched: list = []

    async def dispatch_fn(performer_id, payload):
        dispatched.append((performer_id, payload))

    gh = _gh_with_content(file_contents)

    # Sanity: no flag → no dispatch.
    await svc.check_and_trigger(sym_name, sym, gh, state, dispatch_fn)
    assert dispatched == []

    # Performer reports unhealthy → monitor_performer would call this:
    svc.mark_runtime_health_failed(sym_name, state)
    cs = state["env_cache"][sym_name]
    assert cs.runtime_health_failed is True

    # Next cycle: forced regen dispatches even though spec files unchanged.
    await svc.check_and_trigger(sym_name, sym, gh, state, dispatch_fn)
    assert len(dispatched) == 1, "forced regen must dispatch in one cycle"

    # The new readme_sha is a 64-char forced-regen key, distinct from sealed.
    assert cs.readme_sha is not None
    assert cs.readme_sha != sealed_key
    assert len(cs.readme_sha) == 64
    # Flag cleared.
    assert cs.runtime_health_failed is False
    # Bootstrap in flight — next on_bootstrap_complete will release it.
    assert cs.bootstrap_in_flight is True
