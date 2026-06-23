"""Performer environment caching service (spec 060)."""

from __future__ import annotations

import asyncio
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
from coordinare.services.test_env_loader import (
    TestEnvFileError,
    load_test_env,
    parse_test_env,
)

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


def bootstrap_hold_detail(cache_state: EnvCacheState) -> str:
    """088 (FR-010): human-readable reason a consumer dispatch is held.

    Names the exhausted circuit breaker explicitly (with the attempt count and
    last error) so an operator reading the hold log knows the system has
    STOPPED retrying and why — instead of a generic 'waiting for bootstrap'."""
    if cache_state.bootstrap_exhausted:
        last_error = (cache_state.last_bootstrap_error or "unknown failure").strip()
        return (
            f"Env bootstrap budget exhausted after {cache_state.bootstrap_attempts} "
            f"failed attempt(s) — no further bootstraps will be dispatched for this "
            f"spec. Last failure: {last_error}. Fix the env spec files (a SHA change "
            f"resets the budget) or repair the cache and restart."
        )
    return (
        "Holding dispatch until env_bootstrap has SUCCESSFULLY completed for "
        "the current spec. Card retries on the next pickup cycle."
    )


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


# 092: the three files coordinare itself seeds into every cache_dir before a
# bootstrap runs (render_activate_sh / render_verify_sh / the manifest dump).
# A successful bootstrap must leave the toolchain *beside* these — if the mount
# contains only the seed set after a "successful" run, the agent installed the
# toolchain somewhere other than the absolute cache_mount_path it was given
# (the observed glm-4.7-flash failure: a relative-path build under
# <cwd>/devenv/<name> while the real mount stayed empty). See the absolute-path
# block in http_performer_service._build_env_bootstrap_payload.
_CACHE_SEED_FILES = frozenset({"activate.sh", "manifest.json", "verify.sh"})


def _cache_dir_is_populated(cache_dir: Path) -> bool:
    """Return True iff cache_dir holds toolchain content beyond the seed set.

    Used to fail a "successful" bootstrap loudly when the agent built the
    toolchain in the wrong location and the real mount is still just the three
    coordinare-seeded files. A non-empty result requires at least one entry whose
    name is not in ``_CACHE_SEED_FILES``.
    """
    try:
        return any(entry.name not in _CACHE_SEED_FILES for entry in cache_dir.iterdir())
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


async def resolve_test_env_vars(
    *,
    symphony_name: str,
    test_env: Any,
    github_org: str,
    repo: str,
    github_service: _GitHubService,
    fallback_source: str | None = None,
) -> dict[str, str]:
    """Resolve a symphony's test-environment vars to literal ``KEY -> value`` pairs (092).

    Shared by the env-bootstrap dispatch (``EnvCacheService._load_test_env_vars``) and
    the consumer/QA-runtime dispatch (``dispatch_performer``) so both apply identical
    secret-handling. The coordinare has no on-disk clone at dispatch, so the two
    *configured* sources are resolved differently:

    * ``host_path`` — read directly off the coordinare host via ``load_test_env``.
    * ``repo_path`` — fetched through the GitHub API and parsed via ``parse_test_env``
      (clone-relative, containment already enforced at config-load; ``get_file_content``
      cannot escape the repo).

    092 US2 fallback: when no ``test_env`` block is configured but a prior inference
    persisted an agent-discovered ``fallback_source`` PATH, reload that same repo-relative
    file through the GitHub API. The configured block always wins (this branch only runs
    when ``test_env`` is ``None``). A missing fallback file is best-effort — the
    genuinely-unset var still trips the services-start.sh exit-75 gate downstream.

    Returns ``{}`` when neither a configured ``test_env`` nor a fallback source is present.
    Raises :class:`TestEnvFileError` when a *configured* source is missing — a clear
    error, never a silent empty dict.

    Logs keys + source only, never values (spec-091/092 secret invariant).
    """
    if test_env is None:
        if not fallback_source:
            return {}
        # Discovered path-only fallback: repo-relative, fetched via the API.
        content = await github_service.get_file_content(github_org, repo, fallback_source)
        if content is None:
            # Best-effort: a vanished discovered file is not a config error.
            logger.warning(
                "env_cache.test_env_fallback_missing",
                symphony=symphony_name,
                source=f"test_env_source:{fallback_source}",
            )
            return {}
        loaded = parse_test_env(content)
        logger.info(
            "env_cache.test_env_loaded",
            symphony=symphony_name,
            source=f"test_env_source:{fallback_source}",
            keys=sorted(loaded.keys()),
        )
        return loaded

    if test_env.host_path is not None:
        # repo_root is unused for host_path resolution; pass a placeholder.
        loaded = load_test_env(test_env, repo_root=Path("/"))
        source = f"host_path:{test_env.host_path}"
    else:
        content = await github_service.get_file_content(
            github_org, repo, test_env.repo_path
        )
        if content is None:
            msg = (
                f"test_env file not found in repo: {test_env.repo_path!r} "
                "(configured via repo_path); a configured test-env file must exist"
            )
            raise TestEnvFileError(msg)
        loaded = parse_test_env(content)
        source = f"repo_path:{test_env.repo_path}"

    logger.info(
        "env_cache.test_env_loaded",
        symphony=symphony_name,
        source=source,
        keys=sorted(loaded.keys()),
    )
    return loaded


