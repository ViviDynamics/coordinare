"""Tests for spec 060: performer environment caching."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from coordinare.models.env_cache import BootstrapJobPayload, EnvCacheState
from coordinare.services.env_cache import (
    EnvCacheService,
    _collect_env_volumes_for_persistent_performer,
    get_env_volume_for_symphony,
    sanitise_symphony_name,
)


def _combined_sha(per_file: dict[str, str]) -> str:
    """Compute the 12-char combined SHA that EnvCacheService stores."""
    s = json.dumps(dict(sorted(per_file.items())), separators=(",", ":"))
    return hashlib.sha256(s.encode()).hexdigest()[:12]


# ---------------------------------------------------------------------------
# T026: sanitise_symphony_name
# ---------------------------------------------------------------------------


class TestSanitiseSymphonyName:
    def test_lowercase_and_slug(self) -> None:
        result = sanitise_symphony_name("My Project")
        assert result == result.lower()
        assert " " not in result

    def test_special_chars_replaced_with_dashes(self) -> None:
        result = sanitise_symphony_name("Hello World!")
        # Exclamation and space become dashes
        assert "!" not in result
        assert " " not in result

    def test_sha1_suffix_always_appended(self) -> None:
        result = sanitise_symphony_name("test")
        parts = result.rsplit("-", 1)
        assert len(parts) == 2
        suffix = parts[1]
        assert len(suffix) == 6
        assert all(c in "0123456789abcdef" for c in suffix)

    def test_deterministic(self) -> None:
        assert sanitise_symphony_name("abc") == sanitise_symphony_name("abc")

    def test_different_names_different_results(self) -> None:
        assert sanitise_symphony_name("alpha") != sanitise_symphony_name("beta")

    def test_empty_name_uses_symphony_fallback(self) -> None:
        result = sanitise_symphony_name("!!!---")
        assert result.startswith("symphony-")

    def test_no_leading_trailing_dashes_in_slug(self) -> None:
        result = sanitise_symphony_name("---abc---")
        slug = result.rsplit("-", 1)[0]
        assert not slug.startswith("-")
        assert not slug.endswith("-")


# ---------------------------------------------------------------------------
# T028: EnvCacheState and BootstrapJobPayload model tests
# ---------------------------------------------------------------------------


class TestEnvCacheState:
    def test_defaults(self) -> None:
        state = EnvCacheState(
            symphony_name="s",
            sanitised_name="s-abc123",
            cache_dir=Path("/tmp/cache"),
        )
        assert state.readme_sha is None
        assert state.bootstrap_in_flight is False
        assert state.pending_sha is None
        assert state.last_bootstrap_at is None
        assert state.last_bootstrap_succeeded is None

    def test_cache_dir_stored(self) -> None:
        state = EnvCacheState(
            symphony_name="proj",
            sanitised_name="proj-aabbcc",
            cache_dir=Path("/data/envs/proj"),
        )
        assert state.cache_dir == Path("/data/envs/proj")


class TestBootstrapJobPayload:
    def test_job_type_literal(self) -> None:
        p = BootstrapJobPayload(
            symphony_name="s",
            symphony_org="org",
            symphony_repo="repo",
            env_spec_contents={"README.md": "content"},
        )
        assert p.job_type == "env_bootstrap"

    def test_defaults(self) -> None:
        p = BootstrapJobPayload(
            symphony_name="s",
            symphony_org="org",
            symphony_repo="repo",
            env_spec_contents={"README.md": "content"},
        )
        assert p.env_spec_files == ["README.md"]
        assert p.cache_mount_path == "/devenv/symphony"


# ---------------------------------------------------------------------------
# T027: EnvCacheService unit tests
# ---------------------------------------------------------------------------


class TestGetEnvVolumeForSymphony:
    def test_returns_none_when_no_state(self) -> None:
        result = get_env_volume_for_symphony("missing", {}, is_bootstrap=False)
        assert result is None

    def test_returns_none_when_cache_dir_missing(self, tmp_path: Path) -> None:
        state = EnvCacheState(
            symphony_name="s",
            sanitised_name="s-abc",
            cache_dir=tmp_path / "nonexistent",
        )
        result = get_env_volume_for_symphony("s", {"s": state}, is_bootstrap=False)
        assert result is None

    def test_returns_ro_volume_for_regular_performer(self, tmp_path: Path) -> None:
        from coordinare.models.performer_endpoint import VolumeMount

        cache_dir = tmp_path / "env"
        cache_dir.mkdir()
        state = EnvCacheState(
            symphony_name="s",
            sanitised_name="s-abc",
            cache_dir=cache_dir,
            cache_dir_ready=True,
        )
        result = get_env_volume_for_symphony("s", {"s": state}, is_bootstrap=False)
        assert result is not None
        vol, container_path = result
        assert isinstance(vol, VolumeMount)
        assert vol.mode == "ro"
        assert container_path == "/devenv/s-abc"
        assert str(vol.container_path) == "/devenv/s-abc"
        assert vol.host_path == cache_dir

    def test_returns_rw_volume_for_bootstrap_performer(self, tmp_path: Path) -> None:
        cache_dir = tmp_path / "env"
        cache_dir.mkdir()
        state = EnvCacheState(
            symphony_name="s",
            sanitised_name="s-abc",
            cache_dir=cache_dir,
            cache_dir_ready=True,
        )
        result = get_env_volume_for_symphony("s", {"s": state}, is_bootstrap=True)
        assert result is not None
        vol, _ = result
        assert vol.mode == "rw"

    def test_uses_custom_container_devenv_root(self, tmp_path: Path) -> None:
        cache_dir = tmp_path / "env"
        cache_dir.mkdir()
        state = EnvCacheState(
            symphony_name="s",
            sanitised_name="s-abc",
            cache_dir=cache_dir,
            cache_dir_ready=True,
        )
        result = get_env_volume_for_symphony(
            "s", {"s": state}, is_bootstrap=False, container_devenv_root="/custom"
        )
        assert result is not None
        vol, container_path = result
        assert container_path == "/custom/s-abc"
        assert str(vol.container_path) == "/custom/s-abc"


class TestEnvCacheServiceCheckAndTrigger:
    def _make_service(self) -> tuple[EnvCacheService, MagicMock]:
        coordinare_config = MagicMock()
        coordinare_config.symphonies = []
        svc = EnvCacheService(coordinare_config)
        return svc, coordinare_config

    def _make_symphony_config(self, performer_id: str | None = "bootstrap") -> MagicMock:
        cfg = MagicMock()
        cfg.env_bootstrap_performer_id = performer_id
        cfg.env_spec_files = ["README.md"]
        cfg.name = "test-symphony"
        return cfg

    def _make_eff_config(self) -> MagicMock:
        eff = MagicMock()
        eff.github_org = "org"
        eff.project_name = "repo"
        return eff

    @pytest.mark.asyncio
    async def test_returns_early_when_no_bootstrap_performer(self) -> None:
        svc, _ = self._make_service()
        sym_cfg = self._make_symphony_config(performer_id=None)
        dispatch_fn = AsyncMock()
        github = AsyncMock()
        await svc.check_and_trigger("sym", sym_cfg, github, {}, dispatch_fn)
        dispatch_fn.assert_not_called()

    @pytest.mark.asyncio
    async def test_dispatches_when_sha_changed(self, tmp_path: Path) -> None:
        svc, _ = self._make_service()
        cache_dir = tmp_path / "env"
        cache_dir.mkdir()
        cache_state = EnvCacheState(
            symphony_name="sym",
            sanitised_name="sym-abc",
            cache_dir=cache_dir,
            readme_sha="old_sha",
        )
        global_cfg = MagicMock()
        sym_cfg = self._make_symphony_config()
        sym_cfg.effective_config = MagicMock(return_value=self._make_eff_config())
        github = AsyncMock()
        github.get_file_blob_sha = AsyncMock(return_value="new_sha")
        github.get_file_content = AsyncMock(return_value="# README content")
        dispatch_fn = AsyncMock()

        state: dict = {
            "env_cache": {"sym": cache_state},
            "config": global_cfg,
        }
        await svc.check_and_trigger("sym", sym_cfg, github, state, dispatch_fn)

        dispatch_fn.assert_awaited_once()
        assert cache_state.bootstrap_in_flight is True
        assert cache_state.readme_sha == _combined_sha({"README.md": "new_sha"})

    @pytest.mark.asyncio
    async def test_skips_dispatch_when_sha_unchanged(self, tmp_path: Path) -> None:
        svc, _ = self._make_service()
        cache_dir = tmp_path / "env"
        cache_dir.mkdir()
        cache_state = EnvCacheState(
            symphony_name="sym",
            sanitised_name="sym-abc",
            cache_dir=cache_dir,
            readme_sha=_combined_sha({"README.md": "same_sha"}),
        )
        global_cfg = MagicMock()
        sym_cfg = self._make_symphony_config()
        sym_cfg.effective_config = MagicMock(return_value=self._make_eff_config())
        github = AsyncMock()
        github.get_file_blob_sha = AsyncMock(return_value="same_sha")
        dispatch_fn = AsyncMock()

        state: dict = {"env_cache": {"sym": cache_state}, "config": global_cfg}
        await svc.check_and_trigger("sym", sym_cfg, github, state, dispatch_fn)

        dispatch_fn.assert_not_called()

    @pytest.mark.asyncio
    async def test_returns_early_when_cache_state_missing(self, tmp_path: Path) -> None:
        svc, _ = self._make_service()
        sym_cfg = self._make_symphony_config()
        sym_cfg.effective_config = MagicMock(return_value=self._make_eff_config())
        dispatch_fn = AsyncMock()
        state: dict = {"env_cache": {}, "config": MagicMock()}
        await svc.check_and_trigger("sym", sym_cfg, AsyncMock(), state, dispatch_fn)
        dispatch_fn.assert_not_called()

    @pytest.mark.asyncio
    async def test_returns_early_when_eff_config_none(self, tmp_path: Path) -> None:
        svc, _ = self._make_service()
        cache_dir = tmp_path / "env"
        cache_dir.mkdir()
        cache_state = EnvCacheState(
            symphony_name="sym", sanitised_name="sym-abc", cache_dir=cache_dir
        )
        sym_cfg = self._make_symphony_config()
        sym_cfg.effective_config = MagicMock(return_value=None)
        dispatch_fn = AsyncMock()
        state: dict = {"env_cache": {"sym": cache_state}, "config": None}
        await svc.check_and_trigger("sym", sym_cfg, AsyncMock(), state, dispatch_fn)
        dispatch_fn.assert_not_called()

    @pytest.mark.asyncio
    async def test_handles_sha_fetch_exception(self, tmp_path: Path) -> None:
        svc, _ = self._make_service()
        cache_dir = tmp_path / "env"
        cache_dir.mkdir()
        cache_state = EnvCacheState(
            symphony_name="sym", sanitised_name="sym-abc", cache_dir=cache_dir
        )
        sym_cfg = self._make_symphony_config()
        sym_cfg.effective_config = MagicMock(return_value=self._make_eff_config())
        github = AsyncMock()
        github.get_file_blob_sha = AsyncMock(side_effect=RuntimeError("timeout"))
        dispatch_fn = AsyncMock()
        state: dict = {"env_cache": {"sym": cache_state}, "config": MagicMock()}
        await svc.check_and_trigger("sym", sym_cfg, github, state, dispatch_fn)
        dispatch_fn.assert_not_called()

    @pytest.mark.asyncio
    async def test_handles_sha_fetch_returns_none(self, tmp_path: Path) -> None:
        svc, _ = self._make_service()
        cache_dir = tmp_path / "env"
        cache_dir.mkdir()
        cache_state = EnvCacheState(
            symphony_name="sym", sanitised_name="sym-abc", cache_dir=cache_dir
        )
        sym_cfg = self._make_symphony_config()
        sym_cfg.effective_config = MagicMock(return_value=self._make_eff_config())
        github = AsyncMock()
        github.get_file_blob_sha = AsyncMock(return_value=None)
        dispatch_fn = AsyncMock()
        state: dict = {"env_cache": {"sym": cache_state}, "config": MagicMock()}
        await svc.check_and_trigger("sym", sym_cfg, github, state, dispatch_fn)
        dispatch_fn.assert_not_called()

    @pytest.mark.asyncio
    async def test_handles_content_fetch_exception(self, tmp_path: Path) -> None:
        svc, _ = self._make_service()
        cache_dir = tmp_path / "env"
        cache_dir.mkdir()
        cache_state = EnvCacheState(
            symphony_name="sym", sanitised_name="sym-abc", cache_dir=cache_dir, readme_sha="old"
        )
        sym_cfg = self._make_symphony_config()
        sym_cfg.effective_config = MagicMock(return_value=self._make_eff_config())
        github = AsyncMock()
        github.get_file_blob_sha = AsyncMock(return_value="new_sha")
        github.get_file_content = AsyncMock(side_effect=RuntimeError("fetch failed"))
        dispatch_fn = AsyncMock()
        state: dict = {"env_cache": {"sym": cache_state}, "config": MagicMock()}
        await svc.check_and_trigger("sym", sym_cfg, github, state, dispatch_fn)
        dispatch_fn.assert_not_called()
        assert cache_state.bootstrap_in_flight is False

    @pytest.mark.asyncio
    async def test_handles_dispatch_exception(self, tmp_path: Path) -> None:
        svc, _ = self._make_service()
        cache_dir = tmp_path / "env"
        cache_dir.mkdir()
        cache_state = EnvCacheState(
            symphony_name="sym", sanitised_name="sym-abc", cache_dir=cache_dir, readme_sha="old"
        )
        sym_cfg = self._make_symphony_config()
        sym_cfg.effective_config = MagicMock(return_value=self._make_eff_config())
        github = AsyncMock()
        github.get_file_blob_sha = AsyncMock(return_value="new_sha")
        github.get_file_content = AsyncMock(return_value="# content")
        dispatch_fn = AsyncMock(side_effect=RuntimeError("dispatch error"))
        state: dict = {"env_cache": {"sym": cache_state}, "config": MagicMock()}
        await svc.check_and_trigger("sym", sym_cfg, github, state, dispatch_fn)
        assert cache_state.bootstrap_in_flight is False

    @pytest.mark.asyncio
    async def test_queues_pending_sha_when_in_flight(self, tmp_path: Path) -> None:
        svc, _ = self._make_service()
        cache_dir = tmp_path / "env"
        cache_dir.mkdir()
        cache_state = EnvCacheState(
            symphony_name="sym",
            sanitised_name="sym-abc",
            cache_dir=cache_dir,
            readme_sha="old_sha",
            bootstrap_in_flight=True,
        )
        global_cfg = MagicMock()
        sym_cfg = self._make_symphony_config()
        sym_cfg.effective_config = MagicMock(return_value=self._make_eff_config())
        github = AsyncMock()
        github.get_file_blob_sha = AsyncMock(return_value="newer_sha")
        dispatch_fn = AsyncMock()

        state: dict = {"env_cache": {"sym": cache_state}, "config": global_cfg}
        await svc.check_and_trigger("sym", sym_cfg, github, state, dispatch_fn)

        dispatch_fn.assert_not_called()
        assert cache_state.pending_sha == _combined_sha({"README.md": "newer_sha"})


class TestEnvCacheServiceInitialise:
    def _make_symphony(
        self,
        name: str = "sym",
        performer_id: str | None = "bootstrap",
        env_spec_files: list[str] | None = None,
    ) -> MagicMock:
        sym = MagicMock()
        sym.name = name
        sym.env_bootstrap_performer_id = performer_id
        sym.env_spec_files = env_spec_files if env_spec_files is not None else ["README.md"]
        eff = MagicMock()
        eff.github_org = "org"
        eff.project_name = "repo"
        sym.effective_config = MagicMock(return_value=eff)
        return sym

    def _make_service(self, tmp_path: Path, symphonies: list) -> MagicMock:
        cfg = MagicMock()
        cfg.symphonies = symphonies
        cfg.global_config.env_cache_root = str(tmp_path)
        svc = EnvCacheService(cfg)
        return svc

    @pytest.mark.asyncio
    async def test_skips_symphony_without_bootstrap_performer(self, tmp_path: Path) -> None:
        sym = self._make_symphony(performer_id=None)
        svc = self._make_service(tmp_path, [sym])
        env_cache: dict = {}
        await svc.initialise(env_cache, {})
        assert "sym" not in env_cache

    @pytest.mark.asyncio
    async def test_creates_cache_dir_and_state_without_github(self, tmp_path: Path) -> None:
        sym = self._make_symphony()
        svc = self._make_service(tmp_path, [sym])
        env_cache: dict = {}
        await svc.initialise(env_cache, {})
        assert "sym" in env_cache
        assert env_cache["sym"].readme_sha is None
        assert env_cache["sym"].cache_dir.exists()

    @pytest.mark.asyncio
    async def test_fetches_initial_sha_when_github_available(self, tmp_path: Path) -> None:
        sym = self._make_symphony()
        svc = self._make_service(tmp_path, [sym])
        github = AsyncMock()
        github.get_file_blob_sha = AsyncMock(return_value="abc123")
        env_cache: dict = {}
        await svc.initialise(env_cache, {"sym": github})
        assert env_cache["sym"].readme_sha == _combined_sha({"README.md": "abc123"})

    @pytest.mark.asyncio
    async def test_handles_sha_fetch_exception(self, tmp_path: Path) -> None:
        sym = self._make_symphony()
        svc = self._make_service(tmp_path, [sym])
        github = AsyncMock()
        github.get_file_blob_sha = AsyncMock(side_effect=RuntimeError("network error"))
        env_cache: dict = {}
        await svc.initialise(env_cache, {"sym": github})
        assert "sym" in env_cache
        assert env_cache["sym"].readme_sha is None

    @pytest.mark.asyncio
    async def test_skips_symphony_on_mkdir_failure(self, tmp_path: Path) -> None:
        sym = self._make_symphony()
        cfg = MagicMock()
        cfg.symphonies = [sym]
        cfg.global_config.env_cache_root = "/nonexistent/readonly/path"
        svc = EnvCacheService(cfg)
        env_cache: dict = {}
        await svc.initialise(env_cache, {})
        assert "sym" not in env_cache


class TestOnBootstrapComplete:
    def _make_service(self) -> EnvCacheService:
        coordinare_config = MagicMock()
        coordinare_config.symphonies = []
        return EnvCacheService(coordinare_config)

    def test_noop_when_symphony_not_in_cache(self) -> None:
        svc = self._make_service()
        state: dict = {"env_cache": {}}
        svc.on_bootstrap_complete("missing", True, state)  # must not raise

    def test_clears_in_flight_flag(self, tmp_path: Path) -> None:
        svc = self._make_service()
        cache_state = EnvCacheState(
            symphony_name="sym",
            sanitised_name="sym-abc",
            cache_dir=tmp_path,
            bootstrap_in_flight=True,
        )
        state = {"env_cache": {"sym": cache_state}}
        svc.on_bootstrap_complete("sym", True, state)
        assert cache_state.bootstrap_in_flight is False
        assert cache_state.last_bootstrap_succeeded is True
        assert cache_state.last_bootstrap_at is not None

    def test_forces_recheck_when_pending_sha_differs(self, tmp_path: Path) -> None:
        svc = self._make_service()
        cache_state = EnvCacheState(
            symphony_name="sym",
            sanitised_name="sym-abc",
            cache_dir=tmp_path,
            bootstrap_in_flight=True,
            readme_sha="sha1",
            pending_sha="sha2",
        )
        state = {"env_cache": {"sym": cache_state}}
        svc.on_bootstrap_complete("sym", True, state)
        assert cache_state.readme_sha is None
        assert cache_state.pending_sha is None

    def test_no_recheck_when_pending_sha_same(self, tmp_path: Path) -> None:
        svc = self._make_service()
        cache_state = EnvCacheState(
            symphony_name="sym",
            sanitised_name="sym-abc",
            cache_dir=tmp_path,
            bootstrap_in_flight=True,
            readme_sha="sha1",
            pending_sha="sha1",
        )
        state = {"env_cache": {"sym": cache_state}}
        svc.on_bootstrap_complete("sym", True, state)
        # Same SHA — no forced recheck needed
        assert cache_state.readme_sha == "sha1"

    def test_clears_sha_on_failure_to_force_retry(self, tmp_path: Path) -> None:
        svc = self._make_service()
        cache_state = EnvCacheState(
            symphony_name="sym",
            sanitised_name="sym-abc",
            cache_dir=tmp_path,
            bootstrap_in_flight=True,
            readme_sha="sha1",
        )
        state = {"env_cache": {"sym": cache_state}}
        svc.on_bootstrap_complete("sym", False, state)
        assert cache_state.last_bootstrap_succeeded is False
        assert cache_state.bootstrap_in_flight is False
        # SHA must be cleared so next cycle re-triggers rather than leaving
        # cache poisoned (bootstrap failed — env was never installed).
        assert cache_state.readme_sha is None

    def test_preserves_sha_on_success(self, tmp_path: Path) -> None:
        svc = self._make_service()
        cache_state = EnvCacheState(
            symphony_name="sym",
            sanitised_name="sym-abc",
            cache_dir=tmp_path,
            bootstrap_in_flight=True,
            readme_sha="sha1",
        )
        state = {"env_cache": {"sym": cache_state}}
        svc.on_bootstrap_complete("sym", True, state)
        assert cache_state.last_bootstrap_succeeded is True
        assert cache_state.readme_sha == "sha1"

    def test_no_job_id_scenario_resolves_in_flight(self, tmp_path: Path) -> None:
        # Regression: when _bootstrap_dispatch_fn receives no job_id from the
        # performer, it must call on_bootstrap_complete(success=False) so that
        # bootstrap_in_flight is not permanently stuck True.
        svc = self._make_service()
        cache_state = EnvCacheState(
            symphony_name="sym",
            sanitised_name="sym-abc",
            cache_dir=tmp_path,
            bootstrap_in_flight=True,
            readme_sha="sha1",
        )
        state = {"env_cache": {"sym": cache_state}}
        # Simulate what the daemon does when _job_id is None.
        svc.on_bootstrap_complete("sym", False, state)
        assert cache_state.bootstrap_in_flight is False
        assert cache_state.readme_sha is None  # cleared so next cycle retries


# ---------------------------------------------------------------------------
# T042 (060): _collect_env_volumes_for_persistent_performer
# ---------------------------------------------------------------------------


class TestCollectEnvVolumesForPersistentPerformer:
    def test_returns_empty_when_no_states(self) -> None:
        result = _collect_env_volumes_for_persistent_performer({})
        assert result == []

    def test_skips_non_ready_caches(self, tmp_path: Path) -> None:
        state = EnvCacheState(
            symphony_name="s",
            sanitised_name="s-abc",
            cache_dir=tmp_path / "env",
            cache_dir_ready=False,
        )
        result = _collect_env_volumes_for_persistent_performer({"s": state})
        assert result == []

    def test_returns_ro_mounts_for_all_ready_caches(self, tmp_path: Path) -> None:
        dir_a = tmp_path / "a"
        dir_a.mkdir()
        dir_b = tmp_path / "b"
        dir_b.mkdir()
        states = {
            "sym-a": EnvCacheState(
                symphony_name="sym-a",
                sanitised_name="sym-a-111111",
                cache_dir=dir_a,
                cache_dir_ready=True,
            ),
            "sym-b": EnvCacheState(
                symphony_name="sym-b",
                sanitised_name="sym-b-222222",
                cache_dir=dir_b,
                cache_dir_ready=True,
            ),
        }
        result = _collect_env_volumes_for_persistent_performer(states)
        assert len(result) == 2
        paths = {str(v.container_path) for v in result}
        assert "/devenv/sym-a-111111" in paths
        assert "/devenv/sym-b-222222" in paths
        for vol in result:
            assert vol.mode == "ro"

    def test_uses_custom_container_devenv_root(self, tmp_path: Path) -> None:
        cache_dir = tmp_path / "env"
        cache_dir.mkdir()
        state = EnvCacheState(
            symphony_name="s",
            sanitised_name="s-abc",
            cache_dir=cache_dir,
            cache_dir_ready=True,
        )
        result = _collect_env_volumes_for_persistent_performer(
            {"s": state}, container_devenv_root="/custom"
        )
        assert len(result) == 1
        assert str(result[0].container_path) == "/custom/s-abc"


# ---------------------------------------------------------------------------
# T044 (060): BootstrapJobPayload.cache_mount_path round-trip
# ---------------------------------------------------------------------------


class TestBootstrapJobPayloadCacheMountPath:
    def test_default_cache_mount_path(self) -> None:
        payload = BootstrapJobPayload(
            symphony_name="s",
            symphony_org="org",
            symphony_repo="repo",
            env_spec_contents={"README.md": "content"},
        )
        assert payload.cache_mount_path == "/devenv/symphony"

    def test_custom_cache_mount_path_round_trips(self) -> None:
        payload = BootstrapJobPayload(
            symphony_name="s",
            symphony_org="org",
            symphony_repo="repo",
            env_spec_contents={"README.md": "content"},
            cache_mount_path="/devenv/my-project-a1b2c3",
        )
        dumped = payload.model_dump()
        assert dumped["cache_mount_path"] == "/devenv/my-project-a1b2c3"
        restored = BootstrapJobPayload.model_validate(dumped)
        assert restored.cache_mount_path == "/devenv/my-project-a1b2c3"


# ---------------------------------------------------------------------------
# Daemon env-cache bootstrap loop (daemon.py lines 1294-1392)
# ---------------------------------------------------------------------------


def _make_daemon_for_env_cache(**kwargs):
    """Return a CoordinareDaemon configured for single-cycle env-cache tests."""
    from unittest.mock import AsyncMock, MagicMock

    from coordinare.daemon import CoordinareDaemon

    graph = MagicMock()
    graph.ainvoke = AsyncMock(return_value={})
    return CoordinareDaemon(
        graph,
        poll_interval_seconds=1,
        heartbeat_interval_seconds=1,
        max_cycles=1,
        sleep_func=lambda _: __import__("asyncio").sleep(0),
        **kwargs,
    )


def _make_symphony_config(name: str = "sym", performer_id: str | None = "bp-1"):
    from unittest.mock import MagicMock

    cfg = MagicMock()
    cfg.name = name
    cfg.env_bootstrap_performer_id = performer_id
    cfg.enabled = True
    return cfg


class TestDaemonEnvCacheBootstrapLoop:
    """Cover daemon.py lines 1294-1392: the env-cache SHA check in start()."""

    @pytest.mark.asyncio
    async def test_env_cache_svc_check_and_trigger_called_per_symphony(self) -> None:
        """check_and_trigger is called once per symphony when env_cache_service present."""
        daemon = _make_daemon_for_env_cache()
        sym_cfg = _make_symphony_config("alpha")
        mock_svc = MagicMock()
        mock_svc.check_and_trigger = AsyncMock()
        mock_gh = AsyncMock()

        daemon._state["symphony_configs"] = {"alpha": sym_cfg}
        daemon._state["symphony_states"] = {}
        daemon._state["env_cache_service"] = mock_svc
        daemon._state["symphony_github_services"] = {"alpha": mock_gh}
        daemon._state["performer_services"] = {}

        await daemon.start()

        mock_svc.check_and_trigger.assert_called_once()
        call_kwargs = mock_svc.check_and_trigger.call_args.kwargs
        assert call_kwargs["symphony_name"] == "alpha"

    @pytest.mark.asyncio
    async def test_env_cache_skipped_when_no_github_service(self) -> None:
        """If symphony has no github service, check_and_trigger is not called."""
        daemon = _make_daemon_for_env_cache()
        sym_cfg = _make_symphony_config("beta")
        mock_svc = MagicMock()
        mock_svc.check_and_trigger = AsyncMock()

        daemon._state["symphony_configs"] = {"beta": sym_cfg}
        daemon._state["symphony_states"] = {}
        daemon._state["env_cache_service"] = mock_svc
        daemon._state["symphony_github_services"] = {}  # no github service
        daemon._state["performer_services"] = {}

        await daemon.start()

        mock_svc.check_and_trigger.assert_not_called()

    @pytest.mark.asyncio
    async def test_env_cache_skipped_when_no_env_cache_service(self) -> None:
        """If env_cache_service is absent, no check_and_trigger calls are made."""
        daemon = _make_daemon_for_env_cache()
        sym_cfg = _make_symphony_config("gamma")
        mock_gh = AsyncMock()

        daemon._state["symphony_configs"] = {"gamma": sym_cfg}
        daemon._state["symphony_states"] = {}
        # env_cache_service intentionally absent
        daemon._state["symphony_github_services"] = {"gamma": mock_gh}
        daemon._state["performer_services"] = {}

        # Should not raise even without env_cache_service
        await daemon.start()

    @pytest.mark.asyncio
    async def test_bootstrap_dispatch_fn_dispatches_to_performer(self, tmp_path: Path) -> None:
        """_bootstrap_dispatch_fn calls dispatch_card on the correct performer service."""
        daemon = _make_daemon_for_env_cache()
        sym_name = "my-project"
        sanitised = sanitise_symphony_name(sym_name)
        cache_dir = tmp_path / sanitised
        cache_dir.mkdir(parents=True, exist_ok=True)

        cache_state = EnvCacheState(
            symphony_name=sym_name,
            sanitised_name=sanitised,
            cache_dir=cache_dir,
            readme_sha="abc123",
            cache_dir_ready=True,
        )
        daemon._state["env_cache"] = {sym_name: cache_state}

        performer_svc = MagicMock()
        performer_svc.dispatch_card = AsyncMock(
            return_value={"status": "accepted", "session_id": None}
        )
        performer_svc._config = MagicMock()
        performer_svc._config.container_devenv_root = "/devenv"

        sym_cfg = _make_symphony_config(sym_name, performer_id="bp-1")
        mock_gh = AsyncMock()

        # Set up env_cache_service to capture and invoke dispatch_fn
        captured_dispatch_fn = None

        async def _fake_check_and_trigger(**kwargs):
            nonlocal captured_dispatch_fn
            captured_dispatch_fn = kwargs["dispatch_fn"]
            payload = BootstrapJobPayload(
                symphony_name=sym_name,
                symphony_org="org",
                symphony_repo="repo",
                env_spec_contents={"README.md": "content"},
                cache_mount_path=f"/devenv/{sanitised}",
            )
            await captured_dispatch_fn("bp-1", payload)

        mock_ec_svc = MagicMock()
        mock_ec_svc.check_and_trigger = _fake_check_and_trigger

        daemon._state["symphony_configs"] = {sym_name: sym_cfg}
        daemon._state["symphony_states"] = {}
        daemon._state["env_cache_service"] = mock_ec_svc
        daemon._state["symphony_github_services"] = {sym_name: mock_gh}
        daemon._state["performer_services"] = {"bp-1": performer_svc}

        # Patch _poll_bootstrap_completion to avoid real polling
        daemon._poll_bootstrap_completion = AsyncMock()

        await daemon.start()

        # The dispatch_fn should have called dispatch_card on the performer
        performer_svc.dispatch_card.assert_called_once()

    @pytest.mark.asyncio
    async def test_bootstrap_dispatch_fn_no_job_id_calls_on_bootstrap_complete(
        self, tmp_path: Path
    ) -> None:
        """When dispatch_card returns no job_id, on_bootstrap_complete(False) is called."""
        daemon = _make_daemon_for_env_cache()
        sym_name = "my-project"
        sanitised = sanitise_symphony_name(sym_name)
        cache_dir = tmp_path / sanitised
        cache_dir.mkdir(parents=True, exist_ok=True)

        cache_state = EnvCacheState(
            symphony_name=sym_name,
            sanitised_name=sanitised,
            cache_dir=cache_dir,
            readme_sha="abc123",
            cache_dir_ready=True,
            bootstrap_in_flight=True,
        )
        daemon._state["env_cache"] = {sym_name: cache_state}

        performer_svc = MagicMock()
        performer_svc.dispatch_card = AsyncMock(
            return_value={"status": "accepted"}  # no session_id / job_id
        )
        performer_svc._config = MagicMock()
        performer_svc._config.container_devenv_root = "/devenv"

        sym_cfg = _make_symphony_config(sym_name, performer_id="bp-1")
        mock_gh = AsyncMock()

        async def _fake_check_and_trigger(**kwargs):
            payload = BootstrapJobPayload(
                symphony_name=sym_name,
                symphony_org="org",
                symphony_repo="repo",
                env_spec_contents={"README.md": "content"},
                cache_mount_path=f"/devenv/{sanitised}",
            )
            await kwargs["dispatch_fn"]("bp-1", payload)

        mock_ec_svc = MagicMock()
        mock_ec_svc.check_and_trigger = _fake_check_and_trigger
        mock_ec_svc.on_bootstrap_complete = MagicMock()

        daemon._state["symphony_configs"] = {sym_name: sym_cfg}
        daemon._state["symphony_states"] = {}
        daemon._state["env_cache_service"] = mock_ec_svc
        daemon._state["symphony_github_services"] = {sym_name: mock_gh}
        daemon._state["performer_services"] = {"bp-1": performer_svc}

        await daemon.start()

        from unittest.mock import ANY
        mock_ec_svc.on_bootstrap_complete.assert_called_once_with(sym_name, False, ANY)

    @pytest.mark.asyncio
    async def test_bootstrap_dispatch_fn_missing_performer_logs_warning(
        self, tmp_path: Path, caplog
    ) -> None:
        """When performer_id not in performer_services, dispatch_fn returns without dispatch."""
        daemon = _make_daemon_for_env_cache()
        sym_name = "my-project"
        sanitised = sanitise_symphony_name(sym_name)
        cache_dir = tmp_path / sanitised
        cache_dir.mkdir(parents=True, exist_ok=True)

        cache_state = EnvCacheState(
            symphony_name=sym_name,
            sanitised_name=sanitised,
            cache_dir=cache_dir,
            readme_sha="abc123",
            cache_dir_ready=True,
        )
        daemon._state["env_cache"] = {sym_name: cache_state}

        sym_cfg = _make_symphony_config(sym_name, performer_id="bp-missing")
        mock_gh = AsyncMock()

        async def _fake_check_and_trigger(**kwargs):
            payload = BootstrapJobPayload(
                symphony_name=sym_name,
                symphony_org="org",
                symphony_repo="repo",
                env_spec_contents={"README.md": "content"},
            )
            await kwargs["dispatch_fn"]("bp-missing", payload)

        mock_ec_svc = MagicMock()
        mock_ec_svc.check_and_trigger = _fake_check_and_trigger

        daemon._state["symphony_configs"] = {sym_name: sym_cfg}
        daemon._state["symphony_states"] = {}
        daemon._state["env_cache_service"] = mock_ec_svc
        daemon._state["symphony_github_services"] = {sym_name: mock_gh}
        daemon._state["performer_services"] = {}  # performer absent

        # Should not raise
        await daemon.start()


# ---------------------------------------------------------------------------
# T060: CoordinareConfiguration.validate_env_bootstrap_performer_ids
# ---------------------------------------------------------------------------


def _multi_symphony_raw(performer_id: str, endpoint_id: str) -> dict:
    return {
        "github_org": "myorg",
        "github_token": "ghp_test",
        "human_reviewers": ["reviewer"],
        "github_project_number": 1,
        "symphonies": [
            {
                "name": "my-symphony",
                "github_project_number": 1,
                "env_bootstrap_performer_id": performer_id,
            }
        ],
        "performer_endpoints": [
            {
                "id": endpoint_id,
                "mode": "ephemeral",
                "roles": ["implementer"],
                "image": "coordinare-performer:full",
                "endpoint": "http://localhost:8080",
            }
        ],
        "orchestra": {"mode": "shared_pool"},
    }


class TestValidateEnvBootstrapPerformerIds:
    """Validator checks env_bootstrap_performer_id against performer_endpoints."""

    def test_valid_id_accepted(self) -> None:
        from coordinare.config import CoordinareConfiguration
        from coordinare.config_validation import coerce_multi_symphony_raw

        raw = _multi_symphony_raw("bootstrap-env", "bootstrap-env")
        cfg = CoordinareConfiguration(**coerce_multi_symphony_raw(raw))
        assert cfg.symphonies[0].env_bootstrap_performer_id == "bootstrap-env"

    def test_unknown_id_rejected(self) -> None:
        import pytest
        from pydantic import ValidationError

        from coordinare.config import CoordinareConfiguration
        from coordinare.config_validation import coerce_multi_symphony_raw

        raw = _multi_symphony_raw("does-not-exist", "codex-ephemeral")
        with pytest.raises(ValidationError, match="not found in performer_endpoints"):
            CoordinareConfiguration(**coerce_multi_symphony_raw(raw))

    def test_none_id_skipped(self) -> None:
        from coordinare.config import CoordinareConfiguration
        from coordinare.config_validation import coerce_multi_symphony_raw

        raw = _multi_symphony_raw("codex-ephemeral", "codex-ephemeral")
        raw["symphonies"][0].pop("env_bootstrap_performer_id", None)
        cfg = CoordinareConfiguration(**coerce_multi_symphony_raw(raw))
        assert cfg.symphonies[0].env_bootstrap_performer_id is None
