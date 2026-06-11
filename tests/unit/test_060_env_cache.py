"""Tests for spec 060: performer environment caching."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import ANY, AsyncMock, MagicMock

import pytest

from coordinare.models.env_cache import BootstrapJobPayload, EnvCacheState
from coordinare.services.env_cache import (
    BOOTSTRAP_RETRY_COOLDOWN_S,
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
        (cache_dir / "activate.sh").touch()
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

    def test_returns_rw_volume_for_bootstrap_when_cache_not_ready(
        self, tmp_path: Path
    ) -> None:
        """Regression: bootstrap dispatch must mount the host cache rw even
        when cache_dir_ready=False — that's the run that populates it.
        Gating bootstrap on cache_dir_ready creates a chicken-and-egg where
        the host cache never gets populated.
        """
        cache_dir = tmp_path / "env"
        cache_dir.mkdir()
        state = EnvCacheState(
            symphony_name="s",
            sanitised_name="s-abc",
            cache_dir=cache_dir,
            cache_dir_ready=False,
        )
        result = get_env_volume_for_symphony("s", {"s": state}, is_bootstrap=True)
        assert result is not None
        vol, _ = result
        assert vol.mode == "rw"
        assert vol.host_path == cache_dir

    def test_returns_none_for_consumer_when_activate_missing(
        self, tmp_path: Path
    ) -> None:
        """Consumer dispatch is held when activate.sh hasn't been written yet —
        the on-disk presence of activate.sh is the authoritative readiness gate,
        independent of the in-process cache_dir_ready flag.
        """
        cache_dir = tmp_path / "env"
        cache_dir.mkdir()
        state = EnvCacheState(
            symphony_name="s",
            sanitised_name="s-abc",
            cache_dir=cache_dir,
            cache_dir_ready=False,
        )
        result = get_env_volume_for_symphony("s", {"s": state}, is_bootstrap=False)
        assert result is None

    def test_returns_ro_volume_when_activate_present_but_flag_false(
        self, tmp_path: Path
    ) -> None:
        """Post-restart cache survival: activate.sh on disk from a prior boot
        is enough to dispatch consumers, even though cache_dir_ready resets to
        False every coordinare restart.
        """
        cache_dir = tmp_path / "env"
        cache_dir.mkdir()
        (cache_dir / "activate.sh").touch()
        state = EnvCacheState(
            symphony_name="s",
            sanitised_name="s-abc",
            cache_dir=cache_dir,
            cache_dir_ready=False,
        )
        result = get_env_volume_for_symphony("s", {"s": state}, is_bootstrap=False)
        assert result is not None
        vol, _ = result
        assert vol.mode == "ro"

    def test_uses_custom_container_devenv_root(self, tmp_path: Path) -> None:
        cache_dir = tmp_path / "env"
        cache_dir.mkdir()
        (cache_dir / "activate.sh").touch()
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
    async def test_retries_when_prior_bootstrap_failed_and_cooldown_elapsed(
        self, tmp_path: Path
    ) -> None:
        """077: a FAILED bootstrap (readme_sha == current_sha, succeeded=False)
        must auto-retry once the cooldown has elapsed — otherwise the
        consumer-gate deadlocks waiting on a success that never re-fires."""
        svc, _ = self._make_service()
        cache_dir = tmp_path / "env"
        cache_dir.mkdir()
        cache_state = EnvCacheState(
            symphony_name="sym",
            sanitised_name="sym-abc",
            cache_dir=cache_dir,
            readme_sha=_combined_sha({"README.md": "same_sha"}),
            last_bootstrap_succeeded=False,
            last_bootstrap_at=datetime.now(UTC)
            - timedelta(seconds=BOOTSTRAP_RETRY_COOLDOWN_S + 5),
        )
        sym_cfg = self._make_symphony_config()
        sym_cfg.effective_config = MagicMock(return_value=self._make_eff_config())
        github = AsyncMock()
        github.get_file_blob_sha = AsyncMock(return_value="same_sha")
        github.get_file_content = AsyncMock(return_value="# README content")
        dispatch_fn = AsyncMock()

        state: dict = {"env_cache": {"sym": cache_state}, "config": MagicMock()}
        await svc.check_and_trigger("sym", sym_cfg, github, state, dispatch_fn)

        dispatch_fn.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_retry_when_prior_failure_within_cooldown(
        self, tmp_path: Path
    ) -> None:
        """077: a recently-failed bootstrap must NOT relaunch a container on the
        very next poll cycle — the cooldown throttles retries."""
        svc, _ = self._make_service()
        cache_dir = tmp_path / "env"
        cache_dir.mkdir()
        cache_state = EnvCacheState(
            symphony_name="sym",
            sanitised_name="sym-abc",
            cache_dir=cache_dir,
            readme_sha=_combined_sha({"README.md": "same_sha"}),
            last_bootstrap_succeeded=False,
            last_bootstrap_at=datetime.now(UTC) - timedelta(seconds=5),
        )
        sym_cfg = self._make_symphony_config()
        sym_cfg.effective_config = MagicMock(return_value=self._make_eff_config())
        github = AsyncMock()
        github.get_file_blob_sha = AsyncMock(return_value="same_sha")
        dispatch_fn = AsyncMock()

        state: dict = {"env_cache": {"sym": cache_state}, "config": MagicMock()}
        await svc.check_and_trigger("sym", sym_cfg, github, state, dispatch_fn)

        dispatch_fn.assert_not_called()

    @pytest.mark.asyncio
    async def test_synchronous_dispatch_failure_does_not_wedge_in_flight(
        self, tmp_path: Path
    ) -> None:
        """077: when dispatch fails SYNCHRONOUSLY (e.g. readiness timeout) the
        daemon calls on_bootstrap_complete(False) inside dispatch_fn, clearing
        in_flight. _do_dispatch must NOT then re-set in_flight=True — that would
        wedge it forever with no container (stale-in-flight deadlock)."""
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
        state: dict = {"env_cache": {"sym": cache_state}, "config": global_cfg}

        async def failing_dispatch(performer_id: str, payload: object) -> None:
            # Mirror the daemon: a synchronous dispatch failure reports completion
            # itself (no job id) rather than raising.
            svc.on_bootstrap_complete("sym", False, state, error="readiness timeout")

        await svc.check_and_trigger("sym", sym_cfg, github, state, failing_dispatch)

        assert cache_state.bootstrap_in_flight is False  # not wedged
        assert cache_state.last_bootstrap_succeeded is False

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
        # 061: cache_dir_ready must NOT be set on initialise — only after
        # a bootstrap actually succeeds, otherwise performers mount an empty
        # directory and find no installed tools.
        assert env_cache["sym"].cache_dir_ready is False

    @pytest.mark.asyncio
    async def test_does_not_seed_sha_or_mark_ready_when_github_available(
        self, tmp_path: Path
    ) -> None:
        """061: initialise must leave readme_sha=None even when github is available.

        Seeding the SHA on init causes check_and_trigger to skip the first
        bootstrap (current_sha == cache_state.readme_sha), so the cache stays
        empty and performers mount nothing. The first check_and_trigger cycle
        is now responsible for fetching the SHA and dispatching the bootstrap.
        """
        sym = self._make_symphony()
        svc = self._make_service(tmp_path, [sym])
        github = AsyncMock()
        github.get_file_blob_sha = AsyncMock(return_value="abc123")
        env_cache: dict = {}
        await svc.initialise(env_cache, {"sym": github})
        assert env_cache["sym"].readme_sha is None
        assert env_cache["sym"].cache_dir_ready is False
        # github SHA fetch must not be called during init
        github.get_file_blob_sha.assert_not_called()

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

    def test_records_error_on_failure_and_clears_on_success(self, tmp_path: Path) -> None:
        """077: a failed bootstrap stores last_bootstrap_error (for the dashboard);
        a successful one clears it."""
        svc = self._make_service()
        cache_state = EnvCacheState(
            symphony_name="sym",
            sanitised_name="sym-abc",
            cache_dir=tmp_path,
            bootstrap_in_flight=True,
        )
        state = {"env_cache": {"sym": cache_state}}
        svc.on_bootstrap_complete("sym", False, state, error="verify failed: chromium missing")
        assert cache_state.last_bootstrap_succeeded is False
        assert cache_state.last_bootstrap_error == "verify failed: chromium missing"
        # A later success clears the error.
        svc.on_bootstrap_complete("sym", True, state)
        assert cache_state.last_bootstrap_succeeded is True
        assert cache_state.last_bootstrap_error is None

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

    def test_marks_cache_ready_only_on_success(self, tmp_path: Path) -> None:
        """061: cache_dir_ready must flip True only after a successful bootstrap.

        Before this fix, initialise() set cache_dir_ready=True unconditionally,
        causing performers to mount empty cache directories. The flag must
        gate on actual bootstrap success.
        """
        svc = self._make_service()
        cache_state = EnvCacheState(
            symphony_name="sym",
            sanitised_name="sym-abc",
            cache_dir=tmp_path,
            cache_dir_ready=False,
        )
        state = {"env_cache": {"sym": cache_state}}
        svc.on_bootstrap_complete("sym", True, state)
        assert cache_state.cache_dir_ready is True

    def test_does_not_mark_cache_ready_on_failure(self, tmp_path: Path) -> None:
        svc = self._make_service()
        cache_state = EnvCacheState(
            symphony_name="sym",
            sanitised_name="sym-abc",
            cache_dir=tmp_path,
            cache_dir_ready=False,
        )
        state = {"env_cache": {"sym": cache_state}}
        svc.on_bootstrap_complete("sym", False, state)
        assert cache_state.cache_dir_ready is False

    def test_keeps_cache_ready_true_across_bootstrap_failure(self, tmp_path: Path) -> None:
        """A failed re-bootstrap on an already-populated cache must NOT
        invalidate the cache — better to serve a stale cache than nothing.
        """
        svc = self._make_service()
        cache_state = EnvCacheState(
            symphony_name="sym",
            sanitised_name="sym-abc",
            cache_dir=tmp_path,
            cache_dir_ready=True,
            readme_sha="sha1",
        )
        state = {"env_cache": {"sym": cache_state}}
        svc.on_bootstrap_complete("sym", False, state)
        assert cache_state.cache_dir_ready is True

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

    def test_skips_caches_without_activate(self, tmp_path: Path) -> None:
        """Caches without an on-disk activate.sh are skipped — no toolchain
        means no usable mount.
        """
        cache_dir = tmp_path / "env"
        cache_dir.mkdir()
        state = EnvCacheState(
            symphony_name="s",
            sanitised_name="s-abc",
            cache_dir=cache_dir,
            cache_dir_ready=False,
        )
        result = _collect_env_volumes_for_persistent_performer({"s": state})
        assert result == []

    def test_returns_ro_mounts_for_all_ready_caches(self, tmp_path: Path) -> None:
        dir_a = tmp_path / "a"
        dir_a.mkdir()
        (dir_a / "activate.sh").touch()
        dir_b = tmp_path / "b"
        dir_b.mkdir()
        (dir_b / "activate.sh").touch()
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
        (cache_dir / "activate.sh").touch()
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
        daemon._state["performer_services_by_id"] = {}

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
        daemon._state["performer_services_by_id"] = {}

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
        daemon._state["performer_services_by_id"] = {}

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
        daemon._state["performer_services_by_id"] = {"bp-1": performer_svc}

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
        daemon._state["performer_services_by_id"] = {"bp-1": performer_svc}

        await daemon.start()

        from unittest.mock import ANY
        mock_ec_svc.on_bootstrap_complete.assert_called_once_with(sym_name, False, ANY, error=ANY)

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
        daemon._state["performer_services_by_id"] = {}  # performer absent

        # Should not raise
        await daemon.start()


class TestPollBootstrapCompletion:
    """Cover daemon.py:1145-1270 — _poll_bootstrap_completion success/failure paths."""

    @pytest.fixture(autouse=True)
    def _fast_sleep(self, monkeypatch):
        """Patch asyncio.sleep in the daemon module to avoid real 10s delays."""
        import coordinare.daemon as daemon_mod

        async def _no_sleep(_):
            return None

        monkeypatch.setattr(daemon_mod.asyncio, "sleep", _no_sleep)

    @pytest.mark.asyncio
    async def test_success_path_calls_on_bootstrap_complete_true(self) -> None:
        daemon = _make_daemon_for_env_cache()
        svc = MagicMock()
        svc.check_status = AsyncMock(return_value={"status": "env_bootstrap_complete"})
        ec_svc = MagicMock()
        ec_svc.on_bootstrap_complete = MagicMock()
        # No persistent performers — skip the warning branch import side-effects
        daemon._state["performer_services"] = {}

        await daemon._poll_bootstrap_completion(svc, "job-1", "alpha", ec_svc)

        ec_svc.on_bootstrap_complete.assert_called_once()
        args = ec_svc.on_bootstrap_complete.call_args.args
        assert args[0] == "alpha"
        assert args[1] is True

    @pytest.mark.asyncio
    async def test_success_with_persistent_performer_logs_warning(self) -> None:
        from coordinare.services.http_performer_service import HTTPPerformerService

        daemon = _make_daemon_for_env_cache()
        svc = MagicMock()
        svc.check_status = AsyncMock(return_value={"status": "ok"})
        ec_svc = MagicMock()

        # Persistent HTTPPerformerService instance triggers the warning loop branch.
        persistent = MagicMock(spec=HTTPPerformerService)
        persistent.mode = "persistent"
        daemon._state["performer_services"] = {"perf-1": persistent}

        await daemon._poll_bootstrap_completion(svc, "job-1", "alpha", ec_svc)

        ec_svc.on_bootstrap_complete.assert_called_once()
        assert ec_svc.on_bootstrap_complete.call_args.args[1] is True

    @pytest.mark.asyncio
    async def test_failure_status_collects_logs_via_get_agent_logs(self) -> None:
        daemon = _make_daemon_for_env_cache()
        svc = MagicMock()
        svc.check_status = AsyncMock(
            return_value={"status": "error", "error": "boom", "availability": "stopped"}
        )
        # No container_id — falls through to get_agent_logs branch
        svc.get_agent_logs = MagicMock(return_value=["line-a", "line-b"])
        # Simulate no _active_jobs container_id either
        svc._active_jobs = {}
        ec_svc = MagicMock()

        await daemon._poll_bootstrap_completion(svc, "job-1", "alpha", ec_svc)

        ec_svc.on_bootstrap_complete.assert_called_once()
        assert ec_svc.on_bootstrap_complete.call_args.args[1] is False

    @pytest.mark.asyncio
    async def test_failure_get_agent_logs_string_split(self) -> None:
        daemon = _make_daemon_for_env_cache()
        svc = MagicMock()
        svc.check_status = AsyncMock(return_value={"status": "failed"})
        svc.get_agent_logs = MagicMock(return_value="line-1\nline-2\nline-3")
        svc._active_jobs = {}
        ec_svc = MagicMock()

        await daemon._poll_bootstrap_completion(svc, "job-1", "alpha", ec_svc)

        ec_svc.on_bootstrap_complete.assert_called_once_with("alpha", False, daemon._state, error=ANY)

    @pytest.mark.asyncio
    async def test_failure_get_agent_logs_raises(self) -> None:
        daemon = _make_daemon_for_env_cache()
        svc = MagicMock()
        svc.check_status = AsyncMock(return_value={"status": "failed"})
        svc.get_agent_logs = MagicMock(side_effect=RuntimeError("boom"))
        svc._active_jobs = {}
        ec_svc = MagicMock()

        await daemon._poll_bootstrap_completion(svc, "job-1", "alpha", ec_svc)

        ec_svc.on_bootstrap_complete.assert_called_once_with("alpha", False, daemon._state, error=ANY)

    @pytest.mark.asyncio
    async def test_budget_exhausted_reaps_container_and_marks_failure(self, monkeypatch) -> None:
        """076 (live QA #150): a bootstrap that never reports terminal status
        must be bounded by ``bootstrap_max_seconds`` — the loop stops at the
        derived attempt cap, reaps the container, and marks failure (which
        unblocks dispatch; the next cycle re-dispatches).
        """
        import coordinare.daemon as daemon_mod

        daemon = _make_daemon_for_env_cache()
        # 20s budget / 10s poll = 2 attempts, then the timeout/reap path.
        cfg = MagicMock()
        cfg.bootstrap_max_seconds = 20
        cfg.bootstrap_idle_timeout_seconds = 0  # disable idle reap — testing the wall-clock path
        daemon._state["coordinare_config"] = cfg

        svc = MagicMock()
        svc.check_status = AsyncMock(return_value={"status": "working"})  # never terminal
        ec_svc = MagicMock()

        # Fake docker subprocesses (logs snapshot each iter + the final reap).
        stop_calls: list[tuple] = []

        class _FakeProc:
            def __init__(self, argv):
                self._argv = argv

            async def communicate(self):
                return (b"", b"")

            async def wait(self):
                return 0

        async def _fake_exec(*argv, **kwargs):
            if "stop" in argv:
                stop_calls.append(argv)
            return _FakeProc(argv)

        monkeypatch.setattr(daemon_mod.asyncio, "create_subprocess_exec", _fake_exec)

        await daemon._poll_bootstrap_completion(
            svc, "job-1", "alpha", ec_svc, container_id="ctr-hung-deadbeef"
        )

        # check_status polled exactly the budgeted number of times, never more.
        assert svc.check_status.await_count == 2
        # Container was explicitly reaped (docker stop) at the cap.
        assert any("stop" in argv and "ctr-hung-deadbeef" in argv for argv in stop_calls)
        # Failure recorded so the cache stays un-ready and dispatch unblocks.
        ec_svc.on_bootstrap_complete.assert_called_once_with("alpha", False, daemon._state, error=ANY)

    @pytest.mark.asyncio
    async def test_sub_10s_budget_clamps_to_one_attempt(self, monkeypatch) -> None:
        """076: a budget under one poll interval (10s) clamps to 1 attempt
        rather than 0 (which would skip polling entirely)."""
        import coordinare.daemon as daemon_mod

        daemon = _make_daemon_for_env_cache()
        cfg = MagicMock()
        cfg.bootstrap_max_seconds = 5  # 5 // 10 == 0 -> clamped to 1
        cfg.bootstrap_idle_timeout_seconds = 0
        daemon._state["coordinare_config"] = cfg

        svc = MagicMock()
        svc.check_status = AsyncMock(return_value={"status": "working"})
        ec_svc = MagicMock()

        async def _fake_exec(*argv, **kwargs):
            class _P:
                async def communicate(self):
                    return (b"", b"")

                async def wait(self):
                    return 0

            return _P()

        monkeypatch.setattr(daemon_mod.asyncio, "create_subprocess_exec", _fake_exec)

        await daemon._poll_bootstrap_completion(svc, "j", "alpha", ec_svc, container_id="c")

        assert svc.check_status.await_count == 1
        ec_svc.on_bootstrap_complete.assert_called_once_with("alpha", False, daemon._state, error=ANY)

    @pytest.mark.asyncio
    async def test_reap_failure_is_non_fatal(self, monkeypatch) -> None:
        """076: if the docker-stop reap itself fails, the bootstrap is still
        marked failed (the symphony must not stay wedged on a reap error)."""
        import coordinare.daemon as daemon_mod

        daemon = _make_daemon_for_env_cache()
        cfg = MagicMock()
        cfg.bootstrap_max_seconds = 10  # 1 attempt
        cfg.bootstrap_idle_timeout_seconds = 0
        daemon._state["coordinare_config"] = cfg

        svc = MagicMock()
        svc.check_status = AsyncMock(return_value={"status": "working"})
        ec_svc = MagicMock()

        async def _fake_exec(*argv, **kwargs):
            if "stop" in argv:
                raise RuntimeError("docker daemon unreachable")

            class _P:
                async def communicate(self):
                    return (b"", b"")

                async def wait(self):
                    return 0

            return _P()

        monkeypatch.setattr(daemon_mod.asyncio, "create_subprocess_exec", _fake_exec)

        await daemon._poll_bootstrap_completion(svc, "j", "alpha", ec_svc, container_id="c-stuck")

        ec_svc.on_bootstrap_complete.assert_called_once_with("alpha", False, daemon._state, error=ANY)

    @pytest.mark.asyncio
    async def test_idle_reap_fires_when_no_log_progress(self, monkeypatch) -> None:
        """076 idle reap: a bootstrap whose meaningful log output is frozen for
        bootstrap_idle_timeout_seconds is reaped early — well before the
        wall-clock budget — and marked failed."""
        import coordinare.daemon as daemon_mod

        daemon = _make_daemon_for_env_cache()
        cfg = MagicMock()
        cfg.bootstrap_max_seconds = 3600  # large: wall-clock must NOT fire here
        cfg.bootstrap_idle_timeout_seconds = 30  # 3 idle polls -> reap
        daemon._state["coordinare_config"] = cfg

        svc = MagicMock()
        svc.check_status = AsyncMock(return_value={"status": "working"})  # never terminal
        ec_svc = MagicMock()

        stop_calls: list[tuple] = []

        # Constant, poll-noise-only logs => no meaningful progress between polls.
        frozen_logs = (
            b'INFO:     172.0.0.1:1 - "GET /jobs/j HTTP/1.1" 200 OK\n'
            b'INFO:     172.0.0.1:2 - "GET /jobs/j HTTP/1.1" 200 OK\n'
        )

        async def _fake_exec(*argv, **kwargs):
            if "stop" in argv:
                stop_calls.append(argv)

            class _P:
                async def communicate(self):
                    return (frozen_logs, b"")

                async def wait(self):
                    return 0

            return _P()

        monkeypatch.setattr(daemon_mod.asyncio, "create_subprocess_exec", _fake_exec)

        await daemon._poll_bootstrap_completion(svc, "j", "alpha", ec_svc, container_id="ctr-idle")

        # Reaped on idle (attempts 0,1,2 poll; attempt 3 = 30s idle -> reap before check_status).
        assert svc.check_status.await_count == 3
        assert any("stop" in argv and "ctr-idle" in argv for argv in stop_calls)
        ec_svc.on_bootstrap_complete.assert_called_once_with("alpha", False, daemon._state, error=ANY)

    @pytest.mark.asyncio
    async def test_idle_reap_not_triggered_while_logs_progress(self, monkeypatch) -> None:
        """076 idle reap: a slow-but-progressing bootstrap (new meaningful log
        lines each poll) is NOT reaped even past the idle window — it completes
        normally."""
        import coordinare.daemon as daemon_mod

        daemon = _make_daemon_for_env_cache()
        cfg = MagicMock()
        cfg.bootstrap_max_seconds = 3600
        cfg.bootstrap_idle_timeout_seconds = 30
        daemon._state["coordinare_config"] = cfg
        daemon._state["performer_services"] = {}

        stop_calls: list[tuple] = []
        counter = {"n": 0}

        async def _fake_exec(*argv, **kwargs):
            if "stop" in argv:
                stop_calls.append(argv)

            class _P:
                async def communicate(self):
                    # New meaningful (non-poll) line every iteration -> progress.
                    counter["n"] += 1
                    body = (
                        f'2026-05-29 18:00:0{counter["n"]} [info] claude_code shim request n={counter["n"]}\n'
                        'INFO:     172.0.0.1:1 - "GET /jobs/j HTTP/1.1" 200 OK\n'
                    ).encode()
                    return (body, b"")

                async def wait(self):
                    return 0

            return _P()

        monkeypatch.setattr(daemon_mod.asyncio, "create_subprocess_exec", _fake_exec)

        # Terminal only after 6 polls — well past the 30s idle window — proving
        # progress kept idle-reap from firing.
        statuses = [{"status": "working"}] * 5 + [{"status": "env_bootstrap_complete"}]
        svc = MagicMock()
        svc.check_status = AsyncMock(side_effect=statuses)
        ec_svc = MagicMock()

        await daemon._poll_bootstrap_completion(svc, "j", "alpha", ec_svc, container_id="ctr-slow")

        assert not stop_calls  # never reaped
        ec_svc.on_bootstrap_complete.assert_called_once()
        assert ec_svc.on_bootstrap_complete.call_args.args[1] is True  # completed successfully

    @pytest.mark.asyncio
    async def test_idle_reap_disabled_when_timeout_zero(self, monkeypatch) -> None:
        """076 idle reap: bootstrap_idle_timeout_seconds=0 disables idle reaping
        (wall-clock budget remains the only bound)."""
        import coordinare.daemon as daemon_mod

        daemon = _make_daemon_for_env_cache()
        cfg = MagicMock()
        cfg.bootstrap_max_seconds = 20  # 2 attempts -> wall-clock reap
        cfg.bootstrap_idle_timeout_seconds = 0  # idle disabled
        daemon._state["coordinare_config"] = cfg

        svc = MagicMock()
        svc.check_status = AsyncMock(return_value={"status": "working"})
        ec_svc = MagicMock()

        async def _fake_exec(*argv, **kwargs):
            class _P:
                async def communicate(self):
                    return (b"", b"")  # frozen, but idle disabled => not reaped early

                async def wait(self):
                    return 0

            return _P()

        monkeypatch.setattr(daemon_mod.asyncio, "create_subprocess_exec", _fake_exec)

        await daemon._poll_bootstrap_completion(svc, "j", "alpha", ec_svc, container_id="c")

        # Ran the full 2-attempt wall-clock budget (idle did not short-circuit it).
        assert svc.check_status.await_count == 2
        ec_svc.on_bootstrap_complete.assert_called_once_with("alpha", False, daemon._state, error=ANY)

    @pytest.mark.asyncio
    async def test_check_status_exception_marks_failure(self) -> None:
        daemon = _make_daemon_for_env_cache()
        svc = MagicMock()
        svc.check_status = AsyncMock(side_effect=RuntimeError("connection lost"))
        ec_svc = MagicMock()

        await daemon._poll_bootstrap_completion(svc, "job-1", "alpha", ec_svc)

        ec_svc.on_bootstrap_complete.assert_called_once_with("alpha", False, daemon._state, error=ANY)

    @pytest.mark.asyncio
    async def test_failure_uses_active_jobs_container_id_lookup(self, monkeypatch) -> None:
        """When container_id is None but svc._active_jobs has the job, fallback path runs."""
        daemon = _make_daemon_for_env_cache()
        svc = MagicMock()
        svc.check_status = AsyncMock(return_value={"status": "failed"})
        # No get_agent_logs attribute — force docker logs path that errors out
        del svc.get_agent_logs

        job_obj = MagicMock()
        job_obj.container_id = "abc123"
        svc._active_jobs = {"job-1": job_obj}

        # Stub asyncio.create_subprocess_exec in daemon module to raise so we hit
        # the except-branch in the docker logs fallback.
        import coordinare.daemon as daemon_mod

        async def _fail_subproc(*_a, **_kw):
            raise RuntimeError("docker missing")

        monkeypatch.setattr(daemon_mod.asyncio, "create_subprocess_exec", _fail_subproc)

        ec_svc = MagicMock()
        await daemon._poll_bootstrap_completion(svc, "job-1", "alpha", ec_svc)

        ec_svc.on_bootstrap_complete.assert_called_once_with("alpha", False, daemon._state, error=ANY)

    @pytest.mark.asyncio
    async def test_container_id_logs_snapshot_branch(self, monkeypatch) -> None:
        """Pass container_id so the pre-check_status docker-logs snapshot block runs."""
        daemon = _make_daemon_for_env_cache()
        svc = MagicMock()
        svc.check_status = AsyncMock(return_value={"status": "env_bootstrap_complete"})
        daemon._state["performer_services"] = {}

        # Stub create_subprocess_exec to return a fake proc with snapshot output.
        class _FakeProc:
            async def communicate(self):
                return (b"snap-line-1\nsnap-line-2\n", b"")

        async def _make_proc(*_a, **_kw):
            return _FakeProc()

        import coordinare.daemon as daemon_mod
        monkeypatch.setattr(daemon_mod.asyncio, "create_subprocess_exec", _make_proc)

        ec_svc = MagicMock()
        await daemon._poll_bootstrap_completion(
            svc, "job-1", "alpha", ec_svc, container_id="cid-xyz"
        )

        ec_svc.on_bootstrap_complete.assert_called_once()
        assert ec_svc.on_bootstrap_complete.call_args.args[1] is True


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


def test_env_bootstrap_persona_mandates_idempotent_reinstall() -> None:
    """077: the bootstrap persona must instruct the agent to RE-RUN installs on a
    populated cache (idempotent), so a newly-documented dependency (e.g. a
    headless browser added to the README) gets installed rather than skipped
    because the cache already has a toolchain/activate.sh. Without this, the
    env-cache SHA-change re-bootstrap silently no-ops and never picks up new deps.
    """
    from coordinare.services.http_performer_service import HTTPPerformerService

    card_context = {
        "symphony_org": "ViviDynamics",
        "symphony_repo": "website",
        "cache_mount_path": "/devenv/website",
        "env_spec_files": ["README.md"],
        "env_spec_contents": {
            "README.md": "Install Google Chrome / Chromium for the system tests."
        },
        "backend": "opencode",
    }

    # The method does not use ``self`` — call it with None as the instance.
    payload = HTTPPerformerService._build_env_bootstrap_payload(None, card_context)
    persona = payload.persona.lower()

    # Must mandate re-running idempotent installs on an already-populated cache.
    assert "re-run" in persona
    assert "idempotent" in persona
    assert "already" in persona
    # The spec-file content is embedded so the agent sees the documented dep.
    assert "chrome" in persona
    # 077: system packages must be made self-contained via the cache's debs/ dir
    # (offline-safe), NOT a bare apt-get install that fails silently without apt
    # lists / egress — the bug that left Chromium uninstalled.
    assert "debs/" in persona
    assert "apt-get download" in persona
    assert "silently" in persona
    # 077 Tier 1: agent must write verify.sh and confirm the install before exit.
    assert "verify.sh" in persona
    assert "confirm the installation before exiting" in persona
    # 077: activate.sh is sourced into every shell — it MUST NOT abort (a sourced
    # `exit 1` killed CLI installs and deadlocked bootstraps). Hard assertions go
    # in verify.sh (run standalone), not activate.sh.
    assert "sourced into every shell" in persona
    assert "never abort" in persona
    # 077: pinned language runtimes (e.g. Ruby 3.4.2) are NOT in apt — the agent
    # must install the EXACT pinned version via a version manager / build tool,
    # not accept the distro default (the Ruby-3.3.8-vs-3.4.2 cascade failure).
    assert "pinned" in persona
    assert ".ruby-version" in persona
    assert "ruby-build" in persona
    # 087: captured-deb SHARED LIBRARIES are now activated deterministically by the
    # coordinare-owned profile (it extracts the *.so files and prepends them to
    # LD_LIBRARY_PATH before activate.sh is sourced). So activate.sh no longer needs
    # to `dpkg -i` / `apt-get install` the debs just to make libraries loadable —
    # that is the coordinare's job now (Guardrails for Forgetful Models). A package
    # whose BINARY must be on PATH is extracted with `dpkg-deb -x` and its bin dir
    # prepended to PATH, never `dpkg -i` (needs root, mutates the system).
    assert "ld_library_path" in persona
    assert "dpkg-deb -x" in persona
    # Guard the binary-on-PATH guidance against regression (adversarial review):
    # `dpkg-deb -x` recreates the deb's absolute layout, so binaries land under
    # <prefix>/usr/bin, NOT <prefix>/bin — the persona must say so explicitly or
    # an agent prepends the wrong dir and the binary is never found.
    assert "usr/bin" in persona
    # And it must forbid `dpkg -i` (needs root, mutates the container).
    assert "never `dpkg -i`" in persona


def _bootstrap_card_context(**extra) -> dict:
    ctx = {
        "symphony_org": "ViviDynamics",
        "symphony_repo": "website",
        "cache_mount_path": "/devenv/website",
        "env_spec_files": ["README.md"],
        "env_spec_contents": {"README.md": "Install Ruby 3.4.2 and Chrome."},
        "backend": "opencode",
    }
    ctx.update(extra)
    return ctx


class TestManifestArtifacts:
    """077: coordinare derives the manifest, writes an authoritative verify.sh."""

    @pytest.mark.asyncio
    async def test_writes_verify_and_returns_checklist(self, tmp_path: Path) -> None:
        svc = EnvCacheService(MagicMock())
        files = {
            ".ruby-version": "3.4.2\n",
            "Gemfile": 'gem "rails"\ngem "puma"\n',
            "Gemfile.lock": "BUNDLED WITH\n   2.5.6\n",
        }

        async def fake_get(org: str, repo: str, path: str) -> str | None:
            return files.get(path)

        github = MagicMock()
        github.get_file_content = fake_get
        cache_dir = tmp_path / "cache"

        checklist, verify_provided = await svc._build_manifest_artifacts(
            symphony_name="website",
            repo="website",
            github_org="ViviDynamics",
            github_service=github,
            readme_contents={"README.md": "Rails app."},
            current_sha="abc123",
            cache_dir=cache_dir,
            cache_mount_path="/devenv/website",
            llm_chat=None,
        )

        assert verify_provided is True
        assert "ruby ==3.4.2" in checklist
        verify = (cache_dir / "verify.sh").read_text()
        assert "RUBY_VERSION" in verify and "3.4.2" in verify
        assert (cache_dir / "verify.sh").stat().st_mode & 0o100  # executable
        manifest = (cache_dir / "manifest.json").read_text()
        assert "rails" in manifest and "2.5.6" in manifest

    @pytest.mark.asyncio
    async def test_no_deps_returns_none_false(self, tmp_path: Path) -> None:
        """A project with no parseable pin files → fall back (agent writes verify)."""
        svc = EnvCacheService(MagicMock())

        async def fake_get(org: str, repo: str, path: str) -> str | None:
            return None

        github = MagicMock()
        github.get_file_content = fake_get
        cache_dir = tmp_path / "cache"

        checklist, verify_provided = await svc._build_manifest_artifacts(
            symphony_name="sym",
            repo="sym",
            github_org="org",
            github_service=github,
            readme_contents={"README.md": "no structured deps"},
            current_sha="sha",
            cache_dir=cache_dir,
            cache_mount_path="/devenv/sym",
            llm_chat=None,
        )
        assert checklist is None
        assert verify_provided is False
        assert not (cache_dir / "verify.sh").exists()


def test_env_bootstrap_persona_injects_previous_failure_on_retry() -> None:
    """077 feedback-injection: on a retry, the prior verification failure is put
    FIRST in the persona so the agent fixes the specific gap (e.g. the pinned
    Ruby version) instead of repeating the same miss."""
    from coordinare.services.http_performer_service import HTTPPerformerService

    ctx = _bootstrap_card_context(
        last_failure="version mismatch (expected 3.4.2, got 3.3.8)\nFAIL: Bundler is not available",
    )
    payload = HTTPPerformerService._build_env_bootstrap_payload(None, ctx)
    persona = payload.persona

    assert "RETRY" in persona
    assert "3.4.2" in persona  # the specific failure is injected verbatim
    assert "Bundler is not available" in persona
    # The failure appears BEFORE the generic bootstrap instructions.
    assert persona.index("RETRY") < persona.index("environment bootstrap agent")


def test_env_bootstrap_persona_no_retry_block_on_first_attempt() -> None:
    """No prior failure → no retry block (first/clean bootstrap)."""
    from coordinare.services.http_performer_service import HTTPPerformerService

    payload = HTTPPerformerService._build_env_bootstrap_payload(None, _bootstrap_card_context())
    assert "RETRY" not in payload.persona


def test_env_bootstrap_persona_injects_manifest_checklist() -> None:
    """077: the coordinare-derived checklist is injected (authoritative install list),
    and a coordinare-provided verify.sh flips the persona to 'run it, don't write it'."""
    from coordinare.services.http_performer_service import HTTPPerformerService

    checklist = (
        "AUTHORITATIVE DEPENDENCY CHECKLIST (derived by coordinare from the project files).\n"
        "  - [runtime] ruby ==3.4.2  (language runtime; from .ruby-version)"
    )
    ctx = _bootstrap_card_context(
        dependency_checklist=checklist,
        verify_provided=True,
    )
    payload = HTTPPerformerService._build_env_bootstrap_payload(None, ctx)
    persona = payload.persona

    # Checklist present and ahead of the free-form spec files.
    assert "AUTHORITATIVE DEPENDENCY CHECKLIST" in persona
    assert "ruby ==3.4.2" in persona
    assert persona.index("AUTHORITATIVE DEPENDENCY CHECKLIST") < persona.index("Spec files")
    # verify_provided → coordinare owns verify.sh; agent must not author it.
    assert "coordinare-owned" in persona.lower()
    assert "do not create, overwrite, or delete it" in persona.lower()


def test_env_bootstrap_persona_agent_writes_verify_when_not_provided() -> None:
    """Without a coordinare manifest, the agent is still told to author verify.sh."""
    from coordinare.services.http_performer_service import HTTPPerformerService

    payload = HTTPPerformerService._build_env_bootstrap_payload(None, _bootstrap_card_context())
    persona = payload.persona.lower()
    assert "also write an executable verification script" in persona
    assert "authoritative dependency checklist" not in persona