async def verify_env_cache_clean(
    state: dict[str, Any], symphony_name: str, svc: Any
) -> tuple[bool | None, str]:
    """077/093: run the cache's ``verify.sh`` in a CLEAN consumer-context
    container — the performer image with ONLY the cache mounted read-only at
    the same path consumers use — so a broken consumer-facing ``activate.sh``
    can't false-pass via installs the bootstrap container did online.

    verify.sh sources activate.sh and asserts every dependency is runnable
    (it installs from the cache's local debs, no network), so this is a
    faithful "does the cache alone provide a working toolchain" check.

    Returns ``(passed, detail)``: ``passed`` is True/False when verify.sh
    ran, or None when it can't be run (absent script / docker error) — None
    is degraded and does NOT downgrade success, to avoid looping on infra
    errors. ``detail`` is a concise human-readable reason (the verify FAIL
    lines) for the dashboard.

    Pure over existing seams (``state``, ``symphony_name``, ``svc``) so both the
    daemon method and the dispatch-time readiness gate (093) share one source of
    truth with no drift. Emits observability with NAMES/PATHS only.
    """
    from coordinare.services.http_performer_service import HTTPPerformerService

    ec = (state.get("env_cache") or {}).get(symphony_name)
    if not isinstance(ec, EnvCacheState):
        return None, "no env-cache state for symphony"
    cache_dir = Path(ec.cache_dir)
    if not (cache_dir / "verify.sh").is_file():
        logger.warning(
            "env_cache.clean_verify_skipped",
            symphony=symphony_name,
            reason="no verify.sh in cache — cannot confirm consumer install",
        )
        return None, "no verify.sh in cache (cannot confirm consumer install)"
    devenv_root = (
        svc.devenv_root if isinstance(svc, HTTPPerformerService) else DEFAULT_DEVENV_ROOT
    )
    image = getattr(getattr(svc, "_config", None), "image", None) or "coordinare-performer:full"
    # Mount at the SAME container path the bootstrap used, since activate.sh /
    # verify.sh hardcode that absolute DEVEENV path.
    container_path = f"{devenv_root}/{ec.sanitised_name}"
    cmd = [
        "docker", "run", "--rm",
        "-v", f"{cache_dir}:{container_path}:ro",
        "--entrypoint", "bash", str(image),
        f"{container_path}/verify.sh",
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out_b, _ = await asyncio.wait_for(proc.communicate(), timeout=300.0)
    except Exception as exc:
        logger.warning(
            "env_cache.clean_verify_errored",
            symphony=symphony_name,
            error=str(exc),
        )
        return None, f"clean verify could not run: {exc}"
    out = out_b.decode(errors="replace")
    passed = proc.returncode == 0
    logger.info(
        "env_cache.clean_verify_result",
        symphony=symphony_name,
        passed=passed,
        returncode=proc.returncode,
        output_tail=out[-500:],
    )
    # Concise reason for the dashboard: the FAIL lines verify.sh emitted.
    fails = [ln.strip() for ln in out.splitlines() if "FAIL" in ln.upper()][:5]
    detail = "; ".join(fails) if fails else (out[-300:].strip() or "verify.sh non-zero")
    return passed, detail


def write_service_scripts(
    cache_dir: Path, service_models: list[Any], *, symphony: str = ""
) -> bool:
    """108 (P1): coordinare renders + writes the service start/stop/health scripts
    into ``<cache_dir>/services/`` DETERMINISTICALLY.

    Previously these were written performer-side by ``apply_manual_override`` during
    the bootstrap, which intermittently left ``<cache>/services/`` empty — so the
    spec-101 readiness gate had no ``services-start.sh`` to run and the declared
    services never started. Writing them coordinare-side here (alongside verify.sh /
    activate.sh, straight to the host cache dir) makes their presence deterministic.

    Best-effort: returns ``False`` (logged) on any render/IO error rather than
    raising — the readiness gate then reports the missing services instead of this
    aborting the manifest write. Returns ``False`` (no-op) when no services declared.
    """
    if not service_models:
        return False
    try:
        from coordinare_service_inference.schema import ServicesManifest
        from coordinare_service_inference.templater import render as render_service_scripts

        svc_manifest = ServicesManifest(
            services=service_models,
            cache_inputs=["score.json"],
            agent_version="coordinare-owned-108",
        )
        scripts = render_service_scripts(svc_manifest)
        svc_dir = cache_dir / "services"
        svc_dir.mkdir(parents=True, exist_ok=True)
        for name, body in (
            ("services-start.sh", scripts.start),
            ("services-stop.sh", scripts.stop),
            ("services-health.sh", scripts.health),
        ):
            path = svc_dir / name
            path.write_text(body)
            path.chmod(0o755)
        # 113: do NOT write services.json here. `load_prior_manifest` derives the
        # inference-cache suffix (part of readme_sha / last_seen_spec_sha) from
        # <cache>/services/services.json. The performer's apply_manual_override is the
        # canonical writer (agent_version="manual-override", cache_inputs from
        # score.json). If coordinare ALSO wrote it (different agent_version +
        # cache_inputs), the two alternated → the suffix oscillated → readme_sha !=
        # last_seen_spec_sha forever → the dispatch gate held every card and
        # re-bootstrapped in a loop. We only own the executable scripts here; the
        # manifest is the performer's to write so the suffix stays stable.
        logger.info(
            "env_cache.service_scripts_written",
            symphony=symphony,
            services=[s.name for s in service_models],
        )
        return True
    except Exception as exc:  # best-effort, never block the bootstrap
        logger.warning(
            "env_cache.service_scripts_write_failed",
            symphony=symphony,
            error=str(exc),
        )
        return False


class EnvCacheService:
    """Manages per-symphony env-cache bootstrapping and SHA change detection."""

    def __init__(self, coordinare_config: Any) -> None:
        self._coordinare_config = coordinare_config
        # 088: strong refs to in-flight exhaustion-notification tasks (prevents GC).
        self._notify_tasks: set[asyncio.Task[Any]] = set()
        # 088 (US5): symphonies whose cache has been verified (or freshly
        # bootstrapped) IN THIS PROCESS.  Per-process by design: an empty set
        # means a fresh coordinare boot, which is exactly when a persisted
        # last_bootstrap_succeeded=True must be re-verified (clean-room
        # verify.sh) instead of blindly trusted — trusting the snapshot alone
        # would re-open the phantom-success hole 23dd839 closed.
        self._restart_verified: set[str] = set()

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
        clean_verify_fn: Callable[[str], Coroutine[Any, Any, tuple[bool | None, str]]]
        | None = None,
    ) -> None:
        """Check for README SHA changes and dispatch bootstrap if needed.

        FR-008: returns immediately if symphony has no env_bootstrap configured.

        ``llm_chat`` (optional) is wired by the daemon to the coordinare's LLM and
        used for the best-effort README pass that augments the deterministic
        manifest with system packages described only in prose.

        ``clean_verify_fn`` (optional, 088 US5) runs the cache's verify.sh in a
        clean consumer-context container and returns ``(passed, detail)`` —
        ``None`` when un-runnable.  On the first cycle after a coordinare
        restart, a persisted successful bootstrap with an unchanged spec SHA is
        re-verified through it instead of re-bootstrapped (SC-004: ready in
        <2 min) or blindly trusted.
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
        # 088 (FR-009): the circuit breaker gates forced regens too — a broken
        # runtime that keeps failing its bootstrap must not bypass the budget.
        if (
            cache_state.runtime_health_failed
            and not cache_state.bootstrap_in_flight
            and not cache_state.bootstrap_exhausted
        ):
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

        # 088 (FR-010): a spec change resets the circuit-breaker budget — fixing
        # the README/Gemfile is the designed self-healing path. The budget is
        # keyed by readme_sha (the SHA the attempts accumulated under).
        if (
            (cache_state.bootstrap_attempts or cache_state.bootstrap_exhausted)
            and cache_state.readme_sha is not None
            and current_sha != cache_state.readme_sha
        ):
            logger.info(
                "env_cache.bootstrap_budget_reset",
                symphony=symphony_name,
                previous_attempts=cache_state.bootstrap_attempts,
                was_exhausted=cache_state.bootstrap_exhausted,
                new_sha=current_sha,
            )
            cache_state.bootstrap_attempts = 0
            cache_state.bootstrap_exhausted = False

        # 088 (FR-009): tripped breaker — no further dispatch for this spec SHA.
        # The (rate-limited implicit) exhaustion was announced once when the
        # breaker tripped; staying silent here avoids a log line per poll cycle.
        if cache_state.bootstrap_exhausted:
            return

        needs_bootstrap = cache_state.readme_sha is None or current_sha != cache_state.readme_sha

        # 077: break the failed-bootstrap deadlock.  readme_sha is set
        # optimistically at dispatch time (and persisted in the snapshot), so a
        # bootstrap that FAILED leaves readme_sha == current_sha while
        # last_bootstrap_succeeded is False.  Without this, needs_bootstrap stays
        # False forever, the consumer-gate holds indefinitely, and the only way
        # to recover is a manual trigger.  Retry on failure, but honour a
        # cooldown so we don't relaunch a container every poll cycle.
        # 088 (FR-009): the cooldown escalates with the attempt count
        # (base x 2^attempts) so consecutive failures back off instead of
        # hammering a fresh container every ~cooldown.
        if not needs_bootstrap and cache_state.last_bootstrap_succeeded is False:
            last_at = cache_state.last_bootstrap_at
            retry_cooldown = BOOTSTRAP_RETRY_COOLDOWN_S * (
                2 ** cache_state.bootstrap_attempts
            )
            cooldown_elapsed = (
                last_at is None
                or (datetime.now(UTC) - last_at).total_seconds() >= retry_cooldown
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

        # 088 (US5): restart honor path — a persisted successful bootstrap with
        # an unchanged spec SHA is re-VERIFIED (clean-room verify.sh) on the
        # first cycle of a fresh boot, not re-bootstrapped and not blindly
        # trusted.  Verify pass ⇒ ready; fail ⇒ full bootstrap immediately (no
        # cooldown — the failure is fresh evidence, not a flaky retry); verify
        # un-runnable (None) ⇒ trust the persisted success (degraded).
        if (
            not needs_bootstrap
            and cache_state.last_bootstrap_succeeded is True
            and clean_verify_fn is not None
            and symphony_name not in self._restart_verified
        ):
            self._restart_verified.add(symphony_name)
            passed, detail = await clean_verify_fn(symphony_name)
            if passed is True:
                cache_state.cache_dir_ready = True
                logger.info(
                    "env_cache.restart_verify_passed", symphony=symphony_name
                )
            elif passed is False:
                logger.warning(
                    "env_cache.restart_verify_failed",
                    symphony=symphony_name,
                    detail=detail,
                )
                cache_state.last_bootstrap_succeeded = False
                cache_state.cache_dir_ready = False
                cache_state.last_bootstrap_error = f"restart verify failed: {detail}"
                needs_bootstrap = True
            else:
                logger.info(
                    "env_cache.restart_verify_degraded",
                    symphony=symphony_name,
                    detail=detail,
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
        # 091: surface durably-declared stateful services so the bootstrap fetches
        # their binaries into the cache (deb-into-<cache>/debs/). Best-effort: a
        # missing/invalid .coordinare/score.json is normal — fall back to no services.
        # Fetched BEFORE the manifest artifacts so verify.sh can emit a live
        # readiness probe (093) for each coordinare-managed service.
        declared_services = await self._fetch_declared_services(
            github_org=eff_config.github_org,
            repo=repo,
            github_service=github_service,
            symphony_name=symphony_name,
        )

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
            declared_services=declared_services,
        )

        # 092: load the symphony's configured test-environment so the bootstrap
        # performer's start-phase dry-run (which runs INSIDE this performer) sees
        # the secret-like vars and clears the unset-secret gate (e.g. the website
        # symphony's POSTGRESQL_PASSWORD). A configured-but-missing file is a clear
        # coordinare-side error, not a silent empty dict — abort dispatch so the
        # operator fixes config instead of getting a confusing QA failure later.
        try:
            test_env_vars = await self._load_test_env_vars(
                symphony_name=symphony_name,
                symphony_config=symphony_config,
                github_org=eff_config.github_org,
                repo=repo,
                github_service=github_service,
                fallback_source=cache_state.test_env_source,
            )
        except TestEnvFileError as exc:
            # The error message carries only the resolved path + field, never values.
            logger.error(
                "env_cache.test_env_load_failed",
                symphony=symphony_name,
                error=str(exc),
            )
            return

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
            declared_services=declared_services,
            test_env_vars=test_env_vars,
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

    async def _load_test_env_vars(
        self,
        *,
        symphony_name: str,
        symphony_config: Any,
        github_org: str,
        repo: str,
        github_service: _GitHubService,
        fallback_source: str | None = None,
    ) -> dict[str, str]:
        """Load the symphony's configured test-environment vars (092).

        Thin wrapper over the module-level :func:`resolve_test_env_vars`, which the
        consumer dispatch path (``dispatch_performer``) shares so both contexts apply
        identical secret-handling. See that function for the full contract.
        """
        return await resolve_test_env_vars(
            symphony_name=symphony_name,
            test_env=getattr(symphony_config, "test_env", None),
            github_org=github_org,
            repo=repo,
            github_service=github_service,
            fallback_source=fallback_source,
        )

    async def _fetch_declared_services(
        self,
        *,
        github_org: str,
        repo: str,
        github_service: _GitHubService,
        symphony_name: str,
    ) -> list[dict[str, Any]]:
        """Best-effort fetch of durably-declared services from .coordinare/score.json (091).

        Returns each declared :class:`ServiceEntry` as a plain dict (model dump) so it
        rides BootstrapJobPayload → card_context into the persona builder, which derives
        the service-binary install items. Any failure (no file, parse/validation error)
        returns ``[]`` — the bootstrap simply derives no service-install item, exactly as
        before 091.
        """
        try:
            content = await github_service.get_file_content(
                github_org, repo, ".coordinare/score.json"
            )
        except Exception:
            return []
        if not content:
            return []
        try:
            from coordinare_service_inference.schema import ServicesManifest

            manifest = ServicesManifest.model_validate_json(content)
        except Exception as exc:
            logger.info(
                "env_cache.declared_services_unparseable",
                symphony=symphony_name,
                error=str(exc),
            )
            return []
        services = [svc.model_dump() for svc in manifest.services]
        if services:
            logger.info(
                "env_cache.declared_services_found",
                symphony=symphony_name,
                count=len(services),
                kinds=sorted({svc.get("kind", "generic") for svc in services}),
            )
        return services

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
        declared_services: list[dict[str, Any]] | None = None,
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
        # 093: re-validate the declared-service dicts back into ServiceEntry models so
        # render_verify_sh can emit a live RUNNING/healthy readiness probe (pg_isready /
        # redis PING) per coordinare-managed service. Best-effort: a dict that no longer
        # validates is simply skipped — verify.sh still covers the toolchain checklist.
        service_models: list[Any] = []
        if declared_services:
            try:
                from coordinare_service_inference.schema import ServiceEntry

                for svc in declared_services:
                    try:
                        service_models.append(ServiceEntry.model_validate(svc))
                    except Exception:
                        continue
            except Exception:
                service_models = []

        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
            verify_path = cache_dir / "verify.sh"
            verify_path.write_text(
                render_verify_sh(
                    manifest,
                    cache_mount_path=cache_mount_path,
                    services=service_models,
                )
            )
            verify_path.chmod(0o755)
            activate_path = cache_dir / "activate.sh"
            activate_path.write_text(
                render_activate_sh(manifest, cache_mount_path=cache_mount_path)
            )
            activate_path.chmod(0o755)
            (cache_dir / "manifest.json").write_text(manifest.model_dump_json(indent=2))
            # 108 (P1): coordinare writes the service start/stop/health scripts into
            # <cache>/services/ DETERMINISTICALLY here (like verify.sh/activate.sh),
            # rather than relying on the performer-side manual_override write that
            # intermittently left <cache>/services/ empty so the readiness gate had
            # no services-start.sh to run (postgres never started).
            write_service_scripts(cache_dir, service_models, symphony=symphony_name)
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
        test_env_source: str | None = None,
    ) -> None:
        """063 T026d: stamp the latest service-inference summary onto state.

        Called by the coordinare daemon when a bootstrap job reports terminal
        status, before ``on_bootstrap_complete``. The dashboard reads these
        fields to show operators what the agent produced (or why it didn't
        run) without having to inspect the env-cache directory.

        092 US2: ``test_env_source`` is the agent-discovered test-env source
        PATH (never literal values). Persisting it lets later QA-runtime and
        performer contexts reload the same file when no ``test_env`` block is
        configured.
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
        if test_env_source is not None:
            cache_state.test_env_source = test_env_source
        logger.info(
            "env_cache.inference_recorded",
            symphony=symphony_name,
            skipped_reason=skipped_reason,
            agent_version=agent_version,
            attempts=attempts,
            succeeded=succeeded,
            services=services,
            test_env_source=test_env_source,
        )

    def _notify_bootstrap_exhausted(
        self,
        symphony_name: str,
        cache_state: EnvCacheState,
        state: dict[str, Any],
    ) -> None:
        """088 (FR-009): fire exactly one circuit_breaker_trip notification when
        the bootstrap budget is exhausted.

        Called only on the False→True ``bootstrap_exhausted`` transition (the
        caller guards it), so 'exactly once' per exhaustion event holds; the
        flag is persisted, so a restart does not re-notify. Dispatch is
        scheduled as a task because this runs inside the daemon's event loop
        from a sync completion handler."""
        notification_service = state.get("notification_service")
        if notification_service is None:
            return
        from coordinare.models.notification import (
            EventType,
            NotificationEvent,
            NotificationSeverity,
        )

        event = NotificationEvent(
            event_type=EventType.circuit_breaker_trip,
            severity=NotificationSeverity.warning,
            payload={
                "summary": (
                    f"🔌 Env bootstrap for '{symphony_name}' exhausted its "
                    f"{cache_state.bootstrap_attempts}-attempt budget — no further "
                    f"bootstraps until the env spec changes."
                ),
                "symphony": symphony_name,
                "attempts": str(cache_state.bootstrap_attempts),
                "last_error": str(cache_state.last_bootstrap_error or ""),
            },
            source="env_cache",
            dedup_key=f"bootstrap_exhausted:{symphony_name}:{cache_state.readme_sha}",
        )
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            logger.warning(
                "env_cache.bootstrap_exhausted_notify_skipped",
                symphony=symphony_name,
                detail="no running event loop to dispatch the notification",
            )
            return
        task = loop.create_task(notification_service.dispatch(event))
        self._notify_tasks.add(task)
        task.add_done_callback(self._notify_tasks.discard)

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

        # 092: a bootstrap that reports success but leaves the mount holding only
        # the three coordinare-seeded files installed the toolchain in the wrong
        # place (the agent treated the absolute cache_mount_path as cwd-relative
        # and built under <cwd>/devenv/<name> instead). The real mount is empty,
        # so consumers would get no toolchain. Downgrade to failure LOUDLY here
        # rather than letting cache_dir_ready flip True on an empty cache; the
        # failure flows through the normal attempt-counter/exhaust path below.
        if success and not _cache_dir_is_populated(cache_state.cache_dir):
            success = False
            error = (
                "env-cache mount contains only coordinare-seeded files "
                "(activate.sh, manifest.json, verify.sh) after a reported-"
                "successful bootstrap — the toolchain was installed in the "
                f"wrong location; the absolute mount {cache_state.cache_dir} is "
                "empty. The bootstrap agent likely treated the absolute "
                "cache_mount_path as a cwd-relative path. Cache rejected."
            )
            logger.error(
                "env_cache.bootstrap_mount_empty",
                symphony=symphony_name,
                cache_dir=str(cache_state.cache_dir),
            )

        cache_state.last_bootstrap_succeeded = success
        # 077: surface WHY a bootstrap failed (dashboard); clear on success.
        cache_state.last_bootstrap_error = None if success else (error or "bootstrap failed")

        # 061: Mark the cache usable only after a successful bootstrap.
        # cache_dir_ready stays False until at least one bootstrap succeeds,
        # which prevents performers from mounting an unpopulated cache dir.
        if success:
            cache_state.cache_dir_ready = True
            # 088 (FR-009): success re-arms the circuit breaker.
            cache_state.bootstrap_attempts = 0
            cache_state.bootstrap_exhausted = False
            # 088 (US5): an in-process success was already clean-verified by the
            # completion path — the restart honor verify must not re-fire.
            self._restart_verified.add(symphony_name)

        # 088 (FR-009): on failure, KEEP the recorded SHA. Clearing it (the
        # pre-088 behaviour) made the next cycle recompute needs_bootstrap=True
        # and redispatch immediately — the observed unbounded ~100s hammering.
        # With the SHA retained, retries flow through check_and_trigger's
        # escalating-cooldown branch (readme_sha == current ∧ succeeded=False),
        # and the consumer gate still holds (last_bootstrap_succeeded=False).
        if not success:
            cache_state.bootstrap_attempts += 1
            max_attempts = int(
                getattr(
                    getattr(self._coordinare_config, "global_config", None),
                    "env_bootstrap_max_attempts",
                    3,
                )
                or 3
            )
            if (
                cache_state.bootstrap_attempts >= max_attempts
                and not cache_state.bootstrap_exhausted
            ):
                cache_state.bootstrap_exhausted = True
                logger.warning(
                    "env_cache.bootstrap_exhausted",
                    symphony=symphony_name,
                    attempts=cache_state.bootstrap_attempts,
                    max_attempts=max_attempts,
                    last_error=cache_state.last_bootstrap_error,
                )
                self._notify_bootstrap_exhausted(symphony_name, cache_state, state)

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

        # 088 (US5): flush the completion to disk NOW — it moves no lifecycle
        # signature, so the daemon's signature-gated save would defer it to the
        # next stage transition and a restart in that window would rewind it.
        save_fn = state.get("snapshot_save_fn")
        if callable(save_fn):
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                logger.warning(
                    "env_cache.snapshot_flush_skipped", symphony=symphony_name
                )
            else:
                task = loop.create_task(save_fn())
                self._notify_tasks.add(task)
                task.add_done_callback(self._notify_tasks.discard)
