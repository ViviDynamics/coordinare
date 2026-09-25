"""Unit tests for spec 063 T019/T020 cache-key composition."""

from __future__ import annotations

from pathlib import Path

import pytest
from coordinare_service_inference.cache_key import (
    compute_inference_cache_key,
    forced_regen_cache_key,
    load_prior_manifest,
    local_path_fetcher,
)
from coordinare_service_inference.schema import ServicesManifest


def _manifest(cache_inputs: list[str], agent_version: str = "v1") -> ServicesManifest:
    return ServicesManifest.model_validate(
        {
            "services": [
                {
                    "name": "redis",
                    "binary": "redis-server",
                    "version": "7.2",
                    "data_dir": "/tmp/redis",
                    "port": 6379,
                    "why_needed": "test",
                    "kind": "redis",
                    "sources": cache_inputs[:1] or ["Gemfile"],
                },
            ],
            "cache_inputs": cache_inputs,
            "agent_version": agent_version,
        },
    )


# ---------------------------------------------------------------------------
# T019: load_prior_manifest
# ---------------------------------------------------------------------------


def test_load_prior_manifest_returns_none_when_missing(tmp_path: Path) -> None:
    assert load_prior_manifest(tmp_path) is None


def test_load_prior_manifest_roundtrips(tmp_path: Path) -> None:
    manifest = _manifest(["Gemfile", "config/database.yml"])
    services_dir = tmp_path / "services"
    services_dir.mkdir()
    (services_dir / "services.json").write_text(manifest.model_dump_json())

    loaded = load_prior_manifest(tmp_path)
    assert loaded is not None
    assert loaded.cache_inputs == ["Gemfile", "config/database.yml"]
    assert loaded.agent_version == "v1"


def test_load_prior_manifest_returns_none_on_invalid_json(tmp_path: Path) -> None:
    services_dir = tmp_path / "services"
    services_dir.mkdir()
    (services_dir / "services.json").write_text("{ this is not json")
    assert load_prior_manifest(tmp_path) is None


def test_load_prior_manifest_returns_none_on_schema_mismatch(tmp_path: Path) -> None:
    services_dir = tmp_path / "services"
    services_dir.mkdir()
    (services_dir / "services.json").write_text('{"services": "wrong type"}')
    assert load_prior_manifest(tmp_path) is None


# ---------------------------------------------------------------------------
# T020: compute_inference_cache_key
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cache_key_changes_when_listed_path_content_changes(
    tmp_path: Path,
) -> None:
    """Editing a file listed in cache_inputs must invalidate the key."""
    (tmp_path / "Gemfile").write_text("gem 'rails'\n")
    fetcher = local_path_fetcher(tmp_path)
    manifest = _manifest(["Gemfile"])

    before = await compute_inference_cache_key(
        agent_version="v1", prior_manifest=manifest, content_fetcher=fetcher,
    )

    (tmp_path / "Gemfile").write_text("gem 'rails'\ngem 'pg'\n")
    after = await compute_inference_cache_key(
        agent_version="v1", prior_manifest=manifest, content_fetcher=fetcher,
    )
    assert before != after


@pytest.mark.asyncio
async def test_cache_key_stable_when_unlisted_path_changes(tmp_path: Path) -> None:
    """Editing a file NOT in cache_inputs must NOT invalidate the key."""
    (tmp_path / "Gemfile").write_text("gem 'rails'\n")
    (tmp_path / "README.md").write_text("hello\n")
    fetcher = local_path_fetcher(tmp_path)
    manifest = _manifest(["Gemfile"])

    before = await compute_inference_cache_key(
        agent_version="v1", prior_manifest=manifest, content_fetcher=fetcher,
    )

    (tmp_path / "README.md").write_text("hello, world\n")
    after = await compute_inference_cache_key(
        agent_version="v1", prior_manifest=manifest, content_fetcher=fetcher,
    )
    assert before == after


