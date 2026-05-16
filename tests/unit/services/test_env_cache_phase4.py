"""Spec 063 Phase 4 (T024) unit tests for forced-regen self-healing."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from coordinare.models.env_cache import EnvCacheState
from coordinare.services.env_cache import EnvCacheService, sanitise_symphony_name


def _make_cfg(sym_name: str, cache_root: Path) -> tuple[MagicMock, MagicMock]:
    global_cfg = MagicMock()
    global_cfg.env_cache_root = cache_root
    global_cfg.github_org = "myorg"

    sym = MagicMock()
    sym.name = sym_name
    sym.env_bootstrap_performer_id = "bootstrap-env"
    sym.env_spec_files = ["README.md"]

    eff = MagicMock()
    eff.github_org = "myorg"
    eff.project_name = "my-project"
    sym.effective_config = MagicMock(return_value=eff)

    cfg = MagicMock()
    cfg.global_config = global_cfg
    cfg.symphonies = [sym]
    return cfg, sym


def _seed_state(tmp_path: Path, sym_name: str) -> tuple[dict, Path, EnvCacheState]:
    sanitised = sanitise_symphony_name(sym_name)
    cache_dir = tmp_path / sanitised
    cache_dir.mkdir(parents=True, exist_ok=True)
    cs = EnvCacheState(
        symphony_name=sym_name,
        sanitised_name=sanitised,
        cache_dir=cache_dir,
        readme_sha="sealed-sha",
        cache_dir_ready=True,
    )
    return {"env_cache": {sym_name: cs}}, cache_dir, cs


def test_mark_runtime_health_failed_sets_flag(tmp_path: Path) -> None:
    sym_name = "my-project"
    state, _, cs = _seed_state(tmp_path, sym_name)
    cfg, _ = _make_cfg(sym_name, tmp_path)
    svc = EnvCacheService(cfg)

    assert cs.runtime_health_failed is False
    svc.mark_runtime_health_failed(sym_name, state)
    assert cs.runtime_health_failed is True


def test_mark_runtime_health_failed_unknown_symphony_no_raise(tmp_path: Path) -> None:
    cfg, _ = _make_cfg("my-project", tmp_path)
    svc = EnvCacheService(cfg)
    svc.mark_runtime_health_failed("nope", {"env_cache": {}})


@pytest.mark.asyncio
async def test_forced_regen_dispatches_even_when_sha_unchanged(tmp_path: Path) -> None:
    """T024: runtime_health_failed → next check_and_trigger dispatches forced regen."""
    sym_name = "my-project"
    state, _cache_dir, cs = _seed_state(tmp_path, sym_name)
    cfg, sym = _make_cfg(sym_name, tmp_path)
    svc = EnvCacheService(cfg)

    # Flag it.
    svc.mark_runtime_health_failed(sym_name, state)

    gh = AsyncMock()
    # Health failure path should not even fetch SHAs — we dispatch unconditionally.
    gh.get_file_blob_sha = AsyncMock(return_value="sealed-sha")

    async def _content(_o: str, _r: str, p: str) -> str | None:
        return "x"

    gh.get_file_content = AsyncMock(side_effect=_content)

    dispatched: list = []

    async def dispatch_fn(performer_id, payload):
        dispatched.append((performer_id, payload))

    await svc.check_and_trigger(sym_name, sym, gh, state, dispatch_fn)

    assert len(dispatched) == 1, "forced regen must dispatch a bootstrap"
    assert cs.runtime_health_failed is False, "flag must clear after dispatch"
    assert cs.bootstrap_in_flight is True
    # The new readme_sha is the forced-regen 64-char hash, not the sealed one.
    assert cs.readme_sha is not None
    assert cs.readme_sha != "sealed-sha"
    assert len(cs.readme_sha) == 64


@pytest.mark.asyncio
async def test_no_forced_regen_when_flag_unset(tmp_path: Path) -> None:
    sym_name = "my-project"
    state, _, cs = _seed_state(tmp_path, sym_name)
    cfg, sym = _make_cfg(sym_name, tmp_path)
    svc = EnvCacheService(cfg)

    gh = AsyncMock()
    gh.get_file_blob_sha = AsyncMock(return_value="sealed-sha-12")

    # No prior manifest → no inference suffix, sha equals 12-char prefix.
    # We seal cache_state to that exact prefix below.
    serialised = json.dumps({"README.md": "sealed-sha-12"}, separators=(",", ":"))
    expected = hashlib.sha256(serialised.encode()).hexdigest()[:12]
    cs.readme_sha = expected

    dispatched: list = []

    async def dispatch_fn(performer_id, payload):
        dispatched.append((performer_id, payload))

    await svc.check_and_trigger(sym_name, sym, gh, state, dispatch_fn)
    assert dispatched == [], "no flag set → no dispatch"


def test_record_inference_outcome_stamps_state(tmp_path: Path) -> None:
    """063 T026d: terminal performer fields propagate to EnvCacheState."""
    sym_name = "my-project"
    state, _, cs = _seed_state(tmp_path, sym_name)
    cfg, _ = _make_cfg(sym_name, tmp_path)
    svc = EnvCacheService(cfg)

    svc.record_inference_outcome(
        sym_name,
        state,
        skipped_reason=None,
        agent_version="claude-services-v1",
        attempts=2,
        succeeded=True,
        services=["redis", "postgres"],
    )

    assert cs.last_inference_succeeded is True
    assert cs.last_inference_agent_version == "claude-services-v1"
    assert cs.last_inference_attempts == 2
    assert cs.last_inference_services == ["redis", "postgres"]
    assert cs.last_inference_skipped_reason is None
    assert cs.last_inference_at is not None


def test_record_inference_outcome_skipped(tmp_path: Path) -> None:
    sym_name = "my-project"
    state, _, cs = _seed_state(tmp_path, sym_name)
    cfg, _ = _make_cfg(sym_name, tmp_path)
    svc = EnvCacheService(cfg)

    svc.record_inference_outcome(
        sym_name,
        state,
        skipped_reason="manual_override",
        agent_version="manual-override",
        attempts=1,
        succeeded=True,
        services=["redis"],
    )
    assert cs.last_inference_skipped_reason == "manual_override"
    assert cs.last_inference_services == ["redis"]


def test_record_inference_outcome_no_state_is_noop(tmp_path: Path) -> None:
    cfg, _ = _make_cfg("other", tmp_path)
    svc = EnvCacheService(cfg)
    # Missing symphony in env_cache must not raise.
    svc.record_inference_outcome(
        "unknown",
        {"env_cache": {}},
        skipped_reason=None,
        agent_version=None,
        attempts=None,
        succeeded=None,
        services=[],
    )
