"""Source-head invalidation covers arbitrary inputs, additions and deletions.

Reviewed #351 contracts replace the old services.json cache-input suffix. These
checks drive real cache lifecycle orchestration and publication with a controlled
independent-discovery seam; evidence tool-loop behavior has dedicated tests.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from coordinare.services.env_cache import EnvCacheService
from tests.unit.services.test_env_cache_phase4 import _make_cfg, _seed_state
from tests.unit.test_060_env_cache import (
    _accepted_discovery as _accepted_discovery,
)
from tests.unit.test_060_env_cache import (
    _head,
)


def _github(contents: dict[str, str], label: str = "sealed-sha") -> AsyncMock:
    github = AsyncMock()
    github.get_branch_head_sha.return_value = _head(label)
    github.get_repository_paths.return_value = sorted(contents)

    async def fetch(org, repo, path, *, ref):
        return contents.get(path)

    github.get_file_content.side_effect = fetch
    return github


def _record_discovery(monkeypatch):
    from coordinare.services.environment_discovery import discover_and_review

    discovery = AsyncMock(wraps=discover_and_review)
    monkeypatch.setattr("coordinare.services.environment_discovery.discover_and_review", discovery)
    return discovery


@pytest.mark.asyncio
async def test_edit_inspected_input_dispatches_bootstrap(tmp_path: Path, monkeypatch) -> None:
    state, _, cache = _seed_state(tmp_path, "my-project")
    cfg, symphony = _make_cfg("my-project", tmp_path)
    service = EnvCacheService(cfg)
    contents = {"README.md": "Project usage", "tools.plan": "zog7"}
    github = _github(contents)
    dispatch = AsyncMock()
    discovery = _record_discovery(monkeypatch)

    await service.check_and_trigger("my-project", symphony, github, state, dispatch)
    dispatch.assert_not_called()
    discovery.assert_not_called()

    contents["tools.plan"] = "zog8"
    github.get_branch_head_sha.return_value = _head("edited-input")
    await service.check_and_trigger("my-project", symphony, github, state, dispatch)
    dispatch.assert_awaited_once()
    discovery.assert_awaited_once()
    assert cache.bootstrap_commit_sha == _head("edited-input")
    assert cache.environment_manifest["source_commit"] == cache.bootstrap_commit_sha
    assert cache.environment_assessment["manifest_digest"] == dispatch.await_args.args[1].environment_manifest_digest


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["add", "delete"])
async def test_uninspected_inventory_change_invalidates(tmp_path: Path, monkeypatch, change: str) -> None:
    state, _, _ = _seed_state(tmp_path, "my-project")
    cfg, symphony = _make_cfg("my-project", tmp_path)
    service = EnvCacheService(cfg)
    contents = {"README.md": "Project usage", "tools.plan": "unchanged", "nested/other": "old"}
    github = _github(contents)
    dispatch = AsyncMock()
    discovery = _record_discovery(monkeypatch)
    await service.check_and_trigger("my-project", symphony, github, state, dispatch)
    dispatch.assert_not_called()

    if change == "add":
        contents["new-subproject/arbitrary.instructions"] = "Additional setup"
    else:
        del contents["nested/other"]
    github.get_branch_head_sha.return_value = _head("inventory-" + change)
    github.get_repository_paths.return_value = sorted(contents)
    await service.check_and_trigger("my-project", symphony, github, state, dispatch)
    dispatch.assert_awaited_once()
    assert discovery.await_args.kwargs["paths"] == sorted(contents)
    assert contents["tools.plan"] == "unchanged"


@pytest.mark.asyncio
async def test_unchanged_source_skips_agent_and_inventory_calls(tmp_path: Path, monkeypatch) -> None:
    state, _, _ = _seed_state(tmp_path, "my-project")
    cfg, symphony = _make_cfg("my-project", tmp_path)
    service = EnvCacheService(cfg)
    github = _github({"README.md": "Project usage", "tools.plan": "zog7"})
    dispatch = AsyncMock()
    discovery = _record_discovery(monkeypatch)
    for _ in range(2):
        await service.check_and_trigger("my-project", symphony, github, state, dispatch)
    dispatch.assert_not_called()
    discovery.assert_not_called()
    github.get_repository_paths.assert_not_called()
    github.get_file_content.assert_not_called()


@pytest.mark.asyncio
async def test_missing_prior_acceptance_reconstructs_without_legacy_fallback(tmp_path: Path, monkeypatch) -> None:
    state, _, cache = _seed_state(tmp_path, "my-project")
    cache.environment_manifest = None
    cache.environment_assessment = None
    cfg, symphony = _make_cfg("my-project", tmp_path)
    service = EnvCacheService(cfg)
    github = _github({"README.md": "No extra dependencies; baseline usage is documented", "tools.plan": "baseline usage"})
    dispatch = AsyncMock()
    discovery = _record_discovery(monkeypatch)
    await service.check_and_trigger("my-project", symphony, github, state, dispatch)
    discovery.assert_awaited_once()
    dispatch.assert_awaited_once()
    assert cache.environment_manifest is not None
    assert cache.environment_assessment["verdict"] == "accept"
    assert cache.cache_dir_ready is False


@pytest.mark.asyncio
async def test_runtime_health_failure_self_heals(tmp_path: Path, monkeypatch) -> None:
    state, _, cache = _seed_state(tmp_path, "my-project")
    cfg, symphony = _make_cfg("my-project", tmp_path)
    service = EnvCacheService(cfg)
    github = _github({"README.md": "Project usage", "tools.plan": "zog7"})
    dispatch = AsyncMock()
    discovery = _record_discovery(monkeypatch)
    old_key = cache.readme_sha
    await service.check_and_trigger("my-project", symphony, github, state, dispatch)
    dispatch.assert_not_called()

    service.mark_runtime_health_failed("my-project", state)
    await service.check_and_trigger("my-project", symphony, github, state, dispatch)
    dispatch.assert_awaited_once()
    discovery.assert_awaited_once()
    assert cache.readme_sha != old_key
    assert len(cache.readme_sha) == 64
    assert cache.runtime_health_failed is False
    assert cache.bootstrap_in_flight is True
