"""Tests for spec 060: performer environment caching."""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from unittest.mock import ANY, AsyncMock, MagicMock

import pytest

import coordinare.daemon as daemon_mod
from coordinare.config import EnvCacheConfig
from coordinare.daemon import CoordinareDaemon
from coordinare.models.env_cache import BootstrapJobPayload, EnvCacheState
from coordinare.services.docker_runtime import DockerRuntime
from coordinare.services.env_cache import (
    BOOTSTRAP_RETRY_COOLDOWN_S,
    EnvCacheService,
    _collect_env_volumes_for_persistent_performer,
    get_env_volume_for_symphony,
    sanitise_symphony_name,
    write_service_scripts,
)
from tests.unit.env_manifest_fixture import (
    record_verified_cache_fixture,
    reviewed_metadata,
    verification_output,
)


class _BootstrapClock:
    def __init__(self) -> None:
        self.now = 0.0

    def time(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


def _head(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()[:40]


def _source_fingerprint(per_file: dict[str, str]) -> str:
    """Known source/policy identity used to set up cache lifecycle transitions."""
    head = _head(next(iter(per_file.values())))
    policy = {"schema": "351-v1", "head": head, "image": "trusted/global@sha256:" + "a" * 64,
              "consumer_images": [], "conducting": "None",
              "verifier_image": None, "limits": EnvCacheConfig().model_dump()}
    digest = hashlib.sha256(json.dumps(policy, sort_keys=True).encode()).hexdigest()[:12]
    return f"351-v1:{head}:{digest}"


def _configured(coordinare_config):
    coordinare_config.agent_transport = "docker"
    coordinare_config.performer_image = "trusted/global@sha256:" + "a" * 64
    coordinare_config.global_config.performer_image = "trusted/global@sha256:" + "a" * 64
    coordinare_config.global_config.performer_endpoints = []
    coordinare_config.global_config.conducting = None
    coordinare_config.global_config.kubernetes_verifier_image = None
    coordinare_config.global_config.env_cache = EnvCacheConfig()
    coordinare_config.global_config.bootstrap_max_seconds = 3600
    return coordinare_config


def _accept(cache_state: EnvCacheState, *, nonempty: bool = False) -> EnvCacheState:
    from coordinare_service_inference.environment.models import EnvironmentManifest

    cache_state.bootstrap_commit_sha = cache_state.bootstrap_commit_sha or _head("same_sha")
    metadata = reviewed_metadata(source_commit=cache_state.bootstrap_commit_sha)
    cache_state.environment_capabilities = metadata["environment_capabilities"]
    data = metadata["environment_manifest"]
    data["symphony_name"] = cache_state.symphony_name
    if nonempty:
        data["requirements"] = [{"id": "tool", "description": "Fixture tool usable",
                                 "source": "tools.plan", "checks": ["baseline"], "required": True}]
        data["empty_reason"] = None
    manifest = EnvironmentManifest.model_validate(data)
    cache_state.environment_execution_source = metadata["environment_execution_source"]
    cache_state.environment_manifest = manifest.model_dump()
    from agent.performer.tests.source_contract_fixture import current_review_fixture
    cache_state.environment_assessment = current_review_fixture(manifest,
        {**metadata["environment_assessment"], "manifest_digest": manifest.digest()})
    cache_state.environment_verification = json.loads(verification_output(cache_state.environment_manifest))
    cache_state.environment_verification["image_ref"] = "trusted/global@sha256:" + "a" * 64
    return cache_state


def _accepted_state(**kwargs) -> EnvCacheState:
    if kwargs.get("cache_dir_ready"):
        kwargs.setdefault("last_bootstrap_succeeded", True)
    return _accept(EnvCacheState(**kwargs))


@pytest.fixture(autouse=True)
def _accepted_discovery(monkeypatch):
    """Exercise real publication with a separately tested discovery boundary."""
    from coordinare_service_inference.environment.models import (
        CoverageAssessment,
        EnvironmentManifest,
    )

    async def discover(**kwargs):
        metadata = reviewed_metadata(source_commit=kwargs["commit"], capabilities=kwargs["capabilities"])
        metadata["environment_manifest"]["symphony_name"] = kwargs["symphony_name"]
        manifest = EnvironmentManifest.model_validate(metadata["environment_manifest"])
        from agent.performer.tests.source_contract_fixture import current_review_fixture
        assessment = CoverageAssessment.model_validate(current_review_fixture(manifest,
            {**metadata["environment_assessment"], "manifest_digest": manifest.digest()}))
        return manifest, assessment

    from unittest.mock import Mock

    from coordinare_service_inference.environment.execution_source import (
        ExecutionSourceBundle,
        ExecutionSourceIdentity,
    )

    from coordinare.services.env_cache import EnvCacheService
    from coordinare.services.environment_discovery import discover_and_review as real_discovery
    from tests.unit.test_353_execution_source import bundle_file

    original_build = EnvCacheService._build_manifest_artifacts
    async def build_with_source(self, **kwargs):
        github = kwargs['github_service']
        provider = getattr(github, 'prepare_execution_source', None)
        if provider is None or isinstance(provider, Mock):
            async def source(owner, repo, *, commit, destination, deadline):
                destination.mkdir(parents=True)
                # The pinned bundle must list exactly the paths the provider reports.
                provider = getattr(github, 'get_repository_paths', None)
                listed = getattr(provider, 'return_value', None)
                paths = listed if isinstance(listed, list) and listed else ['tools.plan']
                if isinstance(provider, Mock) and listed is not paths:
                    provider.return_value = paths
                payload, identity = bundle_file(destination, dict.fromkeys(paths, (b'baseline', '100644')), commit=commit)
                return ExecutionSourceBundle(ExecutionSourceIdentity.model_validate(identity), payload,
                    hashlib.sha256(payload.read_bytes()).hexdigest(), payload.stat().st_size)
            github.prepare_execution_source = source
        return await original_build(self, **kwargs)
    monkeypatch.setattr(EnvCacheService, '_build_manifest_artifacts', build_with_source)

    async def inspect_image(image):
        return image, "linux/x86_64"

    monkeypatch.setattr("coordinare.services.environment_capabilities._docker_identity", inspect_image)
    monkeypatch.setattr("coordinare.services.environment_discovery.discover_and_review", discover)
    return real_discovery


def _seed_toolchain(cache_dir: Path) -> None:
    """092: write toolchain content beyond the coordinare-seeded files so
    on_bootstrap_complete's populated-mount invariant treats a success as
    genuine. An empty mount (only the seed set) is now downgraded to failure.
    """
    (cache_dir / "bin").mkdir(parents=True, exist_ok=True)
    (cache_dir / "bin" / "python").write_text("#!/bin/sh\n")


# ---------------------------------------------------------------------------
# T026: sanitise_symphony_name
# ---------------------------------------------------------------------------


# 218: the real quiesce, captured before the class stub replaces it. The idle-reap test restores it.
_ORIGINAL_QUIESCE = CoordinareDaemon._quiesce_bootstrap_container


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

    def test_coordinare_manages_services_field(self) -> None:
        # 116: the field defaults True (preserve managed path for an older/synthetic
        # payload) and round-trips an explicit False set by coordinare from config.
        p = BootstrapJobPayload(
            symphony_name="s",
            symphony_org="org",
            symphony_repo="repo",
            env_spec_contents={"README.md": "content"},
        )
        assert p.coordinare_manages_services is True
        off = BootstrapJobPayload(
            symphony_name="s",
            symphony_org="org",
            symphony_repo="repo",
            env_spec_contents={"README.md": "content"},
            coordinare_manages_services=False,
        )
        assert off.coordinare_manages_services is False
        assert (
            BootstrapJobPayload.model_validate(off.model_dump()).coordinare_manages_services
            is False
        )


# ---------------------------------------------------------------------------
# T027: EnvCacheService unit tests
# ---------------------------------------------------------------------------


# 218: the daemon-rendered activate.sh text. A cache copy counts only when it matches it.
_RENDERED = "# daemon-rendered activate.sh\n"


class TestGetEnvVolumeForSymphony:
    def test_returns_none_when_no_state(self) -> None:
        result = get_env_volume_for_symphony("missing", {}, is_bootstrap=False, performer_mounts=())
        assert result is None

    def test_returns_none_when_cache_dir_missing(self, tmp_path: Path) -> None:
        state = _accepted_state(
            symphony_name="s",
            sanitised_name="s-abc",
            cache_dir=tmp_path / "nonexistent",
        )
        result = get_env_volume_for_symphony("s", {"s": state}, is_bootstrap=False, performer_mounts=())
        assert result is None

    def test_returns_ro_volume_for_regular_performer(self, tmp_path: Path) -> None:
        from coordinare.models.performer_endpoint import VolumeMount

        cache_dir = tmp_path / "env"
        cache_dir.mkdir()
        (cache_dir / "activate.sh").write_text(_RENDERED)
        state = _accepted_state(
            symphony_name="s",
            sanitised_name="s-abc",
            cache_dir=cache_dir,
            cache_dir_ready=True,
            rendered_activate_sh=_RENDERED,
        )
        result = get_env_volume_for_symphony("s", {"s": state}, is_bootstrap=False, performer_mounts=())
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
        state = _accepted_state(
            symphony_name="s",
            sanitised_name="s-abc",
            cache_dir=cache_dir,
            cache_dir_ready=True,
        )
        result = get_env_volume_for_symphony("s", {"s": state}, is_bootstrap=True, performer_mounts=())
        assert result is not None
        vol, _ = result
        assert vol.mode == "rw"

    def test_returns_rw_volume_for_bootstrap_when_cache_not_ready(
        self, tmp_path: Path,
    ) -> None:
        """Regression: bootstrap dispatch must mount the host cache rw even
        when cache_dir_ready=False — that's the run that populates it.
        Gating bootstrap on cache_dir_ready creates a chicken-and-egg where
        the host cache never gets populated.
        """
        cache_dir = tmp_path / "env"
        cache_dir.mkdir()
        state = _accepted_state(
            symphony_name="s",
            sanitised_name="s-abc",
            cache_dir=cache_dir,
            cache_dir_ready=False,
        )
        result = get_env_volume_for_symphony("s", {"s": state}, is_bootstrap=True, performer_mounts=())
        assert result is not None
        vol, _ = result
        assert vol.mode == "rw"
        assert vol.host_path == cache_dir

    def test_returns_none_for_consumer_when_activate_missing(
        self, tmp_path: Path,
    ) -> None:
        """Consumer dispatch is held when activate.sh hasn't been written yet —
        the on-disk presence of activate.sh is the authoritative readiness gate,
        independent of the in-process cache_dir_ready flag.
        """
        cache_dir = tmp_path / "env"
        cache_dir.mkdir()
        state = _accepted_state(
            symphony_name="s",
            sanitised_name="s-abc",
            cache_dir=cache_dir,
            cache_dir_ready=False,
        )
        result = get_env_volume_for_symphony("s", {"s": state}, is_bootstrap=False, performer_mounts=())
        assert result is None

    def test_holds_consumer_when_activate_present_but_ready_flag_false(
        self, tmp_path: Path,
    ) -> None:
        """Activation on disk cannot replace a fresh ready decision after restart."""
        cache_dir = tmp_path / "env"
        cache_dir.mkdir()
        (cache_dir / "activate.sh").write_text(_RENDERED)
        state = _accepted_state(
            symphony_name="s",
            sanitised_name="s-abc",
            cache_dir=cache_dir,
            cache_dir_ready=False,
            rendered_activate_sh=_RENDERED,
        )
        result = get_env_volume_for_symphony("s", {"s": state}, is_bootstrap=False, performer_mounts=())
        assert result is None

    def test_uses_custom_container_devenv_root(self, tmp_path: Path) -> None:
        cache_dir = tmp_path / "env"
        cache_dir.mkdir()
        (cache_dir / "activate.sh").write_text(_RENDERED)
        state = _accepted_state(
            symphony_name="s",
            sanitised_name="s-abc",
            cache_dir=cache_dir,
            cache_dir_ready=True,
            rendered_activate_sh=_RENDERED,
        )
        result = get_env_volume_for_symphony(
            "s", {"s": state}, is_bootstrap=False, performer_mounts=(), container_devenv_root="/custom",
        )
        assert result is not None
        vol, container_path = result
        assert container_path == "/custom/s-abc"
        assert str(vol.container_path) == "/custom/s-abc"


class TestEnvCacheServiceCheckAndTrigger:
    def _make_service(self) -> tuple[EnvCacheService, MagicMock]:
        coordinare_config = _configured(MagicMock())
        coordinare_config.symphonies = []
        svc = EnvCacheService(coordinare_config)
        return svc, coordinare_config

    def _make_symphony_config(self, performer_id: str | None = "bootstrap") -> MagicMock:
        cfg = MagicMock()
        cfg.env_bootstrap_performer_id = performer_id
        cfg.env_spec_files = ["README.md"]
        cfg.name = "test-symphony"
        # 092: no test_env block configured (the common case). A bare MagicMock
        # would yield a truthy .test_env and send _load_test_env_vars down the
        # configured-source path against a mock host_path.
        cfg.test_env = None
        return cfg

    def _make_eff_config(self) -> MagicMock:
        eff = MagicMock()
        eff.github_org = "org"
        eff.project_name = "repo"
        eff.base_branch = "main"
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
        cache_state = _accepted_state(
            symphony_name="sym",
            sanitised_name="sym-abc",
            cache_dir=cache_dir,
            readme_sha="old_sha",
        )
        global_cfg = MagicMock()
        sym_cfg = self._make_symphony_config()
        sym_cfg.effective_config = MagicMock(return_value=self._make_eff_config())
        github = AsyncMock()
        github.get_branch_head_sha = AsyncMock(return_value=_head("new_sha"))
        github.get_file_content = AsyncMock(return_value="# README content")
        dispatch_fn = AsyncMock()

        state: dict = {
            "env_cache": {"sym": cache_state},
            "config": global_cfg,
        }
        await svc.check_and_trigger("sym", sym_cfg, github, state, dispatch_fn)

        dispatch_fn.assert_awaited_once()
        assert cache_state.bootstrap_in_flight is True
        assert cache_state.readme_sha == _source_fingerprint({"README.md": "new_sha"})

    @pytest.mark.asyncio
    async def test_skips_dispatch_when_sha_unchanged(self, tmp_path: Path) -> None:
        svc, _ = self._make_service()
        cache_dir = tmp_path / "env"
        cache_dir.mkdir()
        cache_state = _accepted_state(
            symphony_name="sym",
            sanitised_name="sym-abc",
            cache_dir=cache_dir,
            readme_sha=_source_fingerprint({"README.md": "same_sha"}),
        )
        global_cfg = MagicMock()
        sym_cfg = self._make_symphony_config()
        sym_cfg.effective_config = MagicMock(return_value=self._make_eff_config())
        github = AsyncMock()
        github.get_branch_head_sha = AsyncMock(return_value=_head("same_sha"))
        dispatch_fn = AsyncMock()

        state: dict = {"env_cache": {"sym": cache_state}, "config": global_cfg}
        await svc.check_and_trigger("sym", sym_cfg, github, state, dispatch_fn)

        dispatch_fn.assert_not_called()

    @pytest.mark.asyncio
    async def test_retries_when_prior_bootstrap_failed_and_cooldown_elapsed(
        self, tmp_path: Path,
    ) -> None:
        """077: a FAILED bootstrap (readme_sha == current_sha, succeeded=False)
        must auto-retry once the cooldown has elapsed — otherwise the
        consumer-gate deadlocks waiting on a success that never re-fires."""
        svc, _ = self._make_service()
        cache_dir = tmp_path / "env"
        cache_dir.mkdir()
        cache_state = _accepted_state(
            symphony_name="sym",
            sanitised_name="sym-abc",
            cache_dir=cache_dir,
            readme_sha=_source_fingerprint({"README.md": "same_sha"}),
            last_bootstrap_succeeded=False,
            last_bootstrap_at=datetime.now(UTC)
            - timedelta(seconds=BOOTSTRAP_RETRY_COOLDOWN_S + 5),
        )
        sym_cfg = self._make_symphony_config()
        sym_cfg.effective_config = MagicMock(return_value=self._make_eff_config())
        github = AsyncMock()
        github.get_branch_head_sha = AsyncMock(return_value=_head("same_sha"))
        github.get_file_content = AsyncMock(return_value="# README content")
        dispatch_fn = AsyncMock()

        state: dict = {"env_cache": {"sym": cache_state}, "config": MagicMock()}
        await svc.check_and_trigger("sym", sym_cfg, github, state, dispatch_fn)

        dispatch_fn.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_retriggers_when_success_persisted_but_activate_missing(
        self, tmp_path: Path,
    ) -> None:
        """087: a persisted snapshot can claim last_bootstrap_succeeded=True with
        readme_sha == current_sha, yet the cache dir on disk has lost activate.sh
        (wiped between runs, or the snapshot outlived the cache). Without a
        disk-presence check the trigger computes needs_bootstrap=False and never
        re-fires, while the consumer gate (which DOES check activate.sh) holds
        every card forever — a permanent deadlock. The trigger must mirror the
        consumer gate and re-bootstrap when activate.sh is absent."""
        svc, _ = self._make_service()
        cache_dir = tmp_path / "env"
        cache_dir.mkdir()  # exists, but NO activate.sh inside (hollow cache)
        cache_state = _accepted_state(
            symphony_name="sym",
            sanitised_name="sym-abc",
            cache_dir=cache_dir,
            readme_sha=_source_fingerprint({"README.md": "same_sha"}),
            last_bootstrap_succeeded=True,
            cache_dir_ready=True,
            last_bootstrap_at=datetime.now(UTC)
            - timedelta(seconds=BOOTSTRAP_RETRY_COOLDOWN_S + 5),
        )
        sym_cfg = self._make_symphony_config()
        sym_cfg.effective_config = MagicMock(return_value=self._make_eff_config())
        github = AsyncMock()
        github.get_branch_head_sha = AsyncMock(return_value=_head("same_sha"))
        github.get_file_content = AsyncMock(return_value="# README content")
        dispatch_fn = AsyncMock()

        state: dict = {"env_cache": {"sym": cache_state}, "config": MagicMock()}
        await svc.check_and_trigger("sym", sym_cfg, github, state, dispatch_fn)

        dispatch_fn.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_no_retry_when_prior_failure_within_cooldown(
        self, tmp_path: Path,
    ) -> None:
        """077: a recently-failed bootstrap must NOT relaunch a container on the
        very next poll cycle — the cooldown throttles retries."""
        svc, _ = self._make_service()
        cache_dir = tmp_path / "env"
        cache_dir.mkdir()
        cache_state = _accepted_state(
            symphony_name="sym",
            sanitised_name="sym-abc",
            cache_dir=cache_dir,
            readme_sha=_source_fingerprint({"README.md": "same_sha"}),
            last_bootstrap_succeeded=False,
            last_bootstrap_at=datetime.now(UTC) - timedelta(seconds=5),
        )
        sym_cfg = self._make_symphony_config()
        sym_cfg.effective_config = MagicMock(return_value=self._make_eff_config())
        github = AsyncMock()
        github.get_branch_head_sha = AsyncMock(return_value=_head("same_sha"))
        dispatch_fn = AsyncMock()

        state: dict = {"env_cache": {"sym": cache_state}, "config": MagicMock()}
        await svc.check_and_trigger("sym", sym_cfg, github, state, dispatch_fn)

        dispatch_fn.assert_not_called()

    @pytest.mark.asyncio
    async def test_synchronous_dispatch_failure_does_not_wedge_in_flight(
        self, tmp_path: Path,
    ) -> None:
        """077: when dispatch fails SYNCHRONOUSLY (e.g. readiness timeout) the
        daemon calls on_bootstrap_complete(False) inside dispatch_fn, clearing
        in_flight. _do_dispatch must NOT then re-set in_flight=True — that would
        wedge it forever with no container (stale-in-flight deadlock)."""
        svc, _ = self._make_service()
        cache_dir = tmp_path / "env"
        cache_dir.mkdir()
        cache_state = _accepted_state(
            symphony_name="sym",
            sanitised_name="sym-abc",
            cache_dir=cache_dir,
            readme_sha="old_sha",
        )
        global_cfg = MagicMock()
        sym_cfg = self._make_symphony_config()
        sym_cfg.effective_config = MagicMock(return_value=self._make_eff_config())
        github = AsyncMock()
        github.get_branch_head_sha = AsyncMock(return_value=_head("new_sha"))
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
        cache_state = _accepted_state(
            symphony_name="sym", sanitised_name="sym-abc", cache_dir=cache_dir,
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
        cache_state = _accepted_state(
            symphony_name="sym", sanitised_name="sym-abc", cache_dir=cache_dir,
        )
        sym_cfg = self._make_symphony_config()
        sym_cfg.effective_config = MagicMock(return_value=self._make_eff_config())
        github = AsyncMock()
        github.get_branch_head_sha = AsyncMock(side_effect=RuntimeError("timeout"))
        dispatch_fn = AsyncMock()
        state: dict = {"env_cache": {"sym": cache_state}, "config": MagicMock()}
        await svc.check_and_trigger("sym", sym_cfg, github, state, dispatch_fn)
        dispatch_fn.assert_not_called()

    @pytest.mark.asyncio
    async def test_handles_sha_fetch_returns_none(self, tmp_path: Path) -> None:
        svc, _ = self._make_service()
        cache_dir = tmp_path / "env"
        cache_dir.mkdir()
        cache_state = _accepted_state(
            symphony_name="sym", sanitised_name="sym-abc", cache_dir=cache_dir,
        )
        sym_cfg = self._make_symphony_config()
        sym_cfg.effective_config = MagicMock(return_value=self._make_eff_config())
        github = AsyncMock()
        github.get_branch_head_sha = AsyncMock(return_value=None)
        dispatch_fn = AsyncMock()
        state: dict = {"env_cache": {"sym": cache_state}, "config": MagicMock()}
        await svc.check_and_trigger("sym", sym_cfg, github, state, dispatch_fn)
        dispatch_fn.assert_not_called()

    @pytest.mark.asyncio
    async def test_handles_content_fetch_exception(self, tmp_path: Path) -> None:
        svc, _ = self._make_service()
        cache_dir = tmp_path / "env"
        cache_dir.mkdir()
        cache_state = _accepted_state(
            symphony_name="sym", sanitised_name="sym-abc", cache_dir=cache_dir, readme_sha="old",
        )
        sym_cfg = self._make_symphony_config()
        sym_cfg.effective_config = MagicMock(return_value=self._make_eff_config())
        github = AsyncMock()
        github.get_branch_head_sha = AsyncMock(return_value=_head("new_sha"))
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
        cache_state = _accepted_state(
            symphony_name="sym", sanitised_name="sym-abc", cache_dir=cache_dir, readme_sha="old",
        )
        sym_cfg = self._make_symphony_config()
        sym_cfg.effective_config = MagicMock(return_value=self._make_eff_config())
        github = AsyncMock()
        github.get_branch_head_sha = AsyncMock(return_value=_head("new_sha"))
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
        cache_state = _accepted_state(
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
        github.get_branch_head_sha = AsyncMock(return_value=_head("newer_sha"))
        dispatch_fn = AsyncMock()

        state: dict = {"env_cache": {"sym": cache_state}, "config": global_cfg}
        await svc.check_and_trigger("sym", sym_cfg, github, state, dispatch_fn)

        dispatch_fn.assert_not_called()
        assert cache_state.pending_sha == _source_fingerprint({"README.md": "newer_sha"})


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
        # 092: no test_env block configured (the common case).
        sym.test_env = None
        eff = MagicMock()
        eff.github_org = "org"
        eff.project_name = "repo"
        eff.base_branch = "main"
        sym.effective_config = MagicMock(return_value=eff)
        return sym

    def _make_service(self, tmp_path: Path, symphonies: list) -> MagicMock:
        cfg = MagicMock()
        cfg.symphonies = symphonies
        cfg.global_config.env_cache_root = str(tmp_path)
        return EnvCacheService(cfg)

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
        self, tmp_path: Path,
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
        github.get_branch_head_sha = AsyncMock(return_value=_head("abc123"))
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
        coordinare_config = _configured(MagicMock())
        coordinare_config.symphonies = []
        # 088: a real budget so MagicMock's int-coercion (1) doesn't trip the
        # circuit breaker on the first simulated failure in these tests.
        coordinare_config.global_config.env_bootstrap_max_attempts = 3
        return EnvCacheService(coordinare_config)

    def test_noop_when_symphony_not_in_cache(self) -> None:
        svc = self._make_service()
        state: dict = {"env_cache": {}}
        svc.on_bootstrap_complete("missing", True, state)  # must not raise

    def test_clears_in_flight_flag(self, tmp_path: Path) -> None:
        svc = self._make_service()
        cache_state = _accepted_state(
            symphony_name="sym",
            sanitised_name="sym-abc",
            cache_dir=tmp_path,
            bootstrap_in_flight=True,
        )
        _seed_toolchain(tmp_path)
        state = {"env_cache": {"sym": cache_state}}
        record_verified_cache_fixture(state["env_cache"]["sym"])
        svc.on_bootstrap_complete("sym", True, state)
        assert cache_state.bootstrap_in_flight is False
        assert cache_state.last_bootstrap_succeeded is True
        assert cache_state.last_bootstrap_at is not None

    def test_records_error_on_failure_and_clears_on_success(self, tmp_path: Path) -> None:
        """077: a failed bootstrap stores last_bootstrap_error (for the dashboard);
        a successful one clears it."""
        svc = self._make_service()
        cache_state = _accepted_state(
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
        _seed_toolchain(tmp_path)
        record_verified_cache_fixture(state["env_cache"]["sym"])
        svc.on_bootstrap_complete("sym", True, state)
        assert cache_state.last_bootstrap_succeeded is True
        assert cache_state.last_bootstrap_error is None

    def test_forces_recheck_when_pending_sha_differs(self, tmp_path: Path) -> None:
        svc = self._make_service()
        cache_state = _accepted_state(
            symphony_name="sym",
            sanitised_name="sym-abc",
            cache_dir=tmp_path,
            bootstrap_in_flight=True,
            readme_sha="sha1",
            pending_sha="sha2",
        )
        state = {"env_cache": {"sym": cache_state}}
        record_verified_cache_fixture(state["env_cache"]["sym"])
        svc.on_bootstrap_complete("sym", True, state)
        assert cache_state.readme_sha is None
        assert cache_state.pending_sha is None

    def test_no_recheck_when_pending_sha_same(self, tmp_path: Path) -> None:
        svc = self._make_service()
        cache_state = _accepted_state(
            symphony_name="sym",
            sanitised_name="sym-abc",
            cache_dir=tmp_path,
            bootstrap_in_flight=True,
            readme_sha="sha1",
            pending_sha="sha1",
        )
        state = {"env_cache": {"sym": cache_state}}
        record_verified_cache_fixture(state["env_cache"]["sym"])
        svc.on_bootstrap_complete("sym", True, state)
        # Same SHA — no forced recheck needed
        assert cache_state.readme_sha == "sha1"

    def test_keeps_sha_on_failure_so_retry_honours_cooldown(self, tmp_path: Path) -> None:
        """088 (supersedes the pre-088 clearing): the SHA is RETAINED on failure
        so the retry flows through check_and_trigger's escalating-cooldown
        branch (readme_sha == current ∧ succeeded=False) instead of an
        immediate needs_bootstrap redispatch every ~100s. The consumer gate
        still holds (last_bootstrap_succeeded=False), so the cache is not
        treated as usable."""
        svc = self._make_service()
        cache_state = _accepted_state(
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
        assert cache_state.readme_sha == "sha1"
        assert cache_state.bootstrap_attempts == 1

    def test_preserves_sha_on_success(self, tmp_path: Path) -> None:
        svc = self._make_service()
        cache_state = _accepted_state(
            symphony_name="sym",
            sanitised_name="sym-abc",
            cache_dir=tmp_path,
            bootstrap_in_flight=True,
            readme_sha="sha1",
        )
        _seed_toolchain(tmp_path)
        state = {"env_cache": {"sym": cache_state}}
        record_verified_cache_fixture(state["env_cache"]["sym"])
        svc.on_bootstrap_complete("sym", True, state)
        assert cache_state.last_bootstrap_succeeded is True
        assert cache_state.readme_sha == "sha1"

    def test_seed_only_mount_without_clean_execution_downgrades_reported_success(self, tmp_path: Path) -> None:
        """092: a bootstrap that reports success but leaves the mount holding
        only the coordinare-seeded files (or nothing) installed the toolchain in
        the wrong location. The success is downgraded to failure LOUDLY so the
        empty cache is never marked ready, and it flows through the normal
        attempt-counter path.
        """
        svc = self._make_service()
        # Seed only the three coordinare-owned files — no toolchain content.
        (tmp_path / "activate.sh").write_text("# seed\n")
        (tmp_path / "manifest.json").write_text("{}\n")
        (tmp_path / "verify.sh").write_text("# seed\n")
        cache_state = _accepted_state(
            symphony_name="sym",
            sanitised_name="sym-abc",
            cache_dir=tmp_path,
            cache_dir_ready=False,
            bootstrap_in_flight=True,
        )
        state = {"env_cache": {"sym": cache_state}}
        _accept(cache_state, nonempty=True)
        cache_state.environment_verification = None
        svc.on_bootstrap_complete("sym", True, state)
        assert cache_state.last_bootstrap_succeeded is False
        assert cache_state.cache_dir_ready is False
        assert cache_state.bootstrap_attempts == 1
        assert cache_state.last_bootstrap_error is not None
        assert "required clean consumer execution" in cache_state.last_bootstrap_error

    def test_marks_cache_ready_only_on_success(self, tmp_path: Path) -> None:
        """061: cache_dir_ready must flip True only after a successful bootstrap.

        Before this fix, initialise() set cache_dir_ready=True unconditionally,
        causing performers to mount empty cache directories. The flag must
        gate on actual bootstrap success.
        """
        svc = self._make_service()
        cache_state = _accepted_state(
            symphony_name="sym",
            sanitised_name="sym-abc",
            cache_dir=tmp_path,
            cache_dir_ready=False,
        )
        _seed_toolchain(tmp_path)
        state = {"env_cache": {"sym": cache_state}}
        record_verified_cache_fixture(state["env_cache"]["sym"])
        svc.on_bootstrap_complete("sym", True, state)
        assert cache_state.cache_dir_ready is True

    def test_does_not_mark_cache_ready_on_failure(self, tmp_path: Path) -> None:
        svc = self._make_service()
        cache_state = _accepted_state(
            symphony_name="sym",
            sanitised_name="sym-abc",
            cache_dir=tmp_path,
            cache_dir_ready=False,
        )
        state = {"env_cache": {"sym": cache_state}}
        svc.on_bootstrap_complete("sym", False, state)
        assert cache_state.cache_dir_ready is False

    def test_invalidates_ready_cache_after_bootstrap_failure(self, tmp_path: Path) -> None:
        """A failed refresh invalidates readiness until matching verification passes."""
        svc = self._make_service()
        cache_state = _accepted_state(
            symphony_name="sym",
            sanitised_name="sym-abc",
            cache_dir=tmp_path,
            cache_dir_ready=True,
            readme_sha="sha1",
        )
        state = {"env_cache": {"sym": cache_state}}
        svc.on_bootstrap_complete("sym", False, state)
        assert cache_state.cache_dir_ready is False

    def test_no_job_id_scenario_resolves_in_flight(self, tmp_path: Path) -> None:
        # Regression: when _bootstrap_dispatch_fn receives no job_id from the
        # performer, it must call on_bootstrap_complete(success=False) so that
        # bootstrap_in_flight is not permanently stuck True.
        svc = self._make_service()
        cache_state = _accepted_state(
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
        # 088: SHA retained; the next cycle retries via the cooldown branch
        # (readme_sha == current ∧ succeeded=False), not an immediate redispatch.
        assert cache_state.readme_sha == "sha1"
        assert cache_state.last_bootstrap_succeeded is False


# ---------------------------------------------------------------------------
# T042 (060): _collect_env_volumes_for_persistent_performer
# ---------------------------------------------------------------------------


class TestCollectEnvVolumesForPersistentPerformer:
    def test_returns_empty_when_no_states(self) -> None:
        result = _collect_env_volumes_for_persistent_performer({}, performer_mounts=())
        assert result == []

    def test_skips_caches_without_activate(self, tmp_path: Path) -> None:
        """Caches without an on-disk activate.sh are skipped — no toolchain
        means no usable mount.
        """
        cache_dir = tmp_path / "env"
        cache_dir.mkdir()
        state = _accepted_state(
            symphony_name="s",
            sanitised_name="s-abc",
            cache_dir=cache_dir,
            cache_dir_ready=False,
        )
        result = _collect_env_volumes_for_persistent_performer({"s": state}, performer_mounts=())
        assert result == []

    def test_returns_ro_mounts_for_all_ready_caches(self, tmp_path: Path) -> None:
        dir_a = tmp_path / "a"
        dir_a.mkdir()
        (dir_a / "activate.sh").write_text(_RENDERED)
        dir_b = tmp_path / "b"
        dir_b.mkdir()
        (dir_b / "activate.sh").write_text(_RENDERED)
        states = {
            "sym-a": _accepted_state(
                symphony_name="sym-a",
                sanitised_name="sym-a-111111",
                cache_dir=dir_a,
                cache_dir_ready=True,
                rendered_activate_sh=_RENDERED,
            ),
            "sym-b": _accepted_state(
                symphony_name="sym-b",
                sanitised_name="sym-b-222222",
                cache_dir=dir_b,
                cache_dir_ready=True,
                rendered_activate_sh=_RENDERED,
            ),
        }
        result = _collect_env_volumes_for_persistent_performer(states, performer_mounts=())
        assert len(result) == 2
        paths = {str(v.container_path) for v in result}
        assert "/devenv/sym-a-111111" in paths
        assert "/devenv/sym-b-222222" in paths
        for vol in result:
            assert vol.mode == "ro"

    def test_uses_custom_container_devenv_root(self, tmp_path: Path) -> None:
        cache_dir = tmp_path / "env"
        cache_dir.mkdir()
        (cache_dir / "activate.sh").write_text(_RENDERED)
        state = _accepted_state(
            symphony_name="s",
            sanitised_name="s-abc",
            cache_dir=cache_dir,
            cache_dir_ready=True,
            rendered_activate_sh=_RENDERED,
        )
        result = _collect_env_volumes_for_persistent_performer(
            {"s": state}, container_devenv_root="/custom", performer_mounts=(),
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


def _mount_config() -> object:
    """A loaded-config stand-in with no performer mounts beyond the env-cache root (219)."""
    from types import SimpleNamespace

    global_config = SimpleNamespace(
        performer_log_dir=None,
        performer_endpoints=[],
        env_cache_root="/nonexistent-env-caches",
    )
    return SimpleNamespace(global_config=global_config)


def _make_daemon_for_env_cache(**kwargs):
    """Return a CoordinareDaemon configured for single-cycle env-cache tests."""
    from unittest.mock import AsyncMock, MagicMock

    from coordinare.daemon import CoordinareDaemon

    graph = MagicMock()
    graph.ainvoke = AsyncMock(return_value={})
    daemon = CoordinareDaemon(
        graph,
        poll_interval_seconds=1,
        heartbeat_interval_seconds=1,
        max_cycles=1,
        sleep_func=lambda _: __import__("asyncio").sleep(0),
        **kwargs,
    )
    if daemon._state.get("coordinare_config") is None:
        daemon._state["coordinare_config"] = _mount_config()
    return daemon


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
            return_value={"status": "accepted", "session_id": None},
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
        self, tmp_path: Path,
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
            return_value={"status": "accepted"},  # no session_id / job_id
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
    async def test_bootstrap_dispatch_resolves_model_from_role_mode(
        self, tmp_path: Path,
    ) -> None:
        """The bootstrap dispatch must carry the role's mode-resolved model.

        Regression for the card-vs-bootstrap dispatch asymmetry: the card path
        runs mode → model_endpoint → endpoint resolution
        (resolve_performer_dispatch_model), but the bootstrap path historically
        read only the role's (now schema-forbidden) inline ``model`` field, so
        the dispatch carried model=None. With model=None the self-hosted routing
        table — which keys on (backend, model) — cannot match, and claude_code
        falls back to the LiteLLM shim. This asserts the bootstrap dispatch
        resolves the model (and self-hosted base_url / auth_token_env) the same
        way the card path does.
        """
        import yaml

        from coordinare.config import ProjectConfiguration

        sym_name = "my-project"
        sanitised = sanitise_symphony_name(sym_name)
        cache_dir = tmp_path / sanitised
        cache_dir.mkdir(parents=True, exist_ok=True)

        raw_cfg = {
            "project_name": "t", "github_org": "o", "github_project_number": 1,
            "github_token": "ghp_realtokenvalue1234567890", "human_reviewers": ["a"],
            "endpoints": [
                {"name": "ollama-direct", "kind": "ollama",
                 "base_url": "http://192.0.2.10:11434/v1",
                 "auth_env": "OLLAMA_AUTH_TOKEN"},
            ],
            "model_endpoints": [
                {"name": "qwen25coder", "endpoint": "ollama-direct",
                 "model": "local/qwen2.5-coder:14b-instruct-q6_K"},
            ],
            "modes": [
                {"name": "single-qwen25coder", "strategy": "single", "tool": "qwen25coder"},
            ],
            "performers": {
                "env_bootstrap": {"backend": "claude_code", "mode": "single-qwen25coder"},
            },
        }
        cfg_path = tmp_path / "config.yaml"
        cfg_path.write_text(yaml.safe_dump(raw_cfg))
        real_cfg = ProjectConfiguration.from_yaml(cfg_path)
        expected = real_cfg.resolve_performer_dispatch_model("env_bootstrap")
        assert expected.get("model"), "test precondition: role mode resolves a model"

        daemon = _make_daemon_for_env_cache()
        cache_state = EnvCacheState(
            symphony_name=sym_name,
            sanitised_name=sanitised,
            cache_dir=cache_dir,
            readme_sha="abc123",
            cache_dir_ready=True,
        )
        daemon._state["env_cache"] = {sym_name: cache_state}
        daemon._state["config"] = real_cfg

        performer_svc = MagicMock()
        performer_svc.dispatch_card = AsyncMock(
            return_value={"status": "accepted", "session_id": None},
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

        daemon._state["symphony_configs"] = {sym_name: sym_cfg}
        daemon._state["symphony_states"] = {}
        daemon._state["env_cache_service"] = mock_ec_svc
        daemon._state["symphony_github_services"] = {sym_name: mock_gh}
        daemon._state["performer_services_by_id"] = {"bp-1": performer_svc}
        daemon._poll_bootstrap_completion = AsyncMock()

        await daemon.start()

        performer_svc.dispatch_card.assert_called_once()
        dispatched = performer_svc.dispatch_card.call_args.args[0]
        assert dispatched["backend"] == "claude_code"
        assert dispatched["model"] == expected["model"]
        # self-hosted endpoint → base_url override + bearer auth env propagate too
        if expected.get("base_url"):
            assert dispatched["base_url"] == expected["base_url"]
        if expected.get("auth_token_env"):
            assert dispatched["auth_token_env"] == expected["auth_token_env"]

    @pytest.mark.asyncio
    async def test_bootstrap_dispatch_fn_missing_performer_logs_warning(
        self, tmp_path: Path, caplog,
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
        """Advance the daemon clock with each sleep, without real delays."""
        clock = _BootstrapClock()
        monkeypatch.setattr(daemon_mod.asyncio, "sleep", clock.sleep)
        monkeypatch.setattr(daemon_mod, "monotonic", clock.time)

    @pytest.fixture(autouse=True)
    def _clean_verify_passes(self, monkeypatch):
        """218: these tests cover the poll loop mapping a result to on_bootstrap_complete.
        The clean-context verify has its own tests in test_218_bootstrap_trust."""
        from coordinare.daemon import CoordinareDaemon

        monkeypatch.setattr(
            CoordinareDaemon,
            "_verify_bootstrap_in_clean_context",
            AsyncMock(return_value=(True, None, False)),
        )
        # 218: the bootstrap container's stop has its own tests; these tests run the success path.
        monkeypatch.setattr(
            CoordinareDaemon,
            "_quiesce_bootstrap_container",
            AsyncMock(return_value=True),
        )

    @pytest.mark.asyncio
    async def test_success_path_calls_on_bootstrap_complete_true(self) -> None:
        daemon = _make_daemon_for_env_cache()
        svc = MagicMock()
        svc.check_status = AsyncMock(return_value={"status": "env_bootstrap_complete"})
        ec_svc = MagicMock(freeze_provenance=AsyncMock(return_value=(True, None)))
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
        ec_svc = MagicMock(freeze_provenance=AsyncMock(return_value=(True, None)))

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
            return_value={"status": "error", "error": "boom", "availability": "stopped"},
        )
        # No container_id — falls through to get_agent_logs branch
        svc.get_agent_logs = MagicMock(return_value=["line-a", "line-b"])
        # Simulate no _active_jobs container_id either
        svc._active_jobs = {}
        ec_svc = MagicMock(freeze_provenance=AsyncMock(return_value=(True, None)))

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
        ec_svc = MagicMock(freeze_provenance=AsyncMock(return_value=(True, None)))

        await daemon._poll_bootstrap_completion(svc, "job-1", "alpha", ec_svc)

        ec_svc.on_bootstrap_complete.assert_called_once_with("alpha", False, daemon._state, error=ANY)

    @pytest.mark.asyncio
    async def test_failure_get_agent_logs_raises(self) -> None:
        daemon = _make_daemon_for_env_cache()
        svc = MagicMock()
        svc.check_status = AsyncMock(return_value={"status": "failed"})
        svc.get_agent_logs = MagicMock(side_effect=RuntimeError("boom"))
        svc._active_jobs = {}
        ec_svc = MagicMock(freeze_provenance=AsyncMock(return_value=(True, None)))

        await daemon._poll_bootstrap_completion(svc, "job-1", "alpha", ec_svc)

        ec_svc.on_bootstrap_complete.assert_called_once_with("alpha", False, daemon._state, error=ANY)

    @pytest.mark.asyncio
    async def test_budget_exhausted_reaps_container_and_marks_failure(self, monkeypatch) -> None:
        """076 (live QA #150): a bootstrap that never reports terminal status
        must be bounded by ``bootstrap_max_seconds``. The loop stops at the
        elapsed deadline, stops the job, and marks failure (which
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
        svc.stop_session_confirmed = AsyncMock(return_value=True)
        ec_svc = MagicMock(freeze_provenance=AsyncMock(return_value=(True, None)))

        # Fake docker subprocesses (logs snapshot each iter).
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
            svc, "job-1", "alpha", ec_svc, container_id="ctr-hung-deadbeef",
        )

        # One regular read plus the final status read at the elapsed deadline.
        assert svc.check_status.await_count == 2
        # The job was stopped through its runtime at the cap (270: not docker stop).
        svc.stop_session_confirmed.assert_awaited_once_with("job-1")
        assert stop_calls == []
        # Failure recorded so the cache stays un-ready and dispatch unblocks.
        ec_svc.on_bootstrap_complete.assert_called_once_with("alpha", False, daemon._state, error=ANY)
        assert "exceeded" in ec_svc.on_bootstrap_complete.call_args.kwargs["error"]

    @pytest.mark.asyncio
    async def test_sub_10s_budget_clamps_to_one_attempt(self, monkeypatch) -> None:
        """A budget below the poll interval still gets a final status read."""
        import coordinare.daemon as daemon_mod

        daemon = _make_daemon_for_env_cache()
        cfg = MagicMock()
        cfg.bootstrap_max_seconds = 5  # 5 // 10 == 0 -> clamped to 1
        cfg.bootstrap_idle_timeout_seconds = 0
        daemon._state["coordinare_config"] = cfg

        svc = MagicMock()
        svc.stop_session_confirmed = AsyncMock(return_value=True)
        svc.check_status = AsyncMock(return_value={"status": "working"})
        ec_svc = MagicMock(freeze_provenance=AsyncMock(return_value=(True, None)))

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
    async def test_reap_failure_retries_before_recording_failure(self, monkeypatch) -> None:
        """An unconfirmed stop stays in flight until a later stop succeeds."""
        import coordinare.daemon as daemon_mod

        daemon = _make_daemon_for_env_cache()
        cfg = MagicMock()
        cfg.bootstrap_max_seconds = 10  # 1 attempt
        cfg.bootstrap_idle_timeout_seconds = 0
        daemon._state["coordinare_config"] = cfg

        svc = MagicMock()
        svc.check_status = AsyncMock(return_value={"status": "working"})
        stop_results = [RuntimeError("runtime unreachable"), True]
        svc.stop_session_confirmed = AsyncMock(side_effect=stop_results)
        ec_svc = MagicMock(freeze_provenance=AsyncMock(return_value=(True, None)))

        async def _fake_exec(*argv, **kwargs):
            class _P:
                async def communicate(self):
                    return (b"", b"")

                async def wait(self):
                    return 0

            return _P()

        monkeypatch.setattr(daemon_mod.asyncio, "create_subprocess_exec", _fake_exec)

        await daemon._poll_bootstrap_completion(svc, "j", "alpha", ec_svc, container_id="c-stuck")

        assert svc.stop_session_confirmed.await_count == len(stop_results)
        ec_svc.on_bootstrap_complete.assert_called_once_with("alpha", False, daemon._state, error=ANY)

    @pytest.mark.asyncio
    async def test_idle_reap_fires_when_no_event_progress(self, monkeypatch) -> None:
        """076 / 270 idle reap: a bootstrap whose status events are frozen for
        bootstrap_idle_timeout_seconds is reaped early, well before the wall-clock
        budget, through its runtime, and marked failed."""
        import coordinare.daemon as daemon_mod

        daemon = _make_daemon_for_env_cache()
        cfg = MagicMock()
        cfg.bootstrap_max_seconds = 3600  # large: wall-clock must NOT fire here
        cfg.bootstrap_idle_timeout_seconds = 30  # 3 idle polls -> reap
        daemon._state["coordinare_config"] = cfg

        # Same events on every poll: no progress.
        svc = MagicMock()
        svc.check_status = AsyncMock(return_value={"status": "working", "events": []})
        svc.stop_session_confirmed = AsyncMock(return_value=True)
        ec_svc = MagicMock(freeze_provenance=AsyncMock(return_value=(True, None)))

        async def _fake_exec(*argv, **kwargs):
            class _P:
                async def communicate(self):
                    return (b"", b"")

                async def wait(self):
                    return 0

            return _P()

        monkeypatch.setattr(daemon_mod.asyncio, "create_subprocess_exec", _fake_exec)

        await daemon._poll_bootstrap_completion(svc, "j", "alpha", ec_svc, container_id="ctr-idle")

        # Four regular reads show 30 seconds of silence, then one final read confirms it.
        assert svc.check_status.await_count == 5
        svc.stop_session_confirmed.assert_awaited_once_with("j")
        error = ec_svc.on_bootstrap_complete.call_args.kwargs["error"]
        assert "no progress for 0.5 minutes" in error
        ec_svc.on_bootstrap_complete.assert_called_once_with("alpha", False, daemon._state, error=ANY)

    @pytest.mark.asyncio
    async def test_idle_reap_not_triggered_while_events_progress(self, monkeypatch) -> None:
        """076 / 270 idle reap: a slow-but-progressing bootstrap (new status events
        each poll) is NOT reaped even past the idle window. It completes normally."""
        import coordinare.daemon as daemon_mod

        daemon = _make_daemon_for_env_cache()
        cfg = MagicMock()
        cfg.bootstrap_max_seconds = 3600
        cfg.bootstrap_idle_timeout_seconds = 30
        daemon._state["coordinare_config"] = cfg
        daemon._state["performer_services"] = {}

        stop_calls: list[tuple] = []
        order: list[str] = []
        counter = {"n": 0}

        async def _fake_exec(*argv, **kwargs):
            if "stop" in argv:
                stop_calls.append(argv)
                order.append("stop")

            class _P:
                returncode = 0

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
        # 270: progress is a new status event each poll (distinct text, not a loop).
        statuses = [
            {"status": "working", "events": [{"text": f"install step {n} finished. "}]}
            for n in range(5)
        ] + [{"status": "env_bootstrap_complete"}]
        svc = MagicMock()
        svc.check_status = AsyncMock(side_effect=statuses)
        svc._runtime = DockerRuntime()
        ec_svc = MagicMock(freeze_provenance=AsyncMock(return_value=(True, None)))

        # The real quiesce (not the class stub), and a verify that records when it runs.
        monkeypatch.setattr(CoordinareDaemon, "_quiesce_bootstrap_container", _ORIGINAL_QUIESCE)

        async def _recording_verify(*_a: Any, **_k: Any) -> tuple[bool, None, bool]:
            order.append("verify")
            return True, None, False

        monkeypatch.setattr(CoordinareDaemon, "_verify_bootstrap_in_clean_context", _recording_verify)

        await daemon._poll_bootstrap_completion(svc, "j", "alpha", ec_svc, container_id="ctr-slow")

        # 218: the one `docker stop` is the completion quiesce, not an idle reap. It names the stopped
        # container, and it happens before the clean verify. Progress kept the idle reap from firing.
        assert len(stop_calls) == 1
        assert "ctr-slow" in stop_calls[0]
        assert order == ["stop", "verify"]
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
        svc.stop_session_confirmed = AsyncMock(return_value=True)
        svc.check_status = AsyncMock(return_value={"status": "working"})
        ec_svc = MagicMock(freeze_provenance=AsyncMock(return_value=(True, None)))

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
        import coordinare.daemon as daemon_mod

        daemon = _make_daemon_for_env_cache()
        svc = MagicMock()
        svc.stop_session_confirmed = AsyncMock(return_value=True)
        svc.check_status = AsyncMock(side_effect=RuntimeError("connection lost"))
        ec_svc = MagicMock(freeze_provenance=AsyncMock(return_value=(True, None)))

        await daemon._poll_bootstrap_completion(svc, "job-1", "alpha", ec_svc)

        ec_svc.on_bootstrap_complete.assert_called_once_with("alpha", False, daemon._state, error=ANY)
        # Only declared terminal after the consecutive-failure tolerance is
        # exhausted — not on the very first transient miss.
        assert svc.check_status.await_count == daemon_mod._BOOTSTRAP_POLL_MAX_CONSECUTIVE_FAILURES

    @pytest.mark.asyncio
    async def test_transient_poll_failures_below_threshold_do_not_fail(self) -> None:
        """A run of transient poll exceptions shorter than the tolerance, then a
        terminal success, must NOT mark the bootstrap failed — the streak resets
        on the first successful poll."""
        daemon = _make_daemon_for_env_cache()
        svc = MagicMock()
        # Two transient failures (tolerance is 3) then a clean terminal success.
        svc.check_status = AsyncMock(
            side_effect=[
                RuntimeError("timeout"),
                RuntimeError("timeout"),
                {"status": "env_bootstrap_complete"},
            ],
        )
        ec_svc = MagicMock(freeze_provenance=AsyncMock(return_value=(True, None)))

        await daemon._poll_bootstrap_completion(svc, "job-1", "alpha", ec_svc)

        # Success path: marked complete with success=True, never a failure.
        # The success call passes an ``error=`` kwarg (None on success), so match
        # loosely on the positional args rather than the full signature.
        ec_svc.on_bootstrap_complete.assert_called_once()
        args = ec_svc.on_bootstrap_complete.call_args.args
        assert args[0] == "alpha"
        assert args[1] is True
        assert svc.check_status.await_count == 3

    @pytest.mark.asyncio
    async def test_poll_failure_streak_resets_on_success(self) -> None:
        """Non-consecutive failures must not accumulate: a success between two
        short failure runs resets the counter so neither run alone trips the
        terminal threshold."""
        daemon = _make_daemon_for_env_cache()
        svc = MagicMock()
        # fail, fail, success (reset), fail, fail, success — never 3 in a row.
        svc.check_status = AsyncMock(
            side_effect=[
                RuntimeError("timeout"),
                RuntimeError("timeout"),
                {"status": "working"},
                RuntimeError("timeout"),
                RuntimeError("timeout"),
                {"status": "env_bootstrap_complete"},
            ],
        )
        ec_svc = MagicMock(freeze_provenance=AsyncMock(return_value=(True, None)))

        await daemon._poll_bootstrap_completion(svc, "job-1", "alpha", ec_svc)

        ec_svc.on_bootstrap_complete.assert_called_once()
        args = ec_svc.on_bootstrap_complete.call_args.args
        assert args[0] == "alpha"
        assert args[1] is True
        assert svc.check_status.await_count == 6

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

        ec_svc = MagicMock(freeze_provenance=AsyncMock(return_value=(True, None)))
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
            returncode = 0

            async def communicate(self):
                return (b"snap-line-1\nsnap-line-2\n", b"")

            async def wait(self):
                return 0

        async def _make_proc(*_a, **_kw):
            return _FakeProc()

        import coordinare.daemon as daemon_mod
        monkeypatch.setattr(daemon_mod.asyncio, "create_subprocess_exec", _make_proc)

        ec_svc = MagicMock(freeze_provenance=AsyncMock(return_value=(True, None)))
        await daemon._poll_bootstrap_completion(
            svc, "job-1", "alpha", ec_svc, container_id="cid-xyz",
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
            },
        ],
        "performer_endpoints": [
            {
                "id": endpoint_id,
                "mode": "ephemeral",
                "roles": ["implementer"],
                "image": "coordinare-performer:full",
                "endpoint": "http://localhost:8080",
            },
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


def test_env_bootstrap_persona_preserves_declared_install_contract() -> None:
    """A refresh installs in place and cannot substitute a different toolchain."""
    from coordinare.services.http_performer_service import HTTPPerformerService

    payload = HTTPPerformerService._build_env_bootstrap_payload(None, _bootstrap_card_context())
    persona = payload.persona.lower()
    assert "declared install actions" in persona
    assert "install in place" in persona
    assert "/devenv/sampleweb" in persona
    assert "do not infer a different toolchain" in persona
    assert "required failure or unrun check cannot be waived" in persona
    assert "read-only at the same absolute path" in persona
    assert "never your success prose" in persona
    assert ".ruby-version" not in persona
    assert "ruby-build" not in persona

def test_env_bootstrap_persona_defers_activation_when_coordinare_owns_it() -> None:
    """Accepted declarative activation is never replaced by installer scripts."""
    from coordinare.services.http_performer_service import HTTPPerformerService

    payload = HTTPPerformerService._build_env_bootstrap_payload(None,
        _bootstrap_card_context(activate_provided=True, verify_provided=True))
    persona = payload.persona.lower()
    assert "daemon owns activate.sh and verify.sh" in persona
    assert "do not rewrite either file" in persona
    assert "preserve its requirements, checks, activation and service lifecycle" in persona
    assert "write a sourceable shell script" not in persona

def _bootstrap_card_context(**extra) -> dict:
    ctx = {
        "symphony_org": "ExampleOrg",
        "symphony_repo": "sampleweb",
        "cache_mount_path": "/devenv/sampleweb",
        "env_spec_files": ["README.md"],
        "env_spec_contents": {"README.md": "Install Ruby 3.4.2 and Chrome."},
        "backend": "opencode",
    }
    ctx.update(extra)
    return ctx


class TestManifestArtifacts:
    """Accepted generic contracts generate both trusted scripts atomically."""

    @pytest.mark.asyncio
    async def test_writes_verify_and_returns_accepted_contract(self, tmp_path: Path, monkeypatch) -> None:
        from coordinare_service_inference.environment.models import (
            CoverageAssessment,
            EnvironmentManifest,
        )

        svc = EnvCacheService(_configured(MagicMock()))
        cache_dir = tmp_path / "cache"
        cache_state = EnvCacheState(symphony_name="sym", sanitised_name="sym",
            cache_dir=cache_dir, bootstrap_commit_sha=_head("same_sha"))
        metadata = reviewed_metadata(source_commit=cache_state.bootstrap_commit_sha,
            checks=[{"id": "baseline", "argv": ["zog", "--check", "7"]}],
            activation={"path": ["{cache}/zog-tools/bin"]})
        manifest = EnvironmentManifest.model_validate(metadata["environment_manifest"])
        assessment = CoverageAssessment.model_validate(metadata["environment_assessment"])
        discover = AsyncMock(return_value=(manifest, assessment))
        monkeypatch.setattr("coordinare.services.environment_discovery.discover_and_review", discover)
        github = AsyncMock()
        github.get_repository_paths.return_value = ["tools.plan"]

        checklist, verify_provided, activate_provided = await svc._build_manifest_artifacts(
            performer_mounts=(), symphony_name="sym", repo="project", github_org="org",
            github_service=github, readme_contents={}, current_sha="source-key",
            cache_dir=cache_dir, cache_mount_path="/devenv/sym", llm_chat=AsyncMock(),
            cache_state=cache_state)

        assert verify_provided is True and activate_provided is True
        assert "ACCEPTED ENVIRONMENT CONTRACT" in checklist
        assert '"zog"' in checklist
        assert manifest.canonical_json() in (cache_dir / "verify.sh").read_text()
        assert "/devenv/sym/zog-tools/bin" in (cache_dir / "activate.sh").read_text()
        assert json.loads((cache_dir / "manifest.json").read_text()) == manifest.model_dump()
        assert cache_state.environment_manifest == manifest.model_dump()
        assert cache_state.environment_assessment == assessment.model_dump()
        assert (cache_dir / "verify.sh").stat().st_mode & 0o100
        assert (cache_dir / "activate.sh").stat().st_mode & 0o100
        github.get_repository_paths.assert_awaited_once_with("org", "project",
            ref=cache_state.bootstrap_commit_sha, max_entries=100_000)

    @pytest.mark.asyncio
    async def test_no_deps_without_review_cannot_render_ready_scripts(self, tmp_path: Path, monkeypatch, _accepted_discovery) -> None:
        """An empty parse is insufficient evidence for an accepted environment."""
        from coordinare.services.environment_discovery import EnvironmentDiscoveryError

        svc = EnvCacheService(_configured(MagicMock()))
        cache_dir = tmp_path / "cache"
        cache_state = EnvCacheState(symphony_name="sym", sanitised_name="sym",
            cache_dir=cache_dir, bootstrap_commit_sha=_head("same_sha"))
        monkeypatch.setattr("coordinare.services.environment_discovery.discover_and_review",
            _accepted_discovery)
        github = AsyncMock()
        github.get_repository_paths.return_value = ["instructions"]

        with pytest.raises(EnvironmentDiscoveryError, match="configuration"):
            await svc._build_manifest_artifacts(performer_mounts=(), symphony_name="sym",
                repo="project", github_org="org", github_service=github,
                readme_contents={"instructions": "No extra dependencies"}, current_sha="source-key",
                cache_dir=cache_dir, cache_mount_path="/devenv/sym", llm_chat=None,
                cache_state=cache_state)
        assert cache_state.environment_manifest is None
        assert cache_state.environment_assessment is None
        assert cache_state.rendered_verify_sh is None
        assert not (cache_dir / "verify.sh").exists()


def test_env_bootstrap_persona_injects_previous_failure_on_retry() -> None:
    """A bounded repair receives the exact prior verification failure."""
    from coordinare.services.http_performer_service import HTTPPerformerService

    failure = "version mismatch (expected zog7, got zog6); executable check failed"
    payload = HTTPPerformerService._build_env_bootstrap_payload(None,
        _bootstrap_card_context(last_failure=failure))
    assert failure in payload.persona
    assert "Previous failure:" in payload.persona
    assert "weaken checks" in payload.persona

def test_env_bootstrap_persona_no_retry_block_on_first_attempt() -> None:
    """No prior failure → no retry block (first/clean bootstrap)."""
    from coordinare.services.http_performer_service import HTTPPerformerService

    payload = HTTPPerformerService._build_env_bootstrap_payload(None, _bootstrap_card_context())
    assert "RETRY" not in payload.persona


def test_env_bootstrap_persona_injects_manifest_checklist() -> None:
    """The authoritative accepted contract is visible to the installer."""
    from coordinare.services.http_performer_service import HTTPPerformerService

    checklist = 'ACCEPTED ENVIRONMENT CONTRACT: {"tool":"zog", "version":"7"}'
    payload = HTTPPerformerService._build_env_bootstrap_payload(None,
        _bootstrap_card_context(dependency_checklist=checklist, verify_provided=True))
    assert checklist in payload.persona
    assert "daemon owns activate.sh and verify.sh" in payload.persona
    assert "Do not rewrite either file" in payload.persona

def test_env_bootstrap_persona_does_not_authorize_unreviewed_verification() -> None:
    """Missing legacy flags cannot authorize installer-owned verification."""
    from coordinare.services.http_performer_service import HTTPPerformerService

    persona = HTTPPerformerService._build_env_bootstrap_payload(None,
        _bootstrap_card_context()).persona.lower()
    assert "also write an executable verification script" not in persona
    assert "daemon owns activate.sh and verify.sh" in persona
    assert "separate clean consumer executes the accepted checks" in persona

class TestBootstrapCircuitBreaker:
    def _make_service(self, max_attempts: int = 3) -> EnvCacheService:
        coordinare_config = _configured(MagicMock())
        coordinare_config.symphonies = []
        coordinare_config.global_config.env_bootstrap_max_attempts = max_attempts
        return EnvCacheService(coordinare_config)

    def _make_cache_state(self, tmp_path: Path, **overrides) -> EnvCacheState:
        defaults = {
            "symphony_name": "sym",
            "sanitised_name": "sym-abc",
            "cache_dir": tmp_path,
        }
        defaults.update(overrides)
        return _accepted_state(**defaults)

    def _make_symphony_config(self) -> MagicMock:
        cfg = MagicMock()
        cfg.env_bootstrap_performer_id = "bootstrap"
        cfg.env_spec_files = ["README.md"]
        cfg.name = "test-symphony"
        # 092: no test_env block configured (the common case).
        cfg.test_env = None
        eff = MagicMock()
        eff.github_org = "org"
        eff.project_name = "repo"
        eff.base_branch = "main"
        cfg.effective_config = MagicMock(return_value=eff)
        return cfg

    def test_failure_increments_attempts(self, tmp_path: Path) -> None:
        svc = self._make_service()
        cache_state = self._make_cache_state(tmp_path)
        state = {"env_cache": {"sym": cache_state}}
        svc.on_bootstrap_complete("sym", False, state, error="boom")
        assert cache_state.bootstrap_attempts == 1
        svc.on_bootstrap_complete("sym", False, state, error="boom")
        assert cache_state.bootstrap_attempts == 2
        assert cache_state.bootstrap_exhausted is False

    def test_failure_keeps_readme_sha_for_cooldown_retry(self, tmp_path: Path) -> None:
        """088: a failure must NOT clear readme_sha — clearing it made the next
        cycle recompute needs_bootstrap=True and redispatch immediately, which
        is the observed unbounded ~100s hammering. Retries flow through the
        escalating-cooldown branch instead."""
        svc = self._make_service()
        cache_state = self._make_cache_state(
            tmp_path, readme_sha="sha-under-attempt", bootstrap_in_flight=True,
        )
        state = {"env_cache": {"sym": cache_state}}
        svc.on_bootstrap_complete("sym", False, state, error="boom")
        assert cache_state.readme_sha == "sha-under-attempt"
        assert cache_state.last_bootstrap_succeeded is False

    def test_success_resets_budget(self, tmp_path: Path) -> None:
        svc = self._make_service()
        cache_state = self._make_cache_state(
            tmp_path, bootstrap_attempts=2, bootstrap_exhausted=True,
        )
        _seed_toolchain(tmp_path)
        state = {"env_cache": {"sym": cache_state}}
        record_verified_cache_fixture(state["env_cache"]["sym"])
        svc.on_bootstrap_complete("sym", True, state)
        assert cache_state.bootstrap_attempts == 0
        assert cache_state.bootstrap_exhausted is False

    @pytest.mark.asyncio
    async def test_exhaustion_at_max_attempts_notifies_exactly_once(
        self, tmp_path: Path,
    ) -> None:
        """attempts >= env_bootstrap_max_attempts flips bootstrap_exhausted and
        fires exactly ONE circuit_breaker_trip notification — further failures
        do not re-notify."""
        from coordinare.models.notification import EventType

        svc = self._make_service(max_attempts=3)
        cache_state = self._make_cache_state(tmp_path)
        notify = MagicMock()
        notify.dispatch = AsyncMock()
        state = {"env_cache": {"sym": cache_state}, "notification_service": notify}

        for _ in range(3):
            svc.on_bootstrap_complete("sym", False, state, error="ruby 9.9.9 unavailable")
            await asyncio.sleep(0)

        assert cache_state.bootstrap_attempts == 3
        assert cache_state.bootstrap_exhausted is True
        await asyncio.sleep(0)
        notify.dispatch.assert_awaited_once()
        event = notify.dispatch.await_args.args[0]
        assert event.event_type == EventType.circuit_breaker_trip

        # A further failure (e.g. in-flight race) must not re-notify.
        svc.on_bootstrap_complete("sym", False, state, error="still broken")
        await asyncio.sleep(0)
        notify.dispatch.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_cooldown_escalates_with_attempts(self, tmp_path: Path) -> None:
        """Retry cooldown is BOOTSTRAP_RETRY_COOLDOWN_S * 2**attempts: after one
        failure the base cooldown is no longer enough."""
        svc = self._make_service()
        sym_cfg = self._make_symphony_config()
        github = AsyncMock()
        github.get_branch_head_sha = AsyncMock(return_value=_head("same_sha"))
        github.get_file_content = AsyncMock(return_value="# README")
        dispatch_fn = AsyncMock()

        cache_state = self._make_cache_state(
            tmp_path,
            readme_sha=_source_fingerprint({"README.md": "same_sha"}),
            last_bootstrap_succeeded=False,
            bootstrap_attempts=1,
            last_bootstrap_at=datetime.now(UTC)
            - timedelta(seconds=BOOTSTRAP_RETRY_COOLDOWN_S + 5),
        )
        state: dict = {"env_cache": {"sym": cache_state}, "config": MagicMock()}
        await svc.check_and_trigger("sym", sym_cfg, github, state, dispatch_fn)
        dispatch_fn.assert_not_called()  # 1 attempt → cooldown doubled to 2x base

        cache_state.last_bootstrap_at = datetime.now(UTC) - timedelta(
            seconds=BOOTSTRAP_RETRY_COOLDOWN_S * 2 + 5,
        )
        await svc.check_and_trigger("sym", sym_cfg, github, state, dispatch_fn)
        dispatch_fn.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_exhausted_blocks_dispatch(self, tmp_path: Path) -> None:
        """Once exhausted, no further bootstrap dispatch for the same spec SHA —
        no matter how much time passes."""
        svc = self._make_service()
        sym_cfg = self._make_symphony_config()
        github = AsyncMock()
        github.get_branch_head_sha = AsyncMock(return_value=_head("same_sha"))
        github.get_file_content = AsyncMock(return_value="# README")
        dispatch_fn = AsyncMock()

        cache_state = self._make_cache_state(
            tmp_path,
            readme_sha=_source_fingerprint({"README.md": "same_sha"}),
            last_bootstrap_succeeded=False,
            bootstrap_attempts=3,
            bootstrap_exhausted=True,
            last_bootstrap_at=datetime.now(UTC) - timedelta(days=2),
        )
        state: dict = {"env_cache": {"sym": cache_state}, "config": MagicMock()}
        await svc.check_and_trigger("sym", sym_cfg, github, state, dispatch_fn)
        dispatch_fn.assert_not_called()

    @pytest.mark.asyncio
    async def test_sha_change_resets_budget_and_redispatches(self, tmp_path: Path) -> None:
        """FR-010: a spec-file change (new SHA) resets the budget and re-enables
        dispatch — fixing the README is the self-healing path."""
        svc = self._make_service()
        sym_cfg = self._make_symphony_config()
        github = AsyncMock()
        github.get_branch_head_sha = AsyncMock(return_value=_head("brand_new_sha"))
        github.get_file_content = AsyncMock(return_value="# README v2")
        dispatch_fn = AsyncMock()

        cache_state = self._make_cache_state(
            tmp_path,
            readme_sha=_source_fingerprint({"README.md": "old_sha"}),
            last_bootstrap_succeeded=False,
            bootstrap_attempts=3,
            bootstrap_exhausted=True,
            last_bootstrap_at=datetime.now(UTC) - timedelta(days=1),
        )
        state: dict = {"env_cache": {"sym": cache_state}, "config": MagicMock()}
        await svc.check_and_trigger("sym", sym_cfg, github, state, dispatch_fn)

        dispatch_fn.assert_awaited_once()
        assert cache_state.bootstrap_attempts == 0
        assert cache_state.bootstrap_exhausted is False

    def test_consumer_hold_detail_names_exhaustion(self, tmp_path: Path) -> None:
        """FR-010: consumer holds must say WHY — an exhausted bootstrap budget is
        named (with the attempt count), not reported as a generic wait."""
        from coordinare.services.env_cache import bootstrap_hold_detail

        exhausted = self._make_cache_state(
            tmp_path, bootstrap_attempts=3, bootstrap_exhausted=True,
            last_bootstrap_error="ruby 9.9.9 unavailable",
        )
        detail = bootstrap_hold_detail(exhausted)
        assert "exhausted" in detail.lower()
        assert "3" in detail

        pending = self._make_cache_state(tmp_path)
        generic = bootstrap_hold_detail(pending)
        assert "exhausted" not in generic.lower()


# ---------------------------------------------------------------------------
# 088 US5 (T028): restart honor path — persisted success is re-verified, not
# blindly re-bootstrapped (and not blindly trusted either).
# ---------------------------------------------------------------------------


class TestRestartHonorPath:
    """A fresh coordinare boot with a persisted successful bootstrap and an
    unchanged spec SHA must run the cheap clean-room verify instead of a full
    bootstrap (SC-004: ready in <2 min). Verify pass ⇒ ready; fail ⇒ full
    bootstrap; un-runnable (None) ⇒ hold readiness until verification can run."""

    def _make_service(self) -> EnvCacheService:
        coordinare_config = _configured(MagicMock())
        coordinare_config.symphonies = []
        coordinare_config.global_config.env_bootstrap_max_attempts = 3
        return EnvCacheService(coordinare_config)

    def _make_symphony_config(self) -> MagicMock:
        cfg = MagicMock()
        cfg.env_bootstrap_performer_id = "bootstrap"
        cfg.env_spec_files = ["README.md"]
        cfg.name = "test-symphony"
        # 092: no test_env block configured (the common case).
        cfg.test_env = None
        eff = MagicMock()
        eff.github_org = "org"
        eff.project_name = "repo"
        eff.base_branch = "main"
        cfg.effective_config = MagicMock(return_value=eff)
        return cfg

    def _make_cache_state(
        self, tmp_path: Path, *, with_activate: bool = True, **overrides,
    ) -> EnvCacheState:
        cache_dir = tmp_path / "env"
        cache_dir.mkdir(exist_ok=True)
        if with_activate:
            (cache_dir / "activate.sh").write_text("#!/bin/sh\n")
        defaults = {
            "symphony_name": "sym",
            "sanitised_name": "sym-abc",
            "cache_dir": cache_dir,
            "readme_sha": _source_fingerprint({"README.md": "same_sha"}),
            "last_bootstrap_succeeded": True,
            "last_bootstrap_at": datetime.now(UTC),
            "cache_dir_ready": True,
        }
        defaults.update(overrides)
        return _accepted_state(**defaults)

    def _make_github(self, sha: str = "same_sha") -> AsyncMock:
        github = AsyncMock()
        github.get_branch_head_sha = AsyncMock(return_value=_head(sha))
        github.get_file_content = AsyncMock(return_value="# README")
        return github

    @pytest.mark.asyncio
    async def test_verify_pass_marks_ready_without_bootstrap(self, tmp_path: Path) -> None:
        """Persisted success + SHA match on a fresh boot → clean verify runs,
        no bootstrap dispatch, and the cache is usable."""
        svc = self._make_service()
        cache_state = self._make_cache_state(tmp_path, cache_dir_ready=False)
        state: dict = {"env_cache": {"sym": cache_state}, "config": MagicMock()}
        dispatch_fn = AsyncMock()
        verify_fn = AsyncMock(return_value=(True, "verify ok"))

        await svc.check_and_trigger(
            "sym", self._make_symphony_config(), self._make_github(), state,
            dispatch_fn, clean_verify_fn=verify_fn,
        )

        verify_fn.assert_awaited_once_with("sym")
        dispatch_fn.assert_not_called()
        assert cache_state.cache_dir_ready is True
        assert cache_state.last_bootstrap_succeeded is True

    @pytest.mark.asyncio
    async def test_verify_failure_triggers_full_bootstrap_immediately(
        self, tmp_path: Path,
    ) -> None:
        """Verify fail ⇒ the persisted success was phantom — downgrade it and
        dispatch a full bootstrap NOW (no retry-cooldown wait: last_bootstrap_at
        is recent here, and the dispatch must still fire)."""
        svc = self._make_service()
        cache_state = self._make_cache_state(
            tmp_path, last_bootstrap_at=datetime.now(UTC),
        )
        state: dict = {"env_cache": {"sym": cache_state}, "config": MagicMock()}
        dispatch_fn = AsyncMock()
        verify_fn = AsyncMock(return_value=(False, "chromium: not found"))

        await svc.check_and_trigger(
            "sym", self._make_symphony_config(), self._make_github(), state,
            dispatch_fn, clean_verify_fn=verify_fn,
        )

        verify_fn.assert_awaited_once()
        dispatch_fn.assert_awaited_once()
        assert cache_state.bootstrap_in_flight is True

    @pytest.mark.asyncio
    async def test_sha_mismatch_bootstraps_without_verify(self, tmp_path: Path) -> None:
        """A spec change means the cache content is stale regardless of its
        health — go straight to a full bootstrap, don't waste a verify run."""
        svc = self._make_service()
        cache_state = self._make_cache_state(
            tmp_path, readme_sha=_source_fingerprint({"README.md": "old_sha"}),
        )
        state: dict = {"env_cache": {"sym": cache_state}, "config": MagicMock()}
        dispatch_fn = AsyncMock()
        verify_fn = AsyncMock(return_value=(True, "verify ok"))

        await svc.check_and_trigger(
            "sym", self._make_symphony_config(), self._make_github("new_sha"), state,
            dispatch_fn, clean_verify_fn=verify_fn,
        )

        verify_fn.assert_not_called()
        dispatch_fn.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_verify_none_holds_persisted_success(self, tmp_path: Path) -> None:
        """Unavailable clean verification cannot preserve ready status."""
        svc = self._make_service()
        cache_state = self._make_cache_state(tmp_path)
        state: dict = {"env_cache": {"sym": cache_state}, "config": MagicMock()}
        dispatch_fn = AsyncMock()
        verify_fn = AsyncMock(return_value=(None, "no verify.sh in cache"))

        await svc.check_and_trigger(
            "sym", self._make_symphony_config(), self._make_github(), state,
            dispatch_fn, clean_verify_fn=verify_fn,
        )

        verify_fn.assert_awaited_once()
        dispatch_fn.assert_not_called()
        assert cache_state.last_bootstrap_succeeded is False
        assert cache_state.cache_dir_ready is False

    @pytest.mark.asyncio
    async def test_verify_runs_only_once_per_process(self, tmp_path: Path) -> None:
        """The honor path is a RESTART check, not a poll-cycle check — the
        second cycle in the same process must not re-run the verify."""
        svc = self._make_service()
        cache_state = self._make_cache_state(tmp_path)
        state: dict = {"env_cache": {"sym": cache_state}, "config": MagicMock()}
        dispatch_fn = AsyncMock()
        verify_fn = AsyncMock(return_value=(True, "verify ok"))
        sym_cfg = self._make_symphony_config()
        github = self._make_github()

        await svc.check_and_trigger(
            "sym", sym_cfg, github, state, dispatch_fn, clean_verify_fn=verify_fn,
        )
        await svc.check_and_trigger(
            "sym", sym_cfg, github, state, dispatch_fn, clean_verify_fn=verify_fn,
        )

        verify_fn.assert_awaited_once()
        dispatch_fn.assert_not_called()

    @pytest.mark.asyncio
    async def test_in_process_success_suppresses_restart_verify(
        self, tmp_path: Path,
    ) -> None:
        """A bootstrap that succeeded IN THIS PROCESS was already clean-verified
        by the daemon's completion path — the honor verify must not re-fire."""
        svc = self._make_service()
        cache_state = self._make_cache_state(tmp_path, last_bootstrap_succeeded=None)
        state: dict = {"env_cache": {"sym": cache_state}, "config": MagicMock()}
        record_verified_cache_fixture(state["env_cache"]["sym"])
        svc.on_bootstrap_complete("sym", True, state)

        dispatch_fn = AsyncMock()
        verify_fn = AsyncMock(return_value=(True, "verify ok"))
        await svc.check_and_trigger(
            "sym", self._make_symphony_config(), self._make_github(), state,
            dispatch_fn, clean_verify_fn=verify_fn,
        )

        verify_fn.assert_not_called()
        dispatch_fn.assert_not_called()

    @pytest.mark.asyncio
    async def test_activate_missing_retrigger_still_fires(self, tmp_path: Path) -> None:
        """087's hollow-cache retrigger must survive the honor path: persisted
        success with NO activate.sh on disk re-bootstraps (the verify is moot —
        the cache demonstrably lost its toolchain)."""
        svc = self._make_service()
        cache_state = self._make_cache_state(
            tmp_path, with_activate=False, last_bootstrap_at=None,
        )
        state: dict = {"env_cache": {"sym": cache_state}, "config": MagicMock()}
        dispatch_fn = AsyncMock()
        verify_fn = AsyncMock(return_value=(True, "verify ok"))

        await svc.check_and_trigger(
            "sym", self._make_symphony_config(), self._make_github(), state,
            dispatch_fn, clean_verify_fn=verify_fn,
        )

        verify_fn.assert_not_called()
        dispatch_fn.assert_awaited_once()


class TestWriteServiceScripts:
    """108/P1: coordinare writes services-{start,stop,health}.sh into <cache>/services/
    deterministically (no dependence on the performer-side manual_override write that
    intermittently left it empty, so the readiness gate had nothing to start)."""

    @staticmethod
    def _pg_redis_models():
        from coordinare_service_inference.schema import ServiceEntry, ServiceInit

        pg = ServiceEntry(
            name="postgres", binary="postgres", version=None, data_dir="/tmp/pg",
            port=45432, why_needed="db", sources=["config/database.yml"], kind="postgres",
            init=ServiceInit(superuser="systemuser", databases=["sampleweb_test"],
                             password_env_var="POSTGRESQL_PASSWORD"),
        )
        redis = ServiceEntry(
            name="redis", binary="redis-server", version=None, data_dir="/tmp/redis",
            port=46379, why_needed="cache", sources=[".env.test"], kind="redis",
            start_args=["redis-server", "--port", "46379", "--dir", "/tmp/redis"],
        )
        return [pg, redis]

    def test_writes_start_stop_health_scripts(self, tmp_path: Path) -> None:
        ok = write_service_scripts(tmp_path, self._pg_redis_models(), symphony="sampleweb", performer_mounts=())
        assert ok is True
        svc = tmp_path / "services"
        for name in ("services-start.sh", "services-stop.sh", "services-health.sh"):
            p = svc / name
            assert p.is_file(), f"missing {name}"
            assert p.stat().st_mode & 0o111, f"{name} not executable"
        # start script actually initializes/starts postgres + redis
        start = (svc / "services-start.sh").read_text()
        assert "initdb" in start  # postgres recipe
        assert "redis-server" in start

    def test_does_not_write_services_json(self, tmp_path: Path) -> None:
        # 113: coordinare writes the .sh scripts but NOT services.json — that file is
        # the performer's apply_manual_override to write (it feeds the inference-cache
        # suffix via load_prior_manifest). A second coordinare writer with a different
        # agent_version/cache_inputs made the suffix oscillate → the dispatch gate
        # held every card forever (readme_sha != last_seen_spec_sha).
        write_service_scripts(tmp_path, self._pg_redis_models(), symphony="sampleweb", performer_mounts=())
        assert not (tmp_path / "services" / "services.json").exists()
        assert (tmp_path / "services" / "services-start.sh").is_file()

    def test_no_services_is_noop(self, tmp_path: Path) -> None:
        assert write_service_scripts(tmp_path, [], symphony="x", performer_mounts=()) is False
        assert not (tmp_path / "services").exists()
