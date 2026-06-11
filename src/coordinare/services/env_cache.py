"""Performer environment caching service (spec 060)."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog
from coordinare_service_inference.cache_key import (
    compute_inference_cache_key,
    forced_regen_cache_key,
    load_prior_manifest,
)

from coordinare.models.env_cache import BootstrapJobPayload, EnvCacheState
from coordinare.services.env_manifest import (
    STRUCTURED_SPEC_FILES,
    derive_manifest,
    render_activate_sh,
    render_checklist,
    render_verify_sh,
)
from coordinare.services.env_manifest_llm import enrich_from_readme

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine
    from typing import Protocol

    from coordinare.models.performer_endpoint import VolumeMount
    from coordinare.services.env_manifest_llm import ChatJson

    class _GitHubService(Protocol):
        async def get_file_blob_sha(self, org: str, repo: str, path: str) -> str | None: ...
        async def get_file_content(self, org: str, repo: str, path: str) -> str | None: ...

logger = structlog.get_logger(__name__)

DEFAULT_DEVENV_ROOT = "/devenv"

# 077: when a bootstrap FAILED for the current spec SHA, auto-retry it on a
# later cycle rather than waiting for the README to change.  The cooldown keeps
# a failing bootstrap from spinning up a fresh container on every poll cycle.
BOOTSTRAP_RETRY_COOLDOWN_S = 120.0


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


def _cache_dir_has_activate(cache_dir: Path) -> bool:
    """Return True iff the host cache_dir has a populated activate.sh.

    The on-disk presence of activate.sh is the authoritative signal that a
    prior bootstrap left a usable toolchain — independent of the in-process
    ``cache_dir_ready`` flag, which resets on every coordinare restart.
    Letting consumer performers mount a stale-but-real cache is strictly
    better than dispatching them with no toolchain at all.
    """
    try:
        return (cache_dir / "activate.sh").is_file()
    except OSError:
        return False


def get_env_volume_for_symphony(
    symphony_name: str,
    env_cache_states: dict[str, Any],
    *,
    is_bootstrap: bool,
    container_devenv_root: str = DEFAULT_DEVENV_ROOT,
) -> tuple[VolumeMount, str] | None:
    """Return (VolumeMount, container_path) for the symphony's env cache, or None.

    Returns rw for bootstrap performers, ro for all others.
    Returns None if the symphony has no env cache state, or — for consumers —
    no activate.sh exists on disk. The container_path is
    ``{container_devenv_root}/{sanitised_name}``.

    Bootstrap dispatches always get the rw mount even when activate.sh is
    missing — that's the run that creates it. Without this carve-out the
    bootstrap container has no host volume backing ``cache_mount_path``, so
    installs land in the ephemeral container fs and the host cache stays
    empty forever (chicken-and-egg).

    Consumers gate on activate.sh existence (not the runtime ``cache_dir_ready``
    flag) so a populated cache from a prior coordinare run is reused across
    restarts. While a fresh bootstrap is in flight, consumers race it on the
    same directory — stale-but-real beats no toolchain, and the next bootstrap
    supersedes whatever they used.
    """
    from coordinare.models.performer_endpoint import VolumeMount

    state = env_cache_states.get(symphony_name)
    if state is None or not isinstance(state, EnvCacheState):
        return None
    if not is_bootstrap and not _cache_dir_has_activate(state.cache_dir):
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
    """Return ro VolumeMounts for every symphony env cache that has an
    activate.sh on disk.

    Used when dispatching to persistent performers. The returned mounts are
    passed as ``extra_volumes`` to ``dispatch_card``. Note: if the persistent
    container was started before the env cache became populated, Docker cannot
    add the volume to the running container — coordinare logs a warning in that
    case and the operator must restart the performer container to pick up the
    mount.
    """
    from coordinare.models.performer_endpoint import VolumeMount

    mounts: list[VolumeMount] = []
    for state in env_cache_states.values():
        if not isinstance(state, EnvCacheState):
            continue
        if not _cache_dir_has_activate(state.cache_dir):
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
        llm_chat: ChatJson | None = None,
    ) -> None:
        """Check for README SHA changes and dispatch bootstrap if needed.

        FR-008: returns immediately if symphony has no env_bootstrap configured.

        ``llm_chat`` (optional) is wired by the daemon to the coordinare's LLM and
        used for the best-effort README pass that augments the deterministic
        manifest with system packages described only in prose.
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
                llm_chat=llm_chat,
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

        # 077: record the current spec SHA every cycle (even when no bootstrap is
        # needed) so consumer dispatch can gate on "cache matches current spec".
        cache_state.last_seen_spec_sha = current_sha

        needs_bootstrap = cache_state.readme_sha is None or current_sha != cache_state.readme_sha

        # 077: break the failed-bootstrap deadlock.  readme_sha is set
        # optimistically at dispatch time (and persisted in the snapshot), so a
        # bootstrap that FAILED can leave readme_sha == current_sha while
        # last_bootstrap_succeeded is False.  Without this, needs_bootstrap stays
        # False forever, the consumer-gate holds indefinitely, and the only way
        # to recover is a manual trigger.  Retry on failure, but honour a
        # cooldown so we don't relaunch a container every poll cycle.
        if not needs_bootstrap and cache_state.last_bootstrap_succeeded is False:
            last_at = cache_state.last_bootstrap_at
            cooldown_elapsed = (
                last_at is None
                or (datetime.now(UTC) - last_at).total_seconds() >= BOOTSTRAP_RETRY_COOLDOWN_S
            )
            if cooldown_elapsed:
                needs_bootstrap = True
                # Only announce the retry when it will actually dispatch — not on
                # every poll cycle while a prior retry is still in-flight (the
                # in_flight guard below would otherwise log this repeatedly).
                if not cache_state.bootstrap_in_flight:
                    logger.info(
                        "env_cache.bootstrap_retry_after_failure",
                        symphony=symphony_name,
                        sha=current_sha,
                        last_error=cache_state.last_bootstrap_error,
                    )

        # 087: break the phantom-success deadlock. A persisted snapshot can claim
        # last_bootstrap_succeeded=True with readme_sha == current_sha while the
        # cache dir on disk has lost activate.sh (wiped between runs, or the
        # snapshot outlived the cache). The consumer gate checks activate.sh on
        # disk and holds every card; if the trigger trusts the persisted success
        # it never re-fires → permanent deadlock recoverable only by hand. Mirror
        # the consumer gate: when the cache claims success but activate.sh is
        # absent, force a re-bootstrap (honouring the same cooldown as the
        # failed-bootstrap retry so we don't relaunch a container every cycle).
        if (
            not needs_bootstrap
            and cache_state.last_bootstrap_succeeded is True
            and not _cache_dir_has_activate(cache_state.cache_dir)
        ):
            last_at = cache_state.last_bootstrap_at
            cooldown_elapsed = (
                last_at is None
                or (datetime.now(UTC) - last_at).total_seconds()
                >= BOOTSTRAP_RETRY_COOLDOWN_S
            )
            if cooldown_elapsed:
                needs_bootstrap = True
                if not cache_state.bootstrap_in_flight:
                    logger.warning(
                        "env_cache.bootstrap_retry_activate_missing",
                        symphony=symphony_name,
                        sha=current_sha,
                        cache_dir=str(cache_state.cache_dir),
                        detail=(
                            "cache claims last_bootstrap_succeeded but activate.sh "
                            "is absent on disk — re-bootstrapping to repopulate."
                        ),
                    )

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
            llm_chat=llm_chat,
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
        llm_chat: ChatJson | None = None,
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

        # 077: derive an authoritative dependency manifest from the symphony's
        # structured project files (best-effort: missing files are normal), then
        # layer in README-described system packages via the optional LLM pass.
        # The manifest drives BOTH the install checklist (persona) and the
        # verification (coordinare-written verify.sh) so a pinned tool version
        # can't be silently missed by a free-forming agent.
        dependency_checklist, verify_provided, activate_provided = await self._build_manifest_artifacts(
            symphony_name=symphony_name,
            repo=repo,
            github_org=eff_config.github_org,
            github_service=github_service,
            readme_contents=env_spec_contents,
            current_sha=current_sha,
            cache_dir=cache_state.cache_dir,
            cache_mount_path=cache_mount_path,
            llm_chat=llm_chat,
        )

        payload = BootstrapJobPayload(
            symphony_name=symphony_name,
            symphony_org=eff_config.github_org,
            symphony_repo=repo,
            env_spec_files=symphony_config.env_spec_files,
            env_spec_contents=env_spec_contents,
            cache_mount_path=cache_mount_path,
            # 077: feed the prior attempt's verification failure back to the agent
            # so it fixes the specific gap (e.g. a pinned Ruby version) on retry.
            last_failure=cache_state.last_bootstrap_error,
            dependency_checklist=dependency_checklist,
            verify_provided=verify_provided,
            activate_provided=activate_provided,
        )

        # Mark in-flight BEFORE dispatching. dispatch_fn may fail SYNCHRONOUSLY
        # (e.g. a readiness timeout inside dispatch_card) and call
        # on_bootstrap_complete(False) itself — which clears bootstrap_in_flight.
        # If we set bootstrap_in_flight=True AFTER dispatch_fn returns, we clobber
        # that reset and wedge in_flight=True forever with no container and no poll
        # task: a stale-in-flight deadlock that blocks every consumer AND the
        # auto-retry (its `if bootstrap_in_flight: return` guard). Setting it first
        # lets the in-handler reset stick; the async-success path leaves it True
        # until the poll task fires completion.
        cache_state.readme_sha = current_sha
        cache_state.bootstrap_in_flight = True
        logger.info(
            "env_cache.bootstrap_dispatched",
            symphony=symphony_name,
            performer_id=symphony_config.env_bootstrap_performer_id,
            new_sha=current_sha,
        )
        try:
            await dispatch_fn(symphony_config.env_bootstrap_performer_id, payload)
        except Exception as exc:
            logger.warning(
                "env_cache.dispatch_failed",
                symphony=symphony_name,
                error=str(exc),
            )
            # Roll back the optimistic in-flight state so the next cycle retries
            # instead of wedging on a dispatch that never produced a job.
            cache_state.bootstrap_in_flight = False
            cache_state.readme_sha = None
            return

    async def _build_manifest_artifacts(
        self,
        *,
        symphony_name: str,
        repo: str,
        github_org: str,
        github_service: _GitHubService,
        readme_contents: dict[str, str],
        current_sha: str,
        cache_dir: Path,
        cache_mount_path: str,
        llm_chat: ChatJson | None,
    ) -> tuple[str | None, bool]:
        """Derive the manifest, write an authoritative verify.sh AND activate.sh
        into the cache, and return ``(dependency_checklist, verify_provided,
        activate_provided)``.

        Best-effort throughout: any failure (no parseable deps, fetch/IO error)
        returns ``(None, False, False)`` so the bootstrap falls back to the prior
        agent-writes-verify/activate behaviour rather than blocking.
        """
        # Fetch structured project files (missing files are normal — skip them).
        struct_contents: dict[str, str] = {}
        for spec_file in STRUCTURED_SPEC_FILES:
            try:
                content = await github_service.get_file_content(github_org, repo, spec_file)
            except Exception:
                content = None
            if content:
                struct_contents[spec_file] = content

        manifest = derive_manifest(symphony_name, struct_contents, spec_sha=current_sha)

        # Best-effort README pass for system packages described only in prose.
        readme_text = readme_contents.get("README.md") or next(
            (c for c in readme_contents.values() if c), ""
        )
        manifest = await enrich_from_readme(manifest, readme_text, llm_chat)

        if not manifest.items:
            logger.info("env_cache.manifest_empty", symphony=symphony_name)
            return None, False, False

        # Write the authoritative verify.sh + activate.sh + manifest.json into the
        # host cache dir (they appear at cache_mount_path inside consumer
        # containers). 087: coordinare owns activate.sh too — an auto-discovering
        # activation that verify.sh sources, so the agent installs the toolchain
        # but no longer hand-writes the (fumbled) activation paths.
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
            verify_path = cache_dir / "verify.sh"
            verify_path.write_text(render_verify_sh(manifest, cache_mount_path=cache_mount_path))
            verify_path.chmod(0o755)
            activate_path = cache_dir / "activate.sh"
            activate_path.write_text(
                render_activate_sh(manifest, cache_mount_path=cache_mount_path)
            )
            activate_path.chmod(0o755)
            (cache_dir / "manifest.json").write_text(manifest.model_dump_json(indent=2))
        except OSError as exc:
            logger.warning(
                "env_cache.manifest_write_failed", symphony=symphony_name, error=str(exc)
            )
            return render_checklist(manifest), False, False

        logger.info(
            "env_cache.manifest_derived",
            symphony=symphony_name,
            items=len(manifest.items),
            runtimes=[f"{i.name}=={i.version}" for i in manifest.runtime_pins()],
            llm_derived=manifest.llm_derived,
        )
        return render_checklist(manifest), True, True

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
        error: str | None = None,
    ) -> None:
        """Called when a bootstrap performer session ends.

        ``error`` is a human-readable failure reason (verify output, dispatch
        error, reap reason); stored on the cache state for the dashboard and
        cleared on success.
        """
        env_cache: dict[str, Any] = state.get("env_cache") or {}
        cache_state: EnvCacheState | None = env_cache.get(symphony_name)
        if cache_state is None:
            return

        cache_state.bootstrap_in_flight = False
        cache_state.last_bootstrap_at = datetime.now(UTC)
        cache_state.last_bootstrap_succeeded = success
        # 077: surface WHY a bootstrap failed (dashboard); clear on success.
        cache_state.last_bootstrap_error = None if success else (error or "bootstrap failed")

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
