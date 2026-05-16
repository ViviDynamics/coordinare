"""Unit tests for performer.main._run_service_inference (spec 063 T026c)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from performer.main import _run_service_inference

if TYPE_CHECKING:
    from pathlib import Path


@pytest.mark.asyncio
async def test_skips_when_env_cache_path_is_empty(tmp_path: Path) -> None:
    out = await _run_service_inference(tmp_path, "")
    assert out == {"inference_skipped_reason": "no_env_cache_path"}


@pytest.mark.asyncio
async def test_skips_when_no_api_key_and_no_manual_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No .coordinare/score.json file → manual override doesn't apply.
    # No API key → LLM path skipped with no_api_key reason.
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir()
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    out = await _run_service_inference(project_dir, str(cache_dir))
    # Either the coordinare package isn't importable in this environment, or
    # the LLM path is skipped for no_api_key. Both are valid "skipped" outcomes
    # that downstream code surfaces verbatim.
    assert out.get("inference_skipped_reason") in {
        "no_api_key",
        "coordinare_not_available",
    }
