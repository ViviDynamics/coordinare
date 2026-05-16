"""Performer environment caching service (spec 060)."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from coordinare.models.env_cache import BootstrapJobPayload, EnvCacheState
from coordinare.services.service_inference.cache_key import (
    compute_inference_cache_key,
    forced_regen_cache_key,
    load_prior_manifest,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine
    from typing import Protocol

    from coordinare.models.performer_endpoint import VolumeMount

    class _GitHubService(Protocol):
        async def get_file_blob_sha(self, org: str, repo: str, path: str) -> str | None: ...
        async def get_file_content(self, org: str, repo: str, path: str) -> str | None: ...

logger = structlog.get_logger(__name__)

DEFAULT_DEVENV_ROOT = "/devenv"


def sanitise_symphony_name(name: str) -> str:
    """Convert a symphony name to a safe filesystem component.

    Lowercases, replaces disallowed characters with dashes, strips leading/
    trailing dashes, then always appends a 6-char SHA1 suffix derived from
    the original name. The suffix makes every slug globally unique — two
    names that produce the same slug base (e.g. "Foo Bar" and "foo-bar")
    still get distinct suffixes because their SHA1s differ.
    """
    slug = re.sub(r"[^a-z0-9_-]", "-", name.lower()).strip("-")
    if not slug:
        slug = "symphony"
    suffix = hashlib.sha1(name.encode()).hexdigest()[:6]
    return f"{slug}-{suffix}"


def get_env_volume_for_symphony(
    symphony_name: str,
    env_cache_states: dict[str, Any],
    *,
    is_bootstrap: bool,
    container_devenv_root: str = DEFAULT_DEVENV_ROOT,
) -> tuple[VolumeMount, str] | None:
    """Return (VolumeMount, container_path) for the symphony's env cache, or None.

    Returns rw for bootstrap performers, ro for all others.
    Returns None if the symphony has no env cache state or the cache dir
    does not yet exist on disk. The container_path is
    ``{container_devenv_root}/{sanitised_name}``.

    The ``cache_dir_ready`` gate only applies to consumer (ro) mounts. Bootstrap
    dispatches must mount the host cache dir rw even when it has not yet been
    populated — that is precisely the run that populates it. Without this
    carve-out the bootstrap container has no host volume backing
    ``cache_mount_path``, so installs land in the ephemeral container fs and
    the host cache stays empty forever (chicken-and-egg).
    """
    from coordinare.models.performer_endpoint import VolumeMount

    state = env_cache_states.get(symphony_name)
    if state is None or not isinstance(state, EnvCacheState):
        return None
    if not is_bootstrap and not state.cache_dir_ready:
        return None
    container_path = f"{container_devenv_root}/{state.sanitised_name}"
    return VolumeMount(
        host_path=state.cache_dir,
        container_path=container_path,
        mode="rw" if is_bootstrap else "ro",
    ), container_path


def _collect_env_volumes_for_persistent_performer(
    env_cache_states: dict[str, Any],
    container_devenv_root: str = DEFAULT_DEVENV_ROOT,
) -> list[VolumeMount]:
    """Return ro VolumeMounts for all ready symphony env caches.

    Used when dispatching to persistent performers. The returned mounts are
    passed as ``extra_volumes`` to ``dispatch_card``. Note: if the persistent
    container was started before the env cache became ready, Docker cannot add
    the volume to the running container — coordinare logs a warning in that case
    and the operator must restart the performer container to pick up the mount.
    """
    from coordinare.models.performer_endpoint import VolumeMount

    mounts: list[VolumeMount] = []
    for state in env_cache_states.values():
        if not isinstance(state, EnvCacheState) or not state.cache_dir_ready:
            continue
        container_path = f"{container_devenv_root}/{state.sanitised_name}"
        mounts.append(
            VolumeMount(
                host_path=state.cache_dir,
                container_path=container_path,
                mode="ro",
            )
        )
    return mounts


class EnvCacheService:
    """Manages per-symphony env-cache bootstrapping and SHA change detection."""

    def __init__(self, coordinare_config: Any) -> None:
        self._coordinare_config = coordinare_config

    async def initialise(
        self,
        env_cache: dict[str, Any],
        symphony_github_services: dict[str, _GitHubService],
    ) -> None:
        """Seed env_cache state, create cache dirs, fetch initial SHAs."""
        if TYPE_CHECKING:
            from coordinare.config import CoordinareConfiguration
        cfg: CoordinareConfiguration = self._coordinare_config
        global_config = cfg.global_config
        env_cache_root = Path(global_config.env_cache_root)

        for symphony in cfg.symphonies:
            if symphony.env_bootstrap_performer_id is None:
                continue

            sanitised = sanitise_symphony_name(symphony.name)
            cache_dir = env_cache_root / sanitised

            try:
                cache_dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                logger.warning(
                    "env_cache.mkdir_failed",
                    symphony=symphony.name,
                    path=str(cache_dir),
                    error=str(exc),
                )
                continue

            # 061: Do NOT seed readme_sha or mark cache_dir_ready here. The
            # directory existing on disk is not the same as a successful
            # bootstrap having populated it. Leaving readme_sha=None forces
            # check_and_trigger to dispatch a bootstrap on the first cycle;
            # cache_dir_ready flips to True only after on_bootstrap_complete
            # records a success. Otherwise performers would mount an empty
            # cache directory ro and find no installed tools.
            env_cache[symphony.name] = EnvCacheState(
                symphony_name=symphony.name,
                sanitised_name=sanitised,
                cache_dir=cache_dir,
                readme_sha=None,
                cache_dir_ready=False,
            )
            logger.info(
                "env_cache.initialised",
                symphony=symphony.name,
                cache_dir=str(cache_dir),
            )

    async def _fetch_combined_sha(
        self,
        symphony_name: str,
        spec_files: list[str],
        github_service: _GitHubService,
        github_org: str,
        github_repo: str,
    ) -> str | None:
        """Fetch blob SHA for each spec file and return a single combined hash.

        Files are sorted alphabetically before hashing so the result is
        order-independent. Returns None and logs a warning on any fetch failure.
        """
        per_file: dict[str, str] = {}
        for spec_file in spec_files:
            try:
                sha = await github_service.get_file_blob_sha(github_org, github_repo, spec_file)
            except Exception as exc:
                logger.warning(
                    "env_cache.sha_fetch_failed",
                    symphony=symphony_name,
                    file=spec_file,
                    error=str(exc),
                )
                return None
            if sha is None:
                logger.warning(
                    "env_cache.sha_fetch_none", symphony=symphony_name, file=spec_file
                )
                return None
            per_file[spec_file] = sha

        serialised = json.dumps(dict(sorted(per_file.items())), separators=(",", ":"))
        return hashlib.sha256(serialised.encode()).hexdigest()[:12]

    async def _inference_cache_suffix(
        self,
        *,
        symphony_name: str,
        cache_dir: Path,
        github_service: _GitHubService,
        github_org: str,
        github_repo: str,
    ) -> str:
        """Spec 063 Phase 3: suffix derived from the prior services.json cache_inputs.

        Returns an empty string when no prior manifest is sealed — this keeps
        the first-run readme_sha identical to pre-063 behaviour. Once a manifest
        exists, returns a 12-char hash that changes whenever any tracked
        cache_input file's content changes (or the agent_version changes).
        """
        prior = load_prior_manifest(cache_dir)
        if prior is None:
            return ""

        async def _fetch(path: str) -> str | None:
            try:
                return await github_service.get_file_content(
                    github_org, github_repo, path
                )
            except Exception as exc:
                logger.warning(
                    "env_cache.inference_cache_input_fetch_failed",
                    symphony=symphony_name,
                    file=path,
                    error=str(exc),
                )
                return None

        key = await compute_inference_cache_key(
            agent_version=prior.agent_version,
            prior_manifest=prior,
            content_fetcher=_fetch,
        )
        return key[:12]

    async def check_and_trigger(
        self,
        symphony_name: str,
        symphony_config: Any,
        github_service: _GitHubService,
        state: dict[str, Any],
        dispatch_fn: Callable[[str, BootstrapJobPayload], Coroutine[Any, Any, None]],
        container_devenv_root: str = DEFAULT_DEVENV_ROOT,
    ) -> None:
        """Check for README SHA changes and dispatch bootstrap if needed.

        FR-008: returns immediately if symphony has no env_bootstrap configured.
        """
        if symphony_config.env_bootstrap_performer_id is None:
            return

        env_cache: dict[str, Any] = state.get("env_cache") or {}
        cache_state: EnvCacheState | None = env_cache.get(symphony_name)
        if cache_state is None:
            return

        global_config = self._coordinare_config.global_config
        eff_config = symphony_config.effective_config(global_config)
        if eff_config is None:
            logger.warning("env_cache.eff_config_none", symphony=symphony_name)
            return

        # 063 Phase 4 (T024): runtime health failure flag → forced regen.
        # Bypass the cache_inputs key entirely so the next bootstrap rebuilds
        # the cache and re-runs inference against a fresh agent pass.
        if cache_state.runtime_health_failed and not cache_state.bootstrap_in_flight:
            prior = load_prior_manifest(cache_state.cache_dir)
            agent_version = prior.agent_version if prior is not None else "no-prior-manifest"
            forced_sha = forced_regen_cache_key(agent_version)
            logger.info(
                "env_cache.forced_regen_triggered",
                symphony=symphony_name,
                agent_version=agent_version,
            )
            cache_state.runtime_health_failed = False
            await self._do_dispatch(
                symphony_name=symphony_name,
                symphony_config=symphony_config,
                github_service=github_service,
                eff_config=eff_config,
                current_sha=forced_sha,
                cache_state=cache_state,
                dispatch_fn=dispatch_fn,
                container_devenv_root=container_devenv_root,
            )
            return

        # Fetch combined blob SHA for all watched files (metadata-only, cheap).
        current_sha = await self._fetch_combined_sha(
            symphony_name,
            symphony_config.env_spec_files,
            github_service,
            eff_config.github_org,
            eff_config.project_name or symphony_name,
        )
        # 063 Phase 3: layer in inference cache_inputs once a manifest is sealed.
        # Empty on first run, so existing readme_sha behaviour is unchanged.
        if current_sha is not None:
            suffix = await self._inference_cache_suffix(
                symphony_name=symphony_name,
                cache_dir=cache_state.cache_dir,
                github_service=github_service,
                github_org=eff_config.github_org,
                github_repo=eff_config.project_name or symphony_name,
            )
            if suffix:
                current_sha = f"{current_sha}:{suffix}"
        if current_sha is None:
            logger.warning(
                "env_cache.sha_fetch_skipped",
                symphony=symphony_name,
                reason="one or more spec files could not be fetched; bootstrap cycle skipped",
            )
            return

        needs_bootstrap = cache_state.readme_sha is None or current_sha != cache_state.readme_sha

        if not needs_bootstrap:
            return

        if cache_state.bootstrap_in_flight:
            # Queue the new SHA and wait for the in-flight bootstrap to finish.
            cache_state.pending_sha = current_sha
            logger.debug(
                "env_cache.bootstrap_queued",
                symphony=symphony_name,
                pending_sha=current_sha,
            )
            return

        await self._do_dispatch(
            symphony_name=symphony_name,
            symphony_config=symphony_config,
            github_service=github_service,
            eff_config=eff_config,
            current_sha=current_sha,
            cache_state=cache_state,
            dispatch_fn=dispatch_fn,
            container_devenv_root=container_devenv_root,
        )

    async def _do_dispatch(
        self,
        *,
        symphony_name: str,
        symphony_config: Any,
        github_service: _GitHubService,
        eff_config: Any,
        current_sha: str,
        cache_state: EnvCacheState,
        dispatch_fn: Callable[[str, BootstrapJobPayload], Coroutine[Any, Any, None]],
        container_devenv_root: str = DEFAULT_DEVENV_ROOT,
    ) -> None:
        repo = eff_config.project_name or symphony_name
        env_spec_contents: dict[str, str] = {}
        for spec_file in symphony_config.env_spec_files:
            try:
                content = await github_service.get_file_content(
                    eff_config.github_org,
                    repo,
                    spec_file,
                )
                env_spec_contents[spec_file] = content or ""
            except Exception as exc:
                logger.warning(
                    "env_cache.content_fetch_failed",
                    symphony=symphony_name,
                    file=spec_file,
                    error=str(exc),
                )
                return

        cache_mount_path = f"{container_devenv_root}/{cache_state.sanitised_name}"
        payload = BootstrapJobPayload(
            symphony_name=symphony_name,
            symphony_org=eff_config.github_org,
            symphony_repo=repo,
            env_spec_files=symphony_config.env_spec_files,
            env_spec_contents=env_spec_contents,
            cache_mount_path=cache_mount_path,
        )

        try:
            await dispatch_fn(symphony_config.env_bootstrap_performer_id, payload)
        except Exception as exc:
            logger.warning(
                "env_cache.dispatch_failed",
                symphony=symphony_name,
                error=str(exc),
            )
            return

        cache_state.readme_sha = current_sha
        cache_state.bootstrap_in_flight = True
        logger.info(
            "env_cache.bootstrap_dispatched",
            symphony=symphony_name,
            performer_id=symphony_config.env_bootstrap_performer_id,
            new_sha=current_sha,
        )

    def mark_runtime_health_failed(
        self,
        symphony_name: str,
        state: dict[str, Any],
    ) -> None:
        """063 Phase 4 (T024): flag this symphony's env-cache for forced regen.

        Called by the coordinare daemon when a performer reports a non-zero
        services-health.sh exit. The next check_and_trigger cycle observes
        ``runtime_health_failed`` and dispatches a bootstrap whose cache key
        is generated via ``forced_regen_cache_key`` (time-nonce mixed), so the
        cache rebuilds even if no spec-file or cache_input has changed.
        """
        env_cache: dict[str, Any] = state.get("env_cache") or {}
        cache_state: EnvCacheState | None = env_cache.get(symphony_name)
        if cache_state is None:
            logger.warning(
                "env_cache.runtime_health_failed_no_state",
                symphony=symphony_name,
            )
            return
        cache_state.runtime_health_failed = True
        logger.warning(
            "env_cache.runtime_health_failed",
            symphony=symphony_name,
        )

    def record_inference_outcome(
        self,
        symphony_name: str,
        state: dict[str, Any],
        *,
        skipped_reason: str | None,
        agent_version: str | None,
        attempts: int | None,
        succeeded: bool | None,
        services: list[str],
    ) -> None:
        """063 T026d: stamp the latest service-inference summary onto state.

        Called by the coordinare daemon when a bootstrap job reports terminal
        status, before ``on_bootstrap_complete``. The dashboard reads these
        fields to show operators what the agent produced (or why it didn't
        run) without having to inspect the env-cache directory.
        """
        env_cache: dict[str, Any] = state.get("env_cache") or {}
        cache_state: EnvCacheState | None = env_cache.get(symphony_name)
        if cache_state is None:
            return
        cache_state.last_inference_at = datetime.now(UTC)
        cache_state.last_inference_skipped_reason = skipped_reason
        cache_state.last_inference_agent_version = agent_version
        cache_state.last_inference_attempts = attempts
        cache_state.last_inference_succeeded = succeeded
        cache_state.last_inference_services = list(services or [])
        logger.info(
            "env_cache.inference_recorded",
            symphony=symphony_name,
            skipped_reason=skipped_reason,
            agent_version=agent_version,
            attempts=attempts,
            succeeded=succeeded,
            services=services,
        )

    def on_bootstrap_complete(
        self,
        symphony_name: str,
        success: bool,
        state: dict[str, Any],
    ) -> None:
        """Called when a bootstrap performer session ends."""
        env_cache: dict[str, Any] = state.get("env_cache") or {}
        cache_state: EnvCacheState | None = env_cache.get(symphony_name)
        if cache_state is None:
            return

        cache_state.bootstrap_in_flight = False
        cache_state.last_bootstrap_at = datetime.now(UTC)
        cache_state.last_bootstrap_succeeded = success

        # 061: Mark the cache usable only after a successful bootstrap.
        # cache_dir_ready stays False until at least one bootstrap succeeds,
        # which prevents performers from mounting an unpopulated cache dir.
        if success:
            cache_state.cache_dir_ready = True

        # On failure, clear the recorded SHA so the next cycle sees a mismatch
        # and retries the bootstrap rather than leaving the cache poisoned.
        # cache_dir_ready is intentionally NOT cleared on failure: if a prior
        # bootstrap succeeded, the existing cache is still usable by consumers
        # while we retry. Only an explicit success flips the gate on.
        if not success:
            cache_state.readme_sha = None

        logger.info(
            "env_cache.bootstrap_complete",
            symphony=symphony_name,
            success=success,
        )

        # Fire queued bootstrap if a SHA change arrived while in-flight.
        if cache_state.pending_sha is not None:
            pending = cache_state.pending_sha
            cache_state.pending_sha = None
            # Re-trigger is deferred to the next check_and_trigger() call by
            # leaving readme_sha at the old value so the next cycle detects the diff.
            # Overwrite readme_sha only after confirming the pending one differs.
            if pending != cache_state.readme_sha:
                cache_state.readme_sha = None  # force re-check on next cycle
                logger.info(
                    "env_cache.pending_sha_queued_for_next_cycle",
                    symphony=symphony_name,
                    pending_sha=pending,
                )
