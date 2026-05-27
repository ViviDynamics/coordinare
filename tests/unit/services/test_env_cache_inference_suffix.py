"""Unit tests for EnvCacheService._inference_cache_suffix (spec 063 Phase 3)."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from coordinare.services.env_cache import EnvCacheService


def _seal_manifest(cache_dir: Path, *, cache_inputs: list[str], agent_version: str) -> None:
    services_dir = cache_dir / "services"
    services_dir.mkdir(parents=True, exist_ok=True)
    (services_dir / "services.json").write_text(
        json.dumps({
            "services": [],
            "cache_inputs": cache_inputs,
            "agent_version": agent_version,
        })
    )


@pytest.mark.asyncio
async def test_inference_cache_suffix_empty_without_manifest(tmp_path: Path) -> None:
    svc = EnvCacheService(coordinare_config=MagicMock())
    gh = MagicMock()
    suffix = await svc._inference_cache_suffix(
        symphony_name="sym",
        cache_dir=tmp_path,
        github_service=gh,
        github_org="o",
        github_repo="r",
    )
    assert suffix == ""


@pytest.mark.asyncio
async def test_inference_cache_suffix_with_manifest_fetches_and_hashes(
    tmp_path: Path,
) -> None:
    _seal_manifest(tmp_path, cache_inputs=["README.md", "pyproject.toml"], agent_version="v1")
    gh = MagicMock()
    gh.get_file_content = AsyncMock(side_effect=["readme-content", "pyproject-content"])

    svc = EnvCacheService(coordinare_config=MagicMock())
    suffix = await svc._inference_cache_suffix(
        symphony_name="sym",
        cache_dir=tmp_path,
        github_service=gh,
        github_org="o",
        github_repo="r",
    )
    assert len(suffix) == 12
    assert gh.get_file_content.await_count == 2


@pytest.mark.asyncio
async def test_inference_cache_suffix_swallows_fetch_exceptions(
    tmp_path: Path,
) -> None:
    _seal_manifest(tmp_path, cache_inputs=["a.txt", "b.txt"], agent_version="v1")
    gh = MagicMock()
    gh.get_file_content = AsyncMock(side_effect=[RuntimeError("boom"), "content-b"])

    svc = EnvCacheService(coordinare_config=MagicMock())
    # Must not raise — the inner _fetch closure logs and returns None on error.
    suffix = await svc._inference_cache_suffix(
        symphony_name="sym",
        cache_dir=tmp_path,
        github_service=gh,
        github_org="o",
        github_repo="r",
    )
    assert len(suffix) == 12
