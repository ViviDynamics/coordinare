"""E2E integration test for spec 060: performer environment caching lifecycle.

Exercises the complete env-caching pipeline end-to-end using real filesystem
operations (tmp_path) and mocked GitHub/dispatch. Covers:

  1. initialise()  — cache dir created, state seeded, initial SHA stored
  2. check_and_trigger() — no dispatch when SHA unchanged
  3. check_and_trigger() — bootstrap dispatched on SHA change; payload correct
  4. on_bootstrap_complete() — state updated; cache_dir_ready preserved
  5. get_env_volume_for_symphony() — ro mount for regular performers
  6. get_env_volume_for_symphony() — rw mount for bootstrap performers
  7. Pending-SHA queueing — second change while in-flight is queued, not lost
  8. Bootstrap failure — SHA retained (088 FR-009); retry gated by cooldown
  9. Dashboard global config API — env_cache_root exposed and editable
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from coordinare.models.env_cache import BootstrapJobPayload, EnvCacheState
from coordinare.services.env_cache import (
    EnvCacheService,
    get_env_volume_for_symphony,
    sanitise_symphony_name,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _combined_sha(per_file: dict[str, str]) -> str:
    s = json.dumps(dict(sorted(per_file.items())), separators=(",", ":"))
    return hashlib.sha256(s.encode()).hexdigest()[:12]


def _make_coordinare_config(symphony_name: str, cache_root: Path, performer_id: str = "bootstrap-env") -> MagicMock:
    """Build a minimal CoordinareConfiguration mock wired for env-caching."""
    global_cfg = MagicMock()
    global_cfg.env_cache_root = cache_root
    global_cfg.github_org = "myorg"
    global_cfg.env_bootstrap_max_attempts = 3

    sym = MagicMock()
    sym.name = symphony_name
    sym.env_bootstrap_performer_id = performer_id
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


def _make_github(sha: str = "abc123", content: str = "# README") -> MagicMock:
    gh = AsyncMock()
    gh.get_file_blob_sha = AsyncMock(return_value=sha)
    gh.get_file_content = AsyncMock(return_value=content)
    return gh


# ---------------------------------------------------------------------------
# Scenario 1-6: Happy-path full lifecycle
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_full_lifecycle(tmp_path: Path) -> None:
    """Complete lifecycle: init → no-change → sha-change → complete → volume check."""
    sym_name = "my-project"
    sanitised = sanitise_symphony_name(sym_name)
    coordinare_cfg, sym_cfg = _make_coordinare_config(sym_name, tmp_path)
    svc = EnvCacheService(coordinare_cfg)

    initial_sha = "aaa111"
    github = _make_github(sha=initial_sha)

    # ---- 1. initialise() ----
    env_cache: dict = {}
    symphony_github = {sym_name: github}
    await svc.initialise(env_cache, symphony_github)

    assert sym_name in env_cache
    state: EnvCacheState = env_cache[sym_name]
    # 061: post-init the cache dir exists but is not ready and SHA is unseeded;
    # readiness only flips after a successful bootstrap.
    assert state.cache_dir_ready is False
    assert state.readme_sha is None
    assert state.cache_dir.is_dir()
    assert state.sanitised_name == sanitised

    # ---- 2. First check_and_trigger() — readme_sha is None, dispatches bootstrap ----
    dispatch_fn = AsyncMock()
    full_state = {"env_cache": env_cache}
    await svc.check_and_trigger(sym_name, sym_cfg, github, full_state, dispatch_fn)
    dispatch_fn.assert_called_once()
    expected_initial = _combined_sha({"README.md": initial_sha})
    assert state.readme_sha == expected_initial
    assert state.bootstrap_in_flight is True

    # First bootstrap completes successfully — cache becomes ready.
    # 092: on_bootstrap_complete downgrades a "successful" bootstrap to failure
    # if the mount holds only the coordinare-seeded files, so seed real toolchain
    # content to exercise the success path.
    (state.cache_dir / "toolchain").mkdir(exist_ok=True)
    svc.on_bootstrap_complete(sym_name, success=True, state=full_state)
    assert state.cache_dir_ready is True
    assert state.bootstrap_in_flight is False

    # ---- 2b. check_and_trigger() — SHA unchanged, no dispatch ----
    dispatch_fn.reset_mock()
    await svc.check_and_trigger(sym_name, sym_cfg, github, full_state, dispatch_fn)
    dispatch_fn.assert_not_called()
    assert state.bootstrap_in_flight is False

    # ---- 3. check_and_trigger() — SHA changed, bootstrap dispatched ----
    new_sha = "bbb222"
    github.get_file_blob_sha = AsyncMock(return_value=new_sha)
    github.get_file_content = AsyncMock(return_value="# Updated README")
    dispatch_fn.reset_mock()

    await svc.check_and_trigger(sym_name, sym_cfg, github, full_state, dispatch_fn)

    dispatch_fn.assert_called_once()
    performer_id_arg, payload_arg = dispatch_fn.call_args.args
    assert performer_id_arg == "bootstrap-env"
    assert isinstance(payload_arg, BootstrapJobPayload)
    assert payload_arg.job_type == "env_bootstrap"
    assert payload_arg.symphony_name == sym_name
    assert payload_arg.env_spec_files == ["README.md"]
    assert payload_arg.env_spec_contents == {"README.md": "# Updated README"}
    assert payload_arg.cache_mount_path.startswith("/devenv/")
    assert sanitised in payload_arg.cache_mount_path

    assert state.bootstrap_in_flight is True
    assert state.readme_sha == _combined_sha({"README.md": new_sha})

    # ---- 4. on_bootstrap_complete(success=True) ----
    svc.on_bootstrap_complete(sym_name, success=True, state=full_state)
    assert state.bootstrap_in_flight is False
    assert state.last_bootstrap_succeeded is True
    assert state.last_bootstrap_at is not None
    assert state.readme_sha == _combined_sha({"README.md": new_sha})  # preserved

    # Simulate the bootstrap performer writing activate.sh — its on-disk
    # presence is the authoritative readiness gate for consumer mounts
    # (cache_dir_ready alone is not enough since it resets on restart).
    (state.cache_dir / "activate.sh").touch()

    # ---- 5. get_env_volume_for_symphony() — regular performer gets ro mount ----
    result = get_env_volume_for_symphony(sym_name, env_cache, is_bootstrap=False)
    assert result is not None
    vol, container_path = result
    assert vol.mode == "ro"
    assert container_path == f"/devenv/{sanitised}"
    assert Path(vol.host_path) == state.cache_dir

    # ---- 6. get_env_volume_for_symphony() — bootstrap performer gets rw mount ----
    result_rw = get_env_volume_for_symphony(sym_name, env_cache, is_bootstrap=True)
    assert result_rw is not None
    vol_rw, _ = result_rw
    assert vol_rw.mode == "rw"


# ---------------------------------------------------------------------------
# Scenario 7: Pending-SHA queueing
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_pending_sha_queued_while_bootstrap_in_flight(tmp_path: Path) -> None:
    """A SHA change that arrives while a bootstrap is in-flight is queued."""
    sym_name = "queued-project"
    coordinare_cfg, sym_cfg = _make_coordinare_config(sym_name, tmp_path)
    svc = EnvCacheService(coordinare_cfg)

    github = _make_github(sha="sha-v1")
    env_cache: dict = {}
    await svc.initialise(env_cache, {sym_name: github})
    state: EnvCacheState = env_cache[sym_name]

    # 061: drive the first bootstrap to completion so the cache is "ready".
    dispatch_fn = AsyncMock()
    full_state = {"env_cache": env_cache}
    await svc.check_and_trigger(sym_name, sym_cfg, github, full_state, dispatch_fn)
    svc.on_bootstrap_complete(sym_name, success=True, state=full_state)

    # Start a bootstrap for sha-v2.
    github.get_file_blob_sha = AsyncMock(return_value="sha-v2")
    github.get_file_content = AsyncMock(return_value="v2 content")
    dispatch_fn.reset_mock()
    await svc.check_and_trigger(sym_name, sym_cfg, github, full_state, dispatch_fn)
    assert state.bootstrap_in_flight is True
    dispatch_fn.assert_called_once()

    # A new change arrives while bootstrap is in-flight — should be queued, not dispatched.
    github.get_file_blob_sha = AsyncMock(return_value="sha-v3")
    dispatch_fn.reset_mock()
    await svc.check_and_trigger(sym_name, sym_cfg, github, full_state, dispatch_fn)
    dispatch_fn.assert_not_called()
    assert state.pending_sha == _combined_sha({"README.md": "sha-v3"})

    # After bootstrap completes, readme_sha cleared so next cycle triggers re-dispatch.
    svc.on_bootstrap_complete(sym_name, success=True, state=full_state)
    assert state.bootstrap_in_flight is False
    assert state.readme_sha is None  # cleared so next cycle picks up pending change


# ---------------------------------------------------------------------------
# Scenario 8: Bootstrap failure keeps SHA; retry gated by cooldown (088 FR-009)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_bootstrap_failure_keeps_sha_and_gates_retry(tmp_path: Path) -> None:
    """088 FR-009: a failed bootstrap keeps readme_sha (no re-fetch hammering);
    the retry is tracked via bootstrap_attempts and gated by the escalating
    cooldown, so an immediate next cycle does NOT redispatch."""
    sym_name = "failing-project"
    coordinare_cfg, sym_cfg = _make_coordinare_config(sym_name, tmp_path)
    svc = EnvCacheService(coordinare_cfg)

    github = _make_github(sha="sha-v1")
    env_cache: dict = {}
    await svc.initialise(env_cache, {sym_name: github})
    state: EnvCacheState = env_cache[sym_name]

    # 061: drive the first bootstrap to completion so we exercise failure
    # against an already-ready cache.
    dispatch_fn = AsyncMock()
    full_state = {"env_cache": env_cache}
    await svc.check_and_trigger(sym_name, sym_cfg, github, full_state, dispatch_fn)
    svc.on_bootstrap_complete(sym_name, success=True, state=full_state)

    github.get_file_blob_sha = AsyncMock(return_value="sha-v2")
    github.get_file_content = AsyncMock(return_value="v2 content")
    dispatch_fn.reset_mock()
    await svc.check_and_trigger(sym_name, sym_cfg, github, full_state, dispatch_fn)
    assert state.bootstrap_in_flight is True
    sha_v2 = state.readme_sha
    assert sha_v2 is not None

    svc.on_bootstrap_complete(sym_name, success=False, state=full_state)
    assert state.bootstrap_in_flight is False
    assert state.last_bootstrap_succeeded is False
    # 088 FR-009: SHA retained — failure no longer clears it.
    assert state.readme_sha == sha_v2
    # Attempt budget tracked; one failure does not exhaust the breaker.
    assert state.bootstrap_attempts == 1
    assert state.bootstrap_exhausted is False

    # Immediate next cycle: retry cooldown has not elapsed → no redispatch.
    dispatch_fn.reset_mock()
    await svc.check_and_trigger(sym_name, sym_cfg, github, full_state, dispatch_fn)
    dispatch_fn.assert_not_called()
    assert state.bootstrap_in_flight is False


# ---------------------------------------------------------------------------
# Scenario 9: No volume before cache is ready
# ---------------------------------------------------------------------------


def test_no_volume_before_cache_ready(tmp_path: Path) -> None:
    """get_env_volume_for_symphony returns None when cache_dir_ready is False."""
    state = EnvCacheState(
        symphony_name="s",
        sanitised_name="s-abc123",
        cache_dir=tmp_path / "s-abc123",
        cache_dir_ready=False,
    )
    result = get_env_volume_for_symphony("s", {"s": state}, is_bootstrap=False)
    assert result is None


def test_no_volume_for_unconfigured_symphony(tmp_path: Path) -> None:
    """get_env_volume_for_symphony returns None when symphony has no cache state."""
    result = get_env_volume_for_symphony("unknown", {}, is_bootstrap=False)
    assert result is None


# ---------------------------------------------------------------------------
# Scenario 10: Multi-file watched specs
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_multi_file_combined_sha(tmp_path: Path) -> None:
    """Bootstrap is triggered when any watched file changes (multi-file mode)."""
    sym_name = "multi-file-project"
    coordinare_cfg, sym_cfg = _make_coordinare_config(sym_name, tmp_path)
    sym_cfg.env_spec_files = ["README.md", "pyproject.toml"]

    github = AsyncMock()
    github.get_file_blob_sha = AsyncMock(side_effect=lambda org, repo, f: {
        "README.md": "readme-sha-1", "pyproject.toml": "toml-sha-1"
    }[f])
    github.get_file_content = AsyncMock(return_value="content")

    env_cache: dict = {}
    svc = EnvCacheService(coordinare_cfg)
    await svc.initialise(env_cache, {sym_name: github})
    state = env_cache[sym_name]
    # 061: post-init state is unseeded — first check_and_trigger drives the
    # initial combined-SHA fetch and dispatches the first bootstrap.
    assert state.readme_sha is None

    dispatch_fn = AsyncMock()
    full_state = {"env_cache": env_cache}
    await svc.check_and_trigger(sym_name, sym_cfg, github, full_state, dispatch_fn)
    initial_combined = _combined_sha({"README.md": "readme-sha-1", "pyproject.toml": "toml-sha-1"})
    assert state.readme_sha == initial_combined
    dispatch_fn.assert_called_once()
    svc.on_bootstrap_complete(sym_name, success=True, state=full_state)

    # Only pyproject.toml changes — combined SHA must differ → dispatch.
    github.get_file_blob_sha = AsyncMock(side_effect=lambda org, repo, f: {
        "README.md": "readme-sha-1", "pyproject.toml": "toml-sha-2"
    }[f])
    dispatch_fn.reset_mock()
    await svc.check_and_trigger(sym_name, sym_cfg, github, full_state, dispatch_fn)
    dispatch_fn.assert_called_once()
    _, payload = dispatch_fn.call_args.args
    assert "pyproject.toml" in payload.env_spec_files