@pytest.mark.asyncio
async def test_cache_key_changes_with_agent_version(tmp_path: Path) -> None:
    (tmp_path / "Gemfile").write_text("gem 'rails'\n")
    fetcher = local_path_fetcher(tmp_path)
    manifest = _manifest(["Gemfile"])

    v1 = await compute_inference_cache_key(
        agent_version="v1", prior_manifest=manifest, content_fetcher=fetcher,
    )
    v2 = await compute_inference_cache_key(
        agent_version="v2", prior_manifest=manifest, content_fetcher=fetcher,
    )
    assert v1 != v2


@pytest.mark.asyncio
async def test_first_run_uses_fallback_paths(tmp_path: Path) -> None:
    """No prior manifest → key is derived from fallback_paths content."""
    (tmp_path / "README.md").write_text("hello\n")
    (tmp_path / "Gemfile").write_text("gem 'rails'\n")
    fetcher = local_path_fetcher(tmp_path)

    key = await compute_inference_cache_key(
        agent_version="v1",
        prior_manifest=None,
        content_fetcher=fetcher,
        fallback_paths=["README.md", "Gemfile"],
    )

    # Editing a fallback path must invalidate.
    (tmp_path / "README.md").write_text("changed\n")
    key2 = await compute_inference_cache_key(
        agent_version="v1",
        prior_manifest=None,
        content_fetcher=fetcher,
        fallback_paths=["README.md", "Gemfile"],
    )
    assert key != key2


@pytest.mark.asyncio
async def test_first_run_requires_non_empty_fallback() -> None:
    async def empty(_: str) -> str | None:
        return None

    with pytest.raises(ValueError):
        await compute_inference_cache_key(
            agent_version="v1",
            prior_manifest=None,
            content_fetcher=empty,
            fallback_paths=[],
        )


@pytest.mark.asyncio
async def test_missing_listed_file_changes_key(tmp_path: Path) -> None:
    """Deleting a tracked cache_input must invalidate (its absence is distinct)."""
    (tmp_path / "Gemfile").write_text("gem 'rails'\n")
    fetcher = local_path_fetcher(tmp_path)
    manifest = _manifest(["Gemfile"])

    before = await compute_inference_cache_key(
        agent_version="v1", prior_manifest=manifest, content_fetcher=fetcher,
    )

    (tmp_path / "Gemfile").unlink()
    after = await compute_inference_cache_key(
        agent_version="v1", prior_manifest=manifest, content_fetcher=fetcher,
    )
    assert before != after


@pytest.mark.asyncio
async def test_cache_input_order_does_not_matter(tmp_path: Path) -> None:
    (tmp_path / "a").write_text("a\n")
    (tmp_path / "b").write_text("b\n")
    fetcher = local_path_fetcher(tmp_path)
    m1 = _manifest(["a", "b"])
    m2 = _manifest(["b", "a"])

    k1 = await compute_inference_cache_key(
        agent_version="v1", prior_manifest=m1, content_fetcher=fetcher,
    )
    k2 = await compute_inference_cache_key(
        agent_version="v1", prior_manifest=m2, content_fetcher=fetcher,
    )
    assert k1 == k2


@pytest.mark.asyncio
async def test_empty_cache_inputs_marker_does_not_collapse(tmp_path: Path) -> None:
    """A manifest with cache_inputs=[] must not produce sha256(agent_version) alone."""

    async def never_called(_: str) -> str | None:
        raise AssertionError("fetcher should not be called with no inputs")

    manifest = _manifest([])
    key = await compute_inference_cache_key(
        agent_version="v1", prior_manifest=manifest, content_fetcher=never_called,
    )
    # Compare to a different agent_version + same empty inputs — should differ.
    key2 = await compute_inference_cache_key(
        agent_version="v2", prior_manifest=manifest, content_fetcher=never_called,
    )
    assert key != key2


# ---------------------------------------------------------------------------
# Sandboxing in local_path_fetcher
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_local_path_fetcher_rejects_traversal(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("secret")
    project = tmp_path / "project"
    project.mkdir()
    fetcher = local_path_fetcher(project)
    assert await fetcher("../outside.txt") is None


# ---------------------------------------------------------------------------
# forced_regen_cache_key
# ---------------------------------------------------------------------------


def test_forced_regen_keys_are_unique() -> None:
    a = forced_regen_cache_key("v1")
    b = forced_regen_cache_key("v1")
    assert a != b
    assert len(a) == 64
