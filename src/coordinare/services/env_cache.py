"""Performer environment caching service (spec 060)."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import re
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

import structlog
from coordinare_service_inference.cache_key import (
    compute_inference_cache_key,
    forced_regen_cache_key,
    parse_prior_manifest,
)
from coordinare_service_inference.manual_override import SERVICES_SUBDIR

from coordinare.lib.redaction import redact_state_value
from coordinare.models.env_cache import BootstrapJobPayload, EnvCacheState
from coordinare.models.notification import EventType, NotificationEvent, NotificationSeverity
from coordinare.models.performer_endpoint import VolumeMount
from coordinare.services.bootstrap_history import (
    ACT_BUDGET_RESET,
    ACT_EXHAUSTED,
    BOOTSTRAP_CANCEL_COOLDOWN_S,
    OUTCOME_CANCELLED,
    AttemptEnd,
    BudgetNow,
    history_of,
    record_activity,
    report_completion,
    short_commit,
)
from coordinare.services.env_provenance import (
    DEFAULT_LIMITS,
    PROVENANCE_DIR_NAME,
    PROVENANCE_FULL_RECHECK_S,
    ContentResult,
    ManifestFile,
    ProvenanceCheck,
    ProvenanceLimits,
    ProvenanceRefusedError,
    ProvenanceScope,
    ProvenanceTransientError,
    build_payload,
    content_pass,
    freeze_cache,
    manifest_cap_bytes,
    open_manifest,
    provenance_slug,
    read_stat_sidecar,
    stat_digest_of,
    write_stat_sidecar,
)
from coordinare.services.environment_acceptance import (
    accepted_environment,
    execution_report,
    immutable_verifier_image,
    saved_execution_accepted,
)
from coordinare.services.environment_repair import (
    begin_attempt,
    dispatch_refusal,
    finish_attempt,
    journal_for,
    prepare_repair,
    repair_context,
    save_attempt,
)
from coordinare.services.kubernetes_provenance import parse_summary, validate_summary
from coordinare.services.kubernetes_verifier import (
    VerifierConfigurationError,
    VerifierSpec,
    configured_verifier_image,
)
from coordinare.services.mounted_file import (
    MountedFileRefusedError,
    anchored_mkdir,
    loaded_performer_mounts,
    mounted_dir_refusal,
    read_mounted_dir_names,
    read_mounted_file,
    replace_mounted_file,
)
from coordinare.services.test_env_loader import (
    TestEnvFileError,
    load_test_env,
    parse_test_env,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Coroutine, Mapping, Sequence
    from typing import Protocol

    from coordinare_service_inference.schema import ServicesManifest

    from coordinare.services.environment_discovery import ChatJson

    class _GitHubFileReader(Protocol):
        async def get_file_content(self, org: str, repo: str, path: str, *, ref: str = "HEAD") -> str | None: ...

    class _GitHubService(_GitHubFileReader, Protocol):
        async def get_file_blob_sha(self, org: str, repo: str, path: str) -> str | None: ...
        async def get_branch_head_sha(self, branch_name: str = "main") -> str | None: ...
        async def get_repository_paths(
            self, owner: str, repo: str, *, ref: str, max_entries: int = 100_000,
        ) -> list[str]: ...

logger = structlog.get_logger(__name__)


def _remember_manifest(cache: EnvCacheState | None, items: list[dict[str, Any]]) -> None:
    """Keep dispatch inputs even when writing the generated scripts is refused."""
    if cache is not None:
        cache.manifest_items = items

DEFAULT_DEVENV_ROOT = "/devenv"

# 077: when a bootstrap FAILED for the current spec SHA, auto-retry it on a
# later cycle rather than waiting for the README to change.  The cooldown keeps
# a failing bootstrap from spinning up a fresh container on every poll cycle.
BOOTSTRAP_RETRY_COOLDOWN_S = 120.0


# 208: the prior manifest lives in a cache dir the performer can write during bootstrap.
_PRIOR_MANIFEST_MAX_BYTES = 1024 * 1024

# 219: the scripts coordinare renders into the cache (verify.sh, activate.sh, services-*.sh).
_EXECUTABLE_MODE = 0o755

# 218: bound on a cache script read back for the byte comparison in the activate.sh gate.
_RENDERED_SCRIPT_MAX_BYTES = 256 * 1024

# 218 (review round 3): the clean verify's wall-clock budget. A run past it is a real failure, and its
# container is removed by name.
_VERIFY_TIMEOUT_S = 300.0

# 218 (review round 3): docker run exit code when the daemon or the image could not start the container.
_DOCKER_RUN_START_FAILED = 125

# 218 (round 4): the daemon's own docker probe. A run is attempted only when this answers.
_DOCKER_PROBE_TIMEOUT_S = 15.0

# 218 (review round 3): infrastructure failures before the cache is exhausted (operator-visible).
_INFRA_FAILURE_CAP = 5


def load_prior_manifest_bounded(
    cache_dir: Path, *, performer_mounts: Sequence[Path],
) -> ServicesManifest | None:
    """The sealed services.json from *cache_dir*, read without links and bounded (208).

    Returns None when it is missing, a link, not a regular file, larger than the bound,
    not UTF-8, or not a valid manifest. The read is anchored at the outermost performer mount
    over the cache dir (219), so a link planted at the root, the cache dir or below is refused.
    Callers run it off the event loop.
    """
    data = read_mounted_file(
        cache_dir / SERVICES_SUBDIR / "services.json",
        max_bytes=_PRIOR_MANIFEST_MAX_BYTES,
        mounts=_env_cache_mounts(cache_dir, performer_mounts),
    )
    if data is None:
        return None
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return parse_prior_manifest(text)


async def _bootstrap_file_content(
    github: _GitHubFileReader, org: str, repo: str, path: str, commit: str | None,
) -> str | None:
    if commit is None:
        return await github.get_file_content(org, repo, path)
    return await github.get_file_content(org, repo, path, ref=commit)


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
        if cache_state.infra_failures and not cache_state.bootstrap_attempts:
            return (
                f"Env bootstrap stopped after {cache_state.infra_failures} infrastructure failure(s): "
                f"docker or the bootstrap container could not confirm the verification. "
                f"Last failure: {last_error}. Fix the docker daemon and the performer image, then "
                "change the spec (a SHA change re-arms it)."
            )
        return (
            f"Env bootstrap budget exhausted after {cache_state.bootstrap_attempts} "
            f"failed run(s); no further bootstraps will be dispatched for this "
            f"spec. Last failure: {last_error}. Fix the env spec files (a SHA change "
            f"resets the budget) or repair the cache and restart."
        )
    return (
        "Holding dispatch until env_bootstrap has SUCCESSFULLY completed for "
        "the current spec. Card retries on the next pickup cycle."
    )


def _env_cache_mounts(cache_dir: Path, performer_mounts: Sequence[Path]) -> tuple[Path, ...]:
    """Every performer-visible host directory, plus the env-cache root (219, 219 review).

    The walk trusts the outermost mount it reaches and refuses links below it. Passing the
    full performer mount list (log dir, every endpoint volume, the root itself) means a root
    that sits under a performer volume is walked from that volume, so a link the performer
    plants at the root is refused, not followed to a host directory.
    """
    return (*performer_mounts, cache_dir.parent)


def _cache_dir_refusal(cache_dir: Path, *, performer_mounts: Sequence[Path]) -> str | None:
    """Why the cache dir may not be mounted or read (219), or None. A missing dir is allowed."""
    reason = mounted_dir_refusal(
        cache_dir, mounts=_env_cache_mounts(cache_dir, performer_mounts),
    )
    return None if reason == "missing" else reason


def _cache_dir_has_activate(
    cache_dir: Path, *, performer_mounts: Sequence[Path], expected_activate: str | None,
) -> bool:
    """Return True iff the cache's activate.sh is the text the daemon rendered (218).

    Presence is not trust. The bootstrap performer writes the cache, so a rewritten or
    emptied activate.sh must not gate a mount or a dispatch. The cache copy has to equal
    ``expected_activate`` (``EnvCacheState.rendered_activate_sh``) byte for byte. No rendered
    text on record (a legacy snapshot, a refused write) is False. The read is anchored (219):
    a link, hard link or FIFO is refused, never followed.
    """
    if expected_activate is None:
        return False
    data = read_mounted_file(
        cache_dir / "activate.sh",
        max_bytes=_RENDERED_SCRIPT_MAX_BYTES,
        mounts=_env_cache_mounts(cache_dir, performer_mounts),
    )
    return data is not None and data == expected_activate.encode()


# 092: the three files coordinare itself seeds into every cache_dir before a
# bootstrap runs (render_activate_sh / render_verify_sh / the manifest dump).
# A successful bootstrap must leave the toolchain *beside* these — if the mount
# contains only the seed set after a "successful" run, the agent installed the
# toolchain somewhere other than the absolute cache_mount_path it was given
# (the observed glm-4.7-flash failure: a relative-path build under
# <cwd>/devenv/<name> while the real mount stayed empty). See the absolute-path
# block in http_performer_service._build_env_bootstrap_payload.
_CACHE_SEED_FILES = frozenset({"activate.sh", "manifest.json", "verify.sh"})

# 218: entries coordinare writes that are not toolchain content. services/ holds the
# service scripts and services.json, never an installed toolchain.
_CACHE_NON_TOOLCHAIN_ENTRIES = _CACHE_SEED_FILES | {SERVICES_SUBDIR}


def _cache_dir_is_populated(cache_dir: Path, *, performer_mounts: Sequence[Path]) -> bool:
    """Return True iff cache_dir holds a toolchain directory beyond the seed set (218).

    Used to fail a "successful" bootstrap loudly when the agent built the toolchain in the
    wrong location and the real mount holds only coordinare's files. A name alone proves
    nothing (a dummy file passes it), so a toolchain entry must be a real directory, not a
    link, file or FIFO, and must hold at least one entry. The daemon cannot predict installed
    toolchain content, so this is only a structural floor. The daemon's verify.sh run is the
    arbiter of whether the toolchain works. A refused or missing directory is False.
    """
    mounts = _env_cache_mounts(cache_dir, performer_mounts)
    names = read_mounted_dir_names(cache_dir, mounts=mounts)
    if names is None:
        return False
    for name in names:
        if name in _CACHE_NON_TOOLCHAIN_ENTRIES:
            continue
        entry = cache_dir / name
        if mounted_dir_refusal(entry, mounts=mounts) is not None:
            continue
        if read_mounted_dir_names(entry, mounts=mounts):
            return True
    return False


def _mount_for(
    symphony_name: str,
    state: EnvCacheState,
    *,
    performer_mounts: Sequence[Path],
    container_devenv_root: str,
) -> tuple[Path, str] | None:
    """(host cache dir, container path) for a cache that passed its gate, or None when it is refused.

    219: the host path is bind-mounted, and docker follows a link in it, so a linked cache dir is never mounted.
    The caller builds the VolumeMount (rw for a bootstrap, ro for a consumer).
    """
    refusal = _cache_dir_refusal(state.cache_dir, performer_mounts=performer_mounts)
    if refusal is not None:
        logger.error(
            "env_cache.mount_refused",
            symphony=symphony_name,
            cache_dir=str(state.cache_dir),
            reason=refusal,
        )
        return None
    return state.cache_dir, f"{container_devenv_root}/{state.sanitised_name}"


async def _resolve_bootstrap_commit(github: _GitHubService, config: Any, cache: EnvCacheState) -> bool:
    cache.bootstrap_commit_sha = None
    commit = await github.get_branch_head_sha(str(getattr(config, "base_branch", None) or "main"))
    if not isinstance(commit, str) or re.fullmatch(r"[0-9a-f]{40,64}", commit) is None:
        message = "could not resolve the bootstrap repository commit"
        raise ProvenanceTransientError(message)
    cache.bootstrap_commit_sha = commit
    refusal = dispatch_refusal(cache, commit)
    if refusal is not None:
        cache.last_bootstrap_error = refusal
        cache.cache_dir_ready = False
        return False
    return True


def _clear_reviewed_environment(cache: EnvCacheState) -> None:
    """Drop every readiness artifact before producing a replacement contract."""
    cache.rendered_verify_sh = None
    cache.rendered_activate_sh = None
    cache.environment_capabilities = None
    cache.environment_manifest = None
    cache.environment_assessment = None
    cache.environment_verification = None
    cache.cache_dir_ready = False
    cache.last_bootstrap_succeeded = None


def kubernetes_cache_verified(ec: EnvCacheState) -> bool:
    """Persisted verifier success plus a complete frozen claim summary gates Kubernetes readiness."""
    return (saved_execution_accepted(ec) and ec.last_bootstrap_succeeded is True and ec.cache_dir_ready
            and ec.provenance_digest is not None and ec.provenance_stat_digest is not None
            and ec.provenance_inputs is not None)


def get_kubernetes_consumer_volume(
    symphony_name: str,
    env_cache_states: dict[str, Any],
    *,
    performer_mounts: Sequence[Path],
    container_devenv_root: str = DEFAULT_DEVENV_ROOT,
) -> tuple[VolumeMount, str] | None:
    """265: the consumer mount on Kubernetes, where the daemon cannot see the cache.

    The daemon's own activate.sh is on its state volume and says nothing about the claim the consumer mounts. Readiness
    here requires the verifier's successful bootstrap and the frozen claim digest and consumer inputs. A wiped claim is not
    caught here. The performer refuses the mount before any work and asks for a re-bootstrap (agent main.py
    _consumer_mount_gap).
    """

    state = env_cache_states.get(symphony_name)
    if not isinstance(state, EnvCacheState):
        return None
    if not kubernetes_cache_verified(state):
        return None
    mount = _mount_for(
        symphony_name, state, performer_mounts=performer_mounts, container_devenv_root=container_devenv_root,
    )
    if mount is None:
        return None
    cache_dir, container_path = mount
    return VolumeMount(
        host_path=cache_dir,
        container_path=PurePosixPath(container_path),
        mode="ro",
    ), container_path


def get_env_volume_for_symphony(
    symphony_name: str,
    env_cache_states: dict[str, Any],
    *,
    is_bootstrap: bool,
    performer_mounts: Sequence[Path],
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

    state = env_cache_states.get(symphony_name)
    if state is None or not isinstance(state, EnvCacheState):
        return None
    if not is_bootstrap and (not saved_execution_accepted(state) or not state.cache_dir_ready
                             or state.last_bootstrap_succeeded is not True):
        return None
    if not is_bootstrap and not _cache_dir_has_activate(
        state.cache_dir,
        performer_mounts=performer_mounts,
        expected_activate=state.rendered_activate_sh,
    ):
        return None
    mount = _mount_for(
        symphony_name, state, performer_mounts=performer_mounts, container_devenv_root=container_devenv_root,
    )
    if mount is None:
        return None
    cache_dir, container_path = mount
    return VolumeMount(
        host_path=cache_dir,
        container_path=PurePosixPath(container_path),
        mode="rw" if is_bootstrap else "ro",
    ), container_path


def _collect_env_volumes_for_persistent_performer(
    env_cache_states: dict[str, Any],
    container_devenv_root: str = DEFAULT_DEVENV_ROOT,
    *,
    performer_mounts: Sequence[Path],
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

    mounts: list[VolumeMount] = []
    for state in env_cache_states.values():
        if (not isinstance(state, EnvCacheState) or not saved_execution_accepted(state)
                or not state.cache_dir_ready or state.last_bootstrap_succeeded is not True):
            continue
        if not _cache_dir_has_activate(
            state.cache_dir,
            performer_mounts=performer_mounts,
            expected_activate=state.rendered_activate_sh,
        ):
            continue
        container_path = f"{container_devenv_root}/{state.sanitised_name}"
        mounts.append(
            VolumeMount(
                host_path=state.cache_dir,
                container_path=PurePosixPath(container_path),
                mode="ro",
            ),
        )
    return mounts


async def resolve_test_env_vars(
    *,
    symphony_name: str,
    test_env: Any,
    github_org: str,
    repo: str,
    github_service: _GitHubFileReader,
    fallback_source: str | None = None,
    performer_mounts: Sequence[Path],
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
        loaded = load_test_env(
            test_env, repo_root=Path("/"), performer_mounts=performer_mounts,
        )
        source = f"host_path:{test_env.host_path}"
    else:
        content = await github_service.get_file_content(
            github_org, repo, test_env.repo_path,
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


async def _remove_verify_container(name: str) -> bool:
    """218: remove the verify container by its name. True when it is confirmed gone.

    A container that is already removed is confirmed: nothing is left running. Any other failure is
    not confirmed."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "rm", "-f", "--", name,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _out, err = await asyncio.wait_for(proc.communicate(), timeout=30.0)
    except (OSError, TimeoutError):
        return False
    if proc.returncode == 0:
        return True
    return "No such container" in err.decode(errors="replace")


async def _kill_and_remove(proc: asyncio.subprocess.Process, name: str) -> bool:
    """218: kill the docker client, then remove its container by name. True when the removal is confirmed."""
    with contextlib.suppress(ProcessLookupError):
        proc.kill()
    return await _remove_verify_container(name)


async def _docker_daemon_reachable() -> bool:
    """218 (round 4): True when the docker daemon answers. An unreachable daemon is infrastructure.

    A performer cannot change this probe, and it does not parse error text: the CLI exits 1 for an
    unreachable daemon, which is the same code as a failed verdict."""
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", "version", "--format", "{{.Server.Version}}",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.wait_for(proc.wait(), timeout=_DOCKER_PROBE_TIMEOUT_S)
    except (OSError, TimeoutError):
        return False
    return proc.returncode == 0


def _verify_cache_refusal(
    symphony_name: str, ec: EnvCacheState, performer_mounts: Sequence[Path],
) -> str | None:
    """218: why the verify must not run against this cache, or None. Logs each refusal."""
    cache_dir = Path(ec.cache_dir)
    cache_refusal = _cache_dir_refusal(cache_dir, performer_mounts=performer_mounts)
    if cache_refusal is not None:
        logger.error(
            "env_cache.clean_verify_refused",
            symphony=symphony_name,
            cache_dir=str(cache_dir),
            reason=cache_refusal,
        )
        return f"env-cache dir refused: {cache_refusal}"
    if not _cache_dir_has_activate(
        cache_dir,
        performer_mounts=performer_mounts,
        expected_activate=ec.rendered_activate_sh,
    ):
        logger.error(
            "env_cache.activate_mismatch",
            symphony=symphony_name,
            cache_dir=str(cache_dir),
        )
        return "activate.sh in the cache does not match the coordinare-rendered content"
    return None


def daemon_sees_performer_cache(state: Mapping[str, Any]) -> bool:
    """265: False on a transport whose performers run in a cluster the daemon cannot mount the cache from.

    On Kubernetes the bootstrap pod mounts its own RWX claim (coordinare-devenv-cache). The daemon's cache_dir is on
    its state volume and holds no toolchain, so a clean verify run from the daemon would check the wrong directory.
    """
    cfg = state.get("coordinare_config")
    return getattr(cfg, "agent_transport", "subprocess") != "kubernetes"


async def verify_env_cache_clean(
    state: Mapping[str, Any],
    symphony_name: str,
    svc: Any,
    *,
    performer_mounts: Sequence[Path],
) -> tuple[bool | None, str]:
    """Use persisted verifier-backed readiness on Kubernetes and the local clean verify on Docker."""
    if not daemon_sees_performer_cache(state):
        ec = (state.get("env_cache") or {}).get(symphony_name)
        ready = isinstance(ec, EnvCacheState) and kubernetes_cache_verified(ec)
        if ready and isinstance(ec, EnvCacheState):
            from coordinare.services.environment_capabilities import (
                capabilities_match,
                resolve_execution_capabilities,
            )

            try:
                capabilities = await resolve_execution_capabilities(state, symphony_name)
                ready = (capabilities_match(ec, capabilities)
                         and (ec.environment_verification or {}).get("image_ref") == capabilities.image_ref
                         and (ec.environment_verification or {}).get("platform") == capabilities.platform)
            except (ValueError, OSError, RuntimeError):
                ready = False
        return ready, "kubernetes readiness requires current capabilities, verifier success and frozen provenance"
    return await _verify_cache_clean(state, symphony_name, svc, performer_mounts=performer_mounts)


async def _verify_cache_clean(
    state: Mapping[str, Any],
    symphony_name: str,
    svc: Any,
    *,
    performer_mounts: Sequence[Path],
) -> tuple[bool | None, str]:
    """077/093: run the cache's ``verify.sh`` in a CLEAN consumer-context
    container — the performer image with ONLY the cache mounted read-only at
    the same path consumers use — so a broken consumer-facing ``activate.sh``
    can't false-pass via installs the bootstrap container did online.

    verify.sh sources activate.sh and asserts every dependency is runnable
    (it installs from the cache's local debs, no network), so this is a
    faithful "does the cache alone provide a working toolchain" check.

    What the verify proves: the toolchain the bootstrap installed runs and reports the expected version.
    Checking a version executes the installed binary, so the verify cannot tell a genuine toolchain from one
    the bootstrap planted to print the right version. Binary provenance (a digest or a snapshot) is tracked in
    #234. A PASSED verify is not a security verdict on the toolchain.

    Returns ``(passed, detail)``. 218: the script run is the one the daemon rendered
    (``EnvCacheState.rendered_verify_sh``), piped on stdin. ``passed`` is False when that
    script exits non-zero, when no script is on record, when the cache dir is refused, or
    when the cache's activate.sh is not the rendered text. None is returned only when docker
    could not run the script at all (degraded, does not downgrade success). ``detail`` is a
    concise human-readable reason (the verify FAIL lines) for the dashboard.

    Pure over existing seams (``state``, ``symphony_name``, ``svc``) so both the
    daemon method and the dispatch-time readiness gate (093) share one source of
    truth with no drift. Emits observability with NAMES/PATHS only.
    """

    ec = (state.get("env_cache") or {}).get(symphony_name)
    if not isinstance(ec, EnvCacheState):
        return None, "no env-cache state for symphony"
    ec.environment_verification = None
    # 218: the script that decides success is the one the DAEMON rendered, kept in daemon
    # memory and the snapshot. The copy in the cache is never executed. Every refusal here is
    # False, not None: None is degraded and would let a performer-caused refusal pass.
    script = ec.rendered_verify_sh
    refusal = None
    contract = accepted_environment(ec)
    if contract is None:
        refusal = "no independently accepted environment contract; rebuild the cache"
    elif script is None:
        refusal = "no daemon-rendered verify.sh on record for this cache"
    else:
        refusal = _verify_cache_refusal(symphony_name, ec, performer_mounts)
    if refusal is not None:
        logger.error("env_cache.clean_verify_refused", symphony=symphony_name, reason=refusal)
        return False, refusal
    script = str(script)
    # 218 (round 4): an unreachable daemon is infrastructure. The CLI exits 1 for it, the same code as a
    # failed verdict, so the probe answers first. A planted toolchain that exits 1 or 125 is still a verdict.
    if not await _docker_daemon_reachable():
        logger.error(
            "env_cache.clean_verify_docker_unreachable",
            symphony=symphony_name,
            detail="the docker daemon did not answer; the clean verify was not run",
        )
        return None, "the docker daemon is not reachable; the clean verify was not run"
    return await _verify_cache_runtime(state, symphony_name, svc, performer_mounts)


async def _verify_cache_runtime(state: Mapping[str, Any], symphony_name: str, svc: Any,
                                performer_mounts: Sequence[Path]) -> tuple[bool | None, str]:
    ec = state["env_cache"][symphony_name]
    contract = accepted_environment(ec)
    script = str(ec.rendered_verify_sh)
    from coordinare.services.http_performer_service import HTTPPerformerService

    cache_dir = Path(ec.cache_dir)
    from coordinare.services.environment_capabilities import (
        capabilities_match,
        resolve_execution_capabilities,
    )

    try:
        capabilities = await resolve_execution_capabilities(state, symphony_name)
    except (ValueError, OSError, RuntimeError):
        return False, "clean verification runtime capabilities are unavailable"
    if not capabilities_match(ec, capabilities):
        return False, "clean verification runtime capabilities changed; rebuild the cache"
    devenv_root = (
        svc.devenv_root if isinstance(svc, HTTPPerformerService) else DEFAULT_DEVENV_ROOT
    )
    image = capabilities.image_ref
    # Mount at the SAME container path the bootstrap used, since activate.sh /
    # verify.sh hardcode that absolute DEVEENV path.
    container_path = f"{devenv_root}/{ec.sanitised_name}"
    # 218: the daemon's script is piped on stdin (bash -s), so the cache copy of verify.sh is
    # never run. The cache is mounted read-only, as a consumer sees it.
    # 218 (review): the image's BASH_ENV is the devenv profile. Bash sources it before `bash -s` reads
    # stdin, and it unpacks the cache's debs, symlinks /usr/bin entries and sources the cache's
    # activate.sh. Empty BASH_ENV and ENV stop that for bash and sh. _DEVENV_SOURCED makes the profile
    # a no-op even if a variable is re-exported, and _DEVENV_SKIP_SERVICES stops any service start.
    name = f"coordinare-verify-{ec.sanitised_name}-{uuid.uuid4().hex[:8]}"
    cmd = [
        "docker", "run", "--rm", "-i", "--name", name, "--network", "none",
        "-e", "BASH_ENV=",
        "-e", "ENV=",
        "-e", "_DEVENV_SOURCED=1",
        "-e", "_DEVENV_SKIP_SERVICES=1",
        "-v", f"{cache_dir}:{container_path}:ro",
        "--entrypoint", "bash", str(image),
        "-s",
    ]
    from coordinare.services.environment_verification import (
        source_setup,
        verification_environment,
        verification_sources,
    )

    try:
        immutable_verifier_image(image)
        files = await verification_sources(state, symphony_name, ec)
        credentials = await verification_environment(state, symphony_name, ec, performer_mounts)
        script = source_setup(files, expected_inputs=contract.inputs if contract is not None else {}) + credentials + script
    except (ValueError, OSError, RuntimeError) as exc:
        return False, f"clean verification inputs refused: {exc}"
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except OSError as exc:
        logger.warning(
            "env_cache.clean_verify_errored",
            symphony=symphony_name,
            error=str(exc),
        )
        return None, f"clean verify could not run: {exc}"
    outcome = await _verify_outcome(proc, name, symphony_name, script, cache=ec)
    if outcome[0] is not None:
        if not await _remove_verify_container(name):
            outcome = None, "clean verifier container removal was not confirmed"
        elif ec.environment_verification is not None:
            if ec.environment_verification.get("platform") != capabilities.platform:
                ec.environment_verification = None
                return False, "clean verification platform differs from accepted capabilities"
            ec.environment_verification["image_ref"] = image
    return outcome


async def _verify_outcome(
    proc: asyncio.subprocess.Process, name: str, symphony_name: str, script: str,
    *, cache: EnvCacheState | None = None,
) -> tuple[bool | None, str]:
    """218 (review round 3): the verdict for a verify container that started.

    A run past the budget is a real failure, and its container is removed by name. An exit 125 means
    docker could not start the container, which is infrastructure. Any other exit is the verify's verdict.
    """
    try:
        out_b, _ = await asyncio.wait_for(
            proc.communicate(input=script.encode()), timeout=_VERIFY_TIMEOUT_S,
        )
    except asyncio.CancelledError:
        # 218 (round 4): the daemon task was cancelled. The container must still be killed and removed.
        # The cleanup is shielded, so a second cancel cannot skip it. The cancellation then propagates.
        await asyncio.shield(_kill_and_remove(proc, name))
        raise
    except TimeoutError:
        removed = await _kill_and_remove(proc, name)
        logger.error(
            "env_cache.clean_verify_timed_out",
            symphony=symphony_name,
            container=name,
            removed=removed,
        )
        state_note = "removed" if removed else "NOT confirmed removed"
        return False, f"clean verify timed out after {_VERIFY_TIMEOUT_S:.0f}s; its container was {state_note}"
    if proc.returncode == _DOCKER_RUN_START_FAILED:
        logger.warning(
            "env_cache.clean_verify_errored",
            symphony=symphony_name,
            error=f"docker run exited {_DOCKER_RUN_START_FAILED}",
        )
        tail = out_b.decode(errors="replace")[-300:].strip()
        return None, f"docker could not start the verify container: {tail}"
    out = out_b.decode(errors="replace")
    passed = proc.returncode == 0
    if cache is not None:
        try:
            report = execution_report(out, cache)
            passed = passed and report.passed
        except ValueError as exc:
            if passed:
                return False, str(exc)
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
    cache_dir: Path,
    service_models: list[Any],
    *,
    symphony: str = "",
    performer_mounts: Sequence[Path],
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
        mounts = _env_cache_mounts(cache_dir, performer_mounts)
        anchored_mkdir(svc_dir, mounts=mounts)
        for name, body in (
            ("services-start.sh", scripts.start),
            ("services-stop.sh", scripts.stop),
            ("services-health.sh", scripts.health),
        ):
            replace_mounted_file(
                svc_dir / name, body.encode(), mounts=mounts, mode=_EXECUTABLE_MODE,
            )
        # 113: do NOT write services.json here. `load_prior_manifest_bounded` derives the
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
    except MountedFileRefusedError as exc:
        logger.error(
            "env_cache.write_refused",
            symphony=symphony,
            cache_dir=str(cache_dir),
            reason=str(exc),
        )
        return False
    except Exception as exc:  # best-effort, never block the bootstrap
        logger.warning(
            "env_cache.service_scripts_write_failed",
            symphony=symphony,
            error=str(exc),
        )
        return False


def rearm_budgets_on_spec_change(cache_state: EnvCacheState, current_sha: str, symphony_name: str) -> None:
    """218 (round 4): a new spec SHA re-arms the infrastructure budget, at any count.

    The 088 breaker re-arms on a spec change, and so must the infrastructure budget. Before this, the
    reset sat inside the verdict-budget branch, so an infrastructure count below the cap survived a
    spec fix. The first SHA, and an unchanged SHA, re-arm nothing."""
    if cache_state.readme_sha is None or current_sha == cache_state.readme_sha:
        return
    if cache_state.infra_failures:
        logger.info(
            "env_cache.infra_budget_reset",
            symphony=symphony_name,
            previous_infra_failures=cache_state.infra_failures,
            new_sha=current_sha,
        )
        cache_state.infra_failures = 0


def _log_provenance_refused(symphony_name: str, reason: str) -> None:
    """234: one place for the refusal log line, called from the handlers that refuse a frozen manifest."""
    logger.error("env_cache.provenance_refused", symphony=symphony_name, reason=reason)


# 234 review 4: the default freshness window for a passing stat check (overridden by config).
DEFAULT_STAT_FRESH_S = 30.0
# 234 review 5: how long a stat walk may run before the dispatches waiting on it are refused (overridden by config).
DEFAULT_STAT_WALK_TIMEOUT_S = 300.0
# 234 review 6: consecutive transient refusals before the operator is told the cache cannot be verified.
TRANSIENT_STREAK_LIMIT = 5


def budget_max_attempts(coordinare_config: Any) -> int:
    """The verdict budget the daemon runs with (``env_bootstrap_max_attempts``), default 3."""
    return int(
        getattr(
            getattr(coordinare_config, "global_config", None),
            "env_bootstrap_max_attempts",
            3,
        )
        or 3,
    )


@dataclass(frozen=True, slots=True)
class _Completion:
    """266: one bootstrap's completion facts, read before the completion changes the state."""

    success: bool
    infra: bool
    dispatch_attempt: int
    was_exhausted: bool
    ended_at: datetime | None
MAX_CONSECUTIVE_STAT_REFUSALS = 3


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
        # 234 review 3: no manifest is held. The stat digest per symphony (manifest digest, stat digest) is small.
        self._stat: dict[str, tuple[str, str]] = {}
        # 234: monotonic time of the last full re-hash per symphony. Absent after a restart, so the first
        # dispatch of a fresh process re-hashes everything.
        self._provenance_full_at: dict[str, float] = {}
        # 234 review: the stat baseline of the last passing hash pass, and the serialised payload, both keyed by
        # symphony and by the digest they were derived from.
        self._payloads: dict[str, tuple[str, str, dict[str, Any]]] = {}
        # 234 review 2: a drift found by a full pass refuses dispatch until the next freeze; the background
        # full passes in flight, by symphony.
        self._drift: dict[str, str] = {}
        # 234 review 4: one stat walk in flight per symphony, and the time of the last passing stat check.
        self._stat_inflight: dict[str, tuple[float, asyncio.Future[Any]]] = {}
        # 234 review 6: consecutive transient refusals per symphony, for the operator-visible surfacing.
        self._transient_streak: dict[str, int] = {}
        self._stat_pass_at: dict[str, tuple[str, float]] = {}
        self._full_inflight: set[str] = set()
        self._kubernetes_state: Mapping[str, Any] = {}

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
                anchored_mkdir(
                    cache_dir, mounts=_env_cache_mounts(cache_dir, self._performer_mounts()),
                )
            except MountedFileRefusedError as exc:
                logger.error(
                    "env_cache.mkdir_refused",
                    symphony=symphony.name,
                    path=str(cache_dir),
                    reason=str(exc),
                )
                continue
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
                    "env_cache.sha_fetch_none", symphony=symphony_name, file=spec_file,
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
        prior = await asyncio.to_thread(
            load_prior_manifest_bounded, cache_dir, performer_mounts=self._performer_mounts(),
        )
        if prior is None:
            return ""

        async def _fetch(path: str) -> str | None:
            try:
                return await github_service.get_file_content(
                    github_org, github_repo, path,
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
        return str(key)[:12]

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
        history = history_of(state)
        recent = history.recent(symphony_name, 1) if history is not None else []
        cancelled_at = (
            recent[0].ended_at if recent and recent[0].outcome == OUTCOME_CANCELLED else None
        )
        if cancelled_at is not None and datetime.now(UTC) < cancelled_at + timedelta(seconds=BOOTSTRAP_CANCEL_COOLDOWN_S):
            return
        self.bind_kubernetes_state(state)
        self.schedule_full_pass(symphony_name, cache_state)

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
            prior = await asyncio.to_thread(
                load_prior_manifest_bounded,
                cache_state.cache_dir,
                performer_mounts=self._performer_mounts(),
            )
            agent_version = prior.agent_version if prior is not None else "no-prior-manifest"
            forced_sha = forced_regen_cache_key(agent_version)
            logger.info(
                "env_cache.forced_regen_triggered",
                symphony=symphony_name,
                agent_version=agent_version,
            )
            # 400: a forced run is keyed on forced_sha, which can never equal
            # the manifest SHA. Without remembering the manifest it was built
            # against, the in-flight cycles queue that SHA as "changed" and
            # completion re-bootstraps for nothing: every forced regen cost two
            # bootstraps. Record it now; completion adopts it as readme_sha.
            cache_state.forced_manifest_sha = await self._current_manifest_sha(
                symphony_name, symphony_config, github_service, eff_config, cache_state,
            )
            if cache_state.forced_manifest_sha is None:
                return
            cache_state.runtime_health_failed = False
            cache_state.bootstrap_forced = True
            try:
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
            finally:
                # _do_dispatch returns without dispatching when a spec file
                # cannot be fetched or the test-env file is invalid, and a
                # synchronous dispatch failure completes (and clears) inside
                # it. In every case with no bootstrap in flight the forced
                # markers must not survive, or the next ORDINARY completion
                # is read as forced and adopts this stale manifest SHA.
                if not cache_state.bootstrap_in_flight:
                    cache_state.bootstrap_forced = False
                    cache_state.forced_manifest_sha = None
            return

        # Fetch combined blob SHA for all watched files (metadata-only, cheap).
        current_sha = await self._current_manifest_sha(
            symphony_name, symphony_config, github_service, eff_config, cache_state,
        )
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
        budget_sha = cache_state.bootstrap_budget_sha or cache_state.forced_manifest_sha or cache_state.readme_sha
        if (
            (cache_state.bootstrap_attempts or cache_state.bootstrap_exhausted)
            and budget_sha is not None
            and current_sha != budget_sha
        ):
            logger.info(
                "env_cache.bootstrap_budget_reset",
                symphony=symphony_name,
                previous_attempts=cache_state.bootstrap_attempts,
                was_exhausted=cache_state.bootstrap_exhausted,
                new_sha=current_sha,
            )
            record_activity(
                state, symphony_name, ACT_BUDGET_RESET,
                f"Budget reset for commit {short_commit(current_sha)}: the env spec changed, so runs start again.",
            )
            cache_state.bootstrap_attempts = 0
            cache_state.bootstrap_exhausted = False
            cache_state.bootstrap_exhaustion_reason = None
            cache_state.runtime_repair_budget = False
            cache_state.bootstrap_budget_sha = current_sha

        # 266: a new commit makes the previous commit's outcome stale. Clear it, so cards and the
        # Symphonies page say the new commit is waiting to bootstrap, not that the old one failed.
        if (
            cache_state.readme_sha is not None
            and current_sha != cache_state.readme_sha
            and cache_state.last_bootstrap_succeeded is not None
            and not cache_state.bootstrap_in_flight
        ):
            cache_state.last_bootstrap_succeeded = None
            cache_state.last_bootstrap_error = None

        # 218 (round 4): a new spec re-arms the infrastructure budget at any count.
        rearm_budgets_on_spec_change(cache_state, current_sha, symphony_name)

        # 088 (FR-009): tripped breaker — no further dispatch for this spec SHA.
        # The (rate-limited implicit) exhaustion was announced once when the
        # breaker tripped; staying silent here avoids a log line per poll cycle.
        if cache_state.bootstrap_exhausted:
            return

        needs_bootstrap = (cache_state.readme_sha is None or current_sha != cache_state.readme_sha
                           or (cache_state.last_bootstrap_succeeded is True and accepted_environment(cache_state) is None))

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
            retry_cooldown = (
                BOOTSTRAP_CANCEL_COOLDOWN_S if cancelled_at is not None else
                BOOTSTRAP_RETRY_COOLDOWN_S * 2 ** (cache_state.bootstrap_attempts + cache_state.infra_failures)
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
            and not _cache_dir_has_activate(
                cache_state.cache_dir,
                performer_mounts=self._performer_mounts(),
                expected_activate=cache_state.rendered_activate_sh,
            )
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
        # un-runnable (None) holds dispatch and enters the bounded retry cooldown.
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
                    "env_cache.restart_verify_passed", symphony=symphony_name,
                )
            elif passed is False:
                logger.warning(
                    "env_cache.restart_verify_failed",
                    symphony=symphony_name,
                    detail=detail,
                )
                cache_state.last_bootstrap_succeeded = False
                cache_state.cache_dir_ready = False
                cache_state.last_bootstrap_error = redact_state_value(f"restart verify failed: {detail}")
                needs_bootstrap = True
            else:
                cache_state.cache_dir_ready = False
                cache_state.last_bootstrap_succeeded = False
                cache_state.last_bootstrap_at = datetime.now(UTC)
                cache_state.last_bootstrap_error = redact_state_value(f"restart verification unavailable: {detail}")
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
        if not await _resolve_bootstrap_commit(github_service, eff_config, cache_state):
            return
        env_spec_contents: dict[str, str] = {}
        for spec_file in symphony_config.env_spec_files:
            try:
                content = await _bootstrap_file_content(
                    github_service, eff_config.github_org, repo, spec_file, cache_state.bootstrap_commit_sha,
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

        self.prepare_bootstrap_generation(cache_state)
        cache_mount_path = f"{container_devenv_root}/{cache_state.sanitised_name}"

        # Service declarations are evidence for the reviewed generic lifecycle.
        # Core execution never selects recipes from a service or language name.
        manages_services = False
        declared_services = await self._fetch_declared_services(
            github_org=eff_config.github_org, repo=repo, github_service=github_service,
            symphony_name=symphony_name, commit=cache_state.bootstrap_commit_sha,
        )

        # 218: forget the previous bootstrap's scripts. Only a complete write in
        # _build_manifest_artifacts sets them again, so a refused or failed write leaves none
        # and the verify fails closed.
        _clear_reviewed_environment(cache_state)
        # 234: the cache is about to be rewritten, so the frozen manifest of the previous one is no longer true.
        # Consumers are refused until the next verified bootstrap freezes a new one.
        cache_state.provenance_digest = None
        cache_state.provenance_frozen_at = None
        cache_state.provenance_stat_digest = None
        cache_state.provenance_inputs = None
        cache_state.provenance_entry_count = 0
        cache_state.provenance_total_bytes = 0
        cache_state.verifier_provenance = None
        self._forget_derived(symphony_name)
        try:
            dependency_checklist, verify_provided, activate_provided = await self._build_manifest_artifacts(
                performer_mounts=self._performer_mounts(),
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
                cache_state=cache_state,
            )

            from coordinare.services.environment_capabilities import installer_capabilities

            target_capabilities = installer_capabilities(cache_state)
        except (RuntimeError, ValueError, OSError) as exc:
            cache_state.readme_sha = current_sha
            cache_state.bootstrap_budget_sha = current_sha
            self.on_bootstrap_complete(
                symphony_name, False,
                dict(self._kubernetes_state) or {"env_cache": {symphony_name: cache_state}},
                error=f"environment discovery/review refused: {type(exc).__name__}: {exc}",
            )
            return


        async with self._bootstrap_execution_window(cache_state):
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
                environment_manifest=cache_state.environment_manifest,
                environment_assessment=cache_state.environment_assessment,
                environment_manifest_digest=(cache_state.environment_assessment["manifest_digest"]
                                             if cache_state.environment_assessment is not None else None),
                environment_verification_image=target_capabilities.image_ref,
                environment_verification_platform=target_capabilities.platform,
                dependency_checklist=dependency_checklist,
                verify_provided=verify_provided,
                activate_provided=activate_provided,
                declared_services=declared_services,
                test_env_vars=test_env_vars,
                coordinare_manages_services=manages_services,
                # 265: the daemon's rendered scripts ride the dispatch. The bootstrap pod cannot read the daemon's cache.
                rendered_verify_sh=cache_state.rendered_verify_sh,
                rendered_activate_sh=cache_state.rendered_activate_sh,
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
            cache_state.bootstrap_budget_sha = cache_state.forced_manifest_sha if cache_state.bootstrap_forced else current_sha
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

    @contextlib.asynccontextmanager
    async def _bootstrap_execution_window(self, cache: EnvCacheState) -> AsyncIterator[None]:
        """Preparation and dispatch share the same expiry as discovery and polling."""
        deadline = max(0.0, journal_for(cache).expires_at - time.time())
        try:
            async with asyncio.timeout(deadline):
                yield
        except TimeoutError:
            state = dict(self._kubernetes_state) or {"env_cache": {cache.symphony_name: cache}}
            self.record_bootstrap_execution(cache.symphony_name, state, None, cleanup_confirmed=False)
            self.on_bootstrap_complete(cache.symphony_name, False, state,
                error="environment bootstrap preparation or dispatch deadline exceeded")

    async def _current_manifest_sha(
        self, symphony_name: str, symphony_config: Any, github_service: Any,
        eff_config: Any, cache_state: EnvCacheState,
    ) -> str | None:
        """The manifest SHA the cache is keyed on: combined spec-file SHA plus
        the inference suffix. One helper so the normal path and the forced
        regen (400) compute it identically."""
        # An immutable repository head covers added/deleted paths as well as changes
        # to already-read inputs. Polling it never invokes discovery or review.
        try:
            head = await github_service.get_branch_head_sha(
                str(getattr(eff_config, "base_branch", "main")),
            )
        except Exception as exc:
            logger.warning("env_cache.source_identity_unavailable", symphony=symphony_name,
                           error_type=type(exc).__name__)
            return None
        if not isinstance(head, str) or not re.fullmatch(r"[0-9a-f]{40}", head):
            return None
        policy: dict[str, Any] = {
            "schema": "351-v1", "head": head,
            "image": getattr(self._coordinare_config.global_config, "performer_image", None),
            "consumer_images": sorted((str(getattr(e, "id", "")), str(getattr(e, "image", "")))
                                      for e in getattr(self._coordinare_config.global_config, "performer_endpoints", [])),
            "conducting": str(getattr(self._coordinare_config.global_config, "conducting", None)),
            "verifier_image": getattr(self._coordinare_config.global_config, "kubernetes_verifier_image", None),
            "limits": getattr(getattr(self._coordinare_config.global_config, "env_cache", None), "model_dump", dict)(),
        }
        return "351-v1:" + head + ":" + hashlib.sha256(json.dumps(policy, sort_keys=True, default=str).encode()).hexdigest()[:12]

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
            performer_mounts=self._performer_mounts(),
        )

    def _cache_refusal_error(self, symphony_name: str, cache_state: EnvCacheState) -> str | None:
        """The bootstrap failure text when the cache dir is refused, or None (219 review).

        A refused dir (a link, hard link or non-directory where the cache must be) is reported as
        such, so the operator is not sent looking for a toolchain in the wrong place.
        """
        refusal = _cache_dir_refusal(
            cache_state.cache_dir, performer_mounts=self._performer_mounts(),
        )
        if refusal is None:
            return None
        logger.error(
            "env_cache.bootstrap_cache_refused",
            symphony=symphony_name,
            cache_dir=str(cache_state.cache_dir),
            reason=refusal,
        )
        return (
            f"env-cache directory refused ({refusal}): {cache_state.cache_dir} is a link, "
            "hard link or non-directory where the cache must be; the bootstrap output cannot "
            "be read or verified. Cache rejected."
        )

    def _performer_mounts(self) -> tuple[Path, ...]:
        """Every host directory a performer can see, from the loaded config (219 review).

        Passed to every env-cache read and write so the outermost performer mount is always the
        trusted start of the anchored walk.
        """
        return loaded_performer_mounts(self._coordinare_config.global_config)

    def _provenance_limits(self) -> ProvenanceLimits:
        """The walk bounds from config (234 review). A config without the knobs keeps the defaults."""
        cfg = getattr(self._coordinare_config.global_config, "env_cache", None)
        if cfg is None:
            return DEFAULT_LIMITS
        return ProvenanceLimits(
            max_entries=int(getattr(cfg, "provenance_max_entries", DEFAULT_LIMITS.max_entries)),
            max_bytes=int(getattr(cfg, "provenance_max_bytes", DEFAULT_LIMITS.max_bytes)),
        )

    def _provenance_root(self) -> Path:
        """The daemon-owned directory for frozen manifests: beside the state file, outside every mount (234)."""
        state_file = getattr(self._coordinare_config.global_config, "state_file_path", None)
        if state_file is None:
            message = "no state_file_path configured; cannot keep a frozen env-cache manifest"
            raise RuntimeError(message)
        return Path(state_file).expanduser().parent / PROVENANCE_DIR_NAME

    def _scope(self) -> ProvenanceScope:
        return ProvenanceScope(self._performer_mounts(), self._provenance_limits())

    def _manifest_path(self, cache_state: EnvCacheState) -> Path:
        return self._provenance_root() / f"{cache_state.sanitised_name}.json"

    def _stat_path(self, cache_state: EnvCacheState) -> Path:
        return self._provenance_root() / f"{cache_state.sanitised_name}.stat.json"

    async def freeze_provenance(
        self, symphony_name: str, state: dict[str, Any],
    ) -> tuple[bool, str | None]:
        """Freeze the just-verified cache (234). Returns ``(True, None)`` or ``(False, reason)``.

        Called by the completion path after the clean verify has passed and the bootstrap container is proven gone.
        Docker streams a manifest to disk. Kubernetes consumes the verifier's bounded summary instead.
        """
        env_cache: dict[str, Any] = state.get("env_cache") or {}
        cache_state: EnvCacheState | None = env_cache.get(symphony_name)
        if cache_state is None or cache_state.rendered_activate_sh is None:
            return False, "no daemon-rendered activate.sh on record; nothing to freeze"
        self.bind_kubernetes_state(state)
        if not daemon_sees_performer_cache(state):
            return self._freeze_claim(symphony_name, cache_state)
        try:
            result = await self._run_walk(
                freeze_cache,
                cache_state.cache_dir,
                (symphony_name, cache_state.readme_sha or ""),
                cache_state.rendered_activate_sh,
                self._scope(),
                self._manifest_path(cache_state),
            )
            await asyncio.to_thread(
                write_stat_sidecar,
                self._stat_path(cache_state),
                manifest_digest=result.manifest.digest,
                stat_digest=result.stat_digest,
                performer_mounts=self._performer_mounts(),
            )
        except (ProvenanceRefusedError, OSError, RuntimeError) as exc:
            _log_provenance_refused(symphony_name, str(exc))
            return False, f"env-cache provenance refused: {exc}"
        cache_state.provenance_digest = result.manifest.digest
        cache_state.provenance_frozen_at = datetime.fromisoformat(result.manifest.meta.frozen_at)
        self._forget_derived(symphony_name)
        self._stat[symphony_name] = (result.manifest.digest, result.stat_digest)
        logger.info(
            "env_cache.provenance_frozen",
            symphony=symphony_name,
            entries=result.entries,
            debs=len(result.manifest.meta.debs),
            digest=result.manifest.digest,
        )
        return True, None

    def bind_kubernetes_state(self, state: Mapping[str, Any]) -> None:
        """Resolve the configured bootstrap runtime afresh, including after a daemon restart."""
        self._kubernetes_state = state

    async def recheck_consumer_provenance(self, symphony_name: str, state: dict[str, Any]) -> None:
        """A consumer stat refusal requests a full content pass before any repair or budget spend."""
        self.bind_kubernetes_state(state)
        ec = (state.get("env_cache") or {}).get(symphony_name)
        if ec is None:
            return
        if ec.runtime_health_failed or (ec.bootstrap_exhausted and ec.bootstrap_exhaustion_reason == "configuration_refusal"):
            return
        self._provenance_full_at.pop(symphony_name, None)
        check = await self.run_full_pass(symphony_name, ec)
        if check is None or check.transient:
            return
        if check.configuration_failure:
            self.refuse_verifier_configuration(ec, state, check.reason or "invalid verifier configuration")
            return
        if not check.ok:
            self.mark_runtime_health_failed(symphony_name, state)
            return
        ec.consumer_stat_refusals += 1
        count = ec.consumer_stat_refusals
        if count > MAX_CONSECUTIVE_STAT_REFUSALS:
            self.mark_runtime_health_failed(symphony_name, state)
            ec.last_bootstrap_error = redact_state_value(
                f"consumer stat digest refused {count} consecutive times; rebuilding within bootstrap budget",
            )
            logger.warning("env_cache.consumer_stat_refusals_exhausted", symphony=symphony_name,
                           count=count, reason=ec.last_bootstrap_error)

    def _freeze_claim(self, symphony_name: str, ec: EnvCacheState) -> tuple[bool, str | None]:
        result = ec.verifier_provenance
        ec.verifier_provenance = None
        if result is None:
            return False, "no verified kubernetes provenance result; re-bootstrap with the current performer image"
        try:
            result = validate_summary(result, ec.rendered_activate_sh or "", self._provenance_limits())
        except (ValueError, UnicodeError, RecursionError) as exc:
            return False, f"kubernetes provenance refused: {exc}"
        self._forget_derived(symphony_name)
        ec.consumer_stat_refusals = 0
        ec.provenance_digest = result["content_digest"]
        ec.provenance_stat_digest = result["stat_digest"]
        ec.provenance_inputs = result["inputs"]
        ec.provenance_entry_count = result["entry_count"]
        ec.provenance_total_bytes = result["total_bytes"]
        ec.provenance_frozen_at = datetime.now(UTC)
        self._provenance_full_at[symphony_name] = time.monotonic()
        return True, None

    async def _check_claim(self, symphony_name: str, ec: EnvCacheState, digest: str) -> ProvenanceCheck:
        state = self._kubernetes_state
        sym_cfg = (state.get("symphony_configs") or {}).get(symphony_name)
        performer_id = getattr(sym_cfg, "env_bootstrap_performer_id", None)
        svc = (state.get("performer_services_by_id") or {}).get(performer_id)
        runtime = getattr(svc, "_runtime", None)
        if runtime is None or not getattr(runtime, "verifies_clean_room", False):
            return ProvenanceCheck(False, "kubernetes verifier runtime unavailable", transient=True)
        limits = self._provenance_limits()
        started = time.monotonic()
        try:
            spec = VerifierSpec(
                image=configured_verifier_image(self._coordinare_config),
                cache_claim="", cache_subpath=ec.sanitised_name,
                mount_path=f"{getattr(svc, 'devenv_root', DEFAULT_DEVENV_ROOT)}/{ec.sanitised_name}",
                verify_text="", project_files={}, activate_text=ec.rendered_activate_sh,
                walk_only=True, max_entries=limits.max_entries, max_bytes=limits.max_bytes,
            )
            ok, detail = await runtime.run_verifier(slug=ec.sanitised_name, spec=spec)
            if ok is None:
                return ProvenanceCheck(False, detail, hashed=True, transient=True)
            result = parse_summary(detail, ec.rendered_activate_sh or "", limits)
        except (VerifierConfigurationError, OSError, TimeoutError, ProvenanceTransientError) as exc:
            configuration_failure = isinstance(exc, VerifierConfigurationError)
            reason = f"kubernetes verifier configuration failure: {exc}" if configuration_failure else str(exc)
            return ProvenanceCheck(False, reason, hashed=True, transient=not configuration_failure,
                                   configuration_failure=configuration_failure)
        except (ValueError, UnicodeError, RecursionError) as exc:
            return self._record_drift(symphony_name, ec, digest, f"kubernetes provenance refused: {exc}")
        if not ok:
            return ProvenanceCheck(False, "kubernetes provenance walker failed", hashed=True, transient=True)
        return self._accept_claim_recheck(symphony_name, ec, digest, result, started)

    def _accept_claim_recheck(
        self, symphony_name: str, ec: EnvCacheState, digest: str, result: dict[str, Any], started: float,
    ) -> ProvenanceCheck:
        if result["content_digest"] != digest or result["inputs"] != ec.provenance_inputs:
            return self._record_drift(symphony_name, ec, digest, "kubernetes cache content changed after freeze")
        if ec.provenance_digest != digest:
            return ProvenanceCheck(False, "frozen provenance changed during the check; retry", transient=True)
        ec.provenance_stat_digest = result["stat_digest"]
        self._provenance_full_at[symphony_name] = started
        return ProvenanceCheck(True, hashed=True)

    def _forget_derived(self, symphony_name: str) -> None:
        """Drop the in-memory views of a manifest: its stat digest, payload, full-pass clock and any drift."""
        self._stat.pop(symphony_name, None)
        self._payloads.pop(symphony_name, None)
        self._provenance_full_at.pop(symphony_name, None)
        self._drift.pop(symphony_name, None)
        self._stat_pass_at.pop(symphony_name, None)
        self._stat_inflight.pop(symphony_name, None)
        self._transient_streak.pop(symphony_name, None)

    async def _open(self, symphony_name: str, cache_state: EnvCacheState, digest: str) -> ManifestFile | None:
        """The manifest for *digest*, opened transiently: digest-verified, metadata read, no rows held (234 review 3)."""
        try:
            path = self._manifest_path(cache_state)
        except RuntimeError as exc:
            _log_provenance_refused(symphony_name, str(exc))
            return None
        try:
            return await asyncio.to_thread(
                open_manifest,
                path,
                digest=digest,
                performer_mounts=self._performer_mounts(),
                max_bytes=manifest_cap_bytes(self._provenance_limits().max_entries),
            )
        except MountedFileRefusedError as exc:
            _log_provenance_refused(symphony_name, str(exc))
            return None
        except OSError as exc:
            # 234 review 6: a read error is not a missing or altered manifest. It is transient, and it is not drift.
            message = f"I/O error reading the frozen manifest: {exc}"
            raise ProvenanceTransientError(message) from exc

    async def _recorded_stat(self, symphony_name: str, cache_state: EnvCacheState, digest: str) -> str | None:
        """The stat digest recorded for *digest*: from memory, else from the small sidecar. None when unknown."""
        held = self._stat.get(symphony_name)
        if held is not None and held[0] == digest:
            return held[1]
        try:
            path = self._stat_path(cache_state)
        except RuntimeError:
            return None
        value = await asyncio.to_thread(
            read_stat_sidecar, path, manifest_digest=digest, performer_mounts=self._performer_mounts(),
        )
        if value is not None:
            self._stat[symphony_name] = (digest, value)
        return value

    def _stat_fresh_seconds(self) -> float:
        """How long a passing stat check is reused without another walk (234 review 4). From config, default 30 s."""
        cfg = getattr(self._coordinare_config.global_config, "env_cache", None)
        if cfg is None:
            return DEFAULT_STAT_FRESH_S
        return float(getattr(cfg, "provenance_stat_fresh_s", DEFAULT_STAT_FRESH_S))

    def _stat_walk_timeout(self) -> float:
        """How long a stat walk may run before the dispatches waiting on it are refused (234 review 5)."""
        cfg = getattr(self._coordinare_config.global_config, "env_cache", None)
        if cfg is None:
            return DEFAULT_STAT_WALK_TIMEOUT_S
        return float(getattr(cfg, "provenance_stat_walk_timeout_s", DEFAULT_STAT_WALK_TIMEOUT_S))

    async def _run_walk(self, fn: Any, *args: Any) -> Any:
        """Run a walk in a thread, off the event loop. Measured lag at 1M entries is p99 about 2 ms (review 4)."""
        return await asyncio.to_thread(fn, *args)

    async def _shared_stat(
        self, symphony_name: str, cache_state: EnvCacheState, arrived: float,
    ) -> tuple[str, float]:
        """The stat digest, and the monotonic time the walk that produced it started (234 review 5).

        A caller joins a walk in flight only if that walk started at or after the caller arrived, so the walk observed the
        cache no earlier than the caller's arrival. An older walk may have seen the cache before a change the caller
        already knows about, so the caller starts its own walk. The walk is shielded, so a cancelled caller does not cancel
        it for the others. A walk that outlives the timeout is dropped from the in-flight table, and this caller is
        refused with a transient error. The next dispatch then starts a new walk.
        """
        entry = self._stat_inflight.get(symphony_name)
        if entry is None or entry[0] < arrived:
            started = time.monotonic()
            fut = asyncio.ensure_future(self._run_walk(stat_digest_of, cache_state.cache_dir, self._scope()))
            entry = (started, fut)
            self._stat_inflight[symphony_name] = entry
        started, walk = entry
        timeout = self._stat_walk_timeout()
        try:
            value = await asyncio.wait_for(asyncio.shield(walk), timeout=timeout)
        except TimeoutError as exc:
            self._drop_inflight(symphony_name, walk)
            message = (
                f"the stat walk did not finish within {timeout:g} s (env_cache.provenance_stat_walk_timeout_s); "
                "this dispatch is refused, and the next one starts a new walk"
            )
            raise ProvenanceTransientError(message) from exc
        return value, started

    def _drop_inflight(self, symphony_name: str, fut: asyncio.Future[Any]) -> None:
        current = self._stat_inflight.get(symphony_name)
        if current is not None and current[1] is fut:
            del self._stat_inflight[symphony_name]

    def _record_drift(
        self, symphony_name: str, cache_state: EnvCacheState, digest: str, reason: str,
    ) -> ProvenanceCheck:
        if cache_state.provenance_digest == digest:
            self._drift[symphony_name] = reason
        return ProvenanceCheck(False, reason, hashed=True)

    async def _content_check(
        self, symphony_name: str, cache_state: EnvCacheState, digest: str,
    ) -> ProvenanceCheck:
        """The full content check (234 review 3, 4 and 5).

        An I/O failure is transient: it is retried once, and if it persists the dispatch is refused without recording drift.
        A mismatch or a refusal is real, and it records drift. On success the stat digest is refreshed (the ctime-churn
        case), and the stat pass is stamped with the time the check started. That stamp is kept only while the digest
        checked is still the current one.
        """
        if getattr(self._coordinare_config, "agent_transport", None) == "kubernetes":
            return await self._check_claim(symphony_name, cache_state, digest)
        try:
            manifest_path = self._manifest_path(cache_state)
        except RuntimeError as exc:
            return self._record_drift(symphony_name, cache_state, digest, str(exc))
        mounts = self._performer_mounts()
        scope = self._scope()
        result = ContentResult(False, "content check did not run")
        started = time.monotonic()
        for _attempt in (1, 2):
            started = time.monotonic()
            try:
                result = await self._run_walk(
                    content_pass, cache_state.cache_dir, manifest_path, digest, mounts, scope,
                )
            except OSError as exc:
                result = ContentResult(False, f"I/O error during the content check: {exc}", transient=True)
            if not result.transient:
                break
        still_current = cache_state.provenance_digest == digest
        if not result.ok:
            if result.transient:
                logger.warning(
                    "env_cache.provenance_check_transient", symphony=symphony_name, reason=result.reason,
                )
                return ProvenanceCheck(False, result.reason, hashed=True, transient=True)
            logger.error(
                "env_cache.provenance_check_failed", symphony=symphony_name, reason=result.reason, full=True,
            )
            return self._record_drift(symphony_name, cache_state, digest, result.reason or "content check failed")
        if still_current and result.stat_digest is not None:
            self._stat[symphony_name] = (digest, result.stat_digest)
            try:
                await asyncio.to_thread(
                    write_stat_sidecar,
                    self._stat_path(cache_state),
                    manifest_digest=digest,
                    stat_digest=result.stat_digest,
                    performer_mounts=mounts,
                )
            except OSError as exc:
                # The sidecar is a restart shortcut. Without it the next restart runs the content check, so this is not fatal.
                logger.warning("env_cache.provenance_sidecar_failed", symphony=symphony_name, error=str(exc))
            self._provenance_full_at[symphony_name] = started
            self._stat_pass_at[symphony_name] = (digest, started)
            logger.info("env_cache.provenance_recheck_full", symphony=symphony_name)
        return ProvenanceCheck(True, None, hashed=True)

    async def _verify(self, symphony_name: str, ec: EnvCacheState) -> tuple[ProvenanceCheck, str | None]:
        if getattr(self._coordinare_config, "agent_transport", None) != "kubernetes":
            return await self._verify_local(symphony_name, ec)
        digest = ec.provenance_digest
        if digest is None or ec.provenance_inputs is None or ec.provenance_stat_digest is None:
            return ProvenanceCheck(False, "no frozen kubernetes provenance; re-bootstrap"), digest
        if symphony_name in self._drift:
            return ProvenanceCheck(False, self._drift[symphony_name]), digest
        if symphony_name not in self._provenance_full_at:
            return await self._content_check(symphony_name, ec, digest), digest
        return ProvenanceCheck(True), digest

    async def _verify_local(self, symphony_name: str, cache_state: EnvCacheState) -> tuple[ProvenanceCheck, str | None]:
        """The dispatch check (234 review 4 and 5). Order: a recorded drift refuses; a passing stat check younger than the
        window is reused with no walk; otherwise one shared stat walk, joined only if it started after this call arrived,
        is compared with the recorded digest; a mismatch, or the first dispatch of a process, runs the content check.
        A transient failure refuses this dispatch only.
        """
        arrived = time.monotonic()
        digest = cache_state.provenance_digest
        if digest is None:
            return ProvenanceCheck(False, "no frozen provenance for this cache; re-bootstrap"), None
        if symphony_name in self._drift:
            return ProvenanceCheck(False, self._drift[symphony_name]), digest
        if symphony_name in self._provenance_full_at:
            passed = self._stat_pass_at.get(symphony_name)
            if (
                passed is not None
                and passed[0] == digest
                and time.monotonic() - passed[1] < self._stat_fresh_seconds()
            ):
                return ProvenanceCheck(True, None, hashed=False), digest
            recorded = await self._recorded_stat(symphony_name, cache_state, digest)
            if recorded is not None:
                try:
                    current, started = await self._shared_stat(symphony_name, cache_state, arrived)
                except ProvenanceTransientError as exc:
                    return ProvenanceCheck(False, str(exc), transient=True), digest
                except ProvenanceRefusedError:
                    current, started = None, None
                if current == recorded and started is not None:
                    self._stat_pass_at[symphony_name] = (digest, started)
                    return ProvenanceCheck(True, None, hashed=False), digest
        return await self._content_check(symphony_name, cache_state, digest), digest

    def schedule_full_pass(self, symphony_name: str, cache_state: EnvCacheState) -> None:
        """Start the hourly full pass in the background when it is due (234 review 2). Never blocks, never raises."""
        if symphony_name in self._full_inflight or cache_state.provenance_digest is None:
            return
        last = self._provenance_full_at.get(symphony_name)
        if last is None or time.monotonic() - last < PROVENANCE_FULL_RECHECK_S:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._full_inflight.add(symphony_name)
        task = loop.create_task(self._scheduled_full_pass(symphony_name, cache_state))
        self._notify_tasks.add(task)
        task.add_done_callback(self._notify_tasks.discard)

    async def _scheduled_full_pass(self, symphony_name: str, cache_state: EnvCacheState) -> None:
        try:
            await self.run_full_pass(symphony_name, cache_state)
        finally:
            self._full_inflight.discard(symphony_name)

    async def run_full_pass(self, symphony_name: str, cache_state: EnvCacheState) -> ProvenanceCheck | None:
        """The full content check of the frozen cache, off the dispatch path (234 review 2 and 3)."""
        digest = cache_state.provenance_digest
        if digest is None:
            return None
        return await self._content_check(symphony_name, cache_state, digest)

    async def _payload_for(
        self, symphony_name: str, cache_state: EnvCacheState, digest: str, container_path: str,
    ) -> dict[str, Any] | None:
        """The small payload for one digest and container path, built once and cached (234 review 3)."""
        if getattr(self._coordinare_config, "agent_transport", None) == "kubernetes":
            if cache_state.provenance_inputs is None or cache_state.provenance_stat_digest is None:
                return None
            limits = self._provenance_limits()
            return {**cache_state.provenance_inputs, "version": 2, "slug": provenance_slug(cache_state.sanitised_name),
                    "cache_path": container_path, "digest": digest, "stat_digest": cache_state.provenance_stat_digest,
                    "max_entries": limits.max_entries, "max_bytes": limits.max_bytes}
        held = self._payloads.get(symphony_name)
        if held is not None and held[0] == digest and held[1] == container_path:
            return held[2]
        manifest = await self._open(symphony_name, cache_state, digest)
        if manifest is None:
            return None
        payload = await asyncio.to_thread(build_payload, manifest.meta, cache_path=container_path, digest=digest)
        self._payloads[symphony_name] = (digest, container_path, payload)
        return payload

    async def resolve_provenance(
        self, symphony_name: str, cache_state: EnvCacheState, *, container_path: str,
    ) -> tuple[ProvenanceCheck, dict[str, Any] | None]:
        """Verify the cache and return the consumer payload, counting transient refusals (234 review 6)."""
        check, payload = await self._resolve_provenance(symphony_name, cache_state, container_path=container_path)
        return self._count_transient(symphony_name, check), payload

    def _count_transient(self, symphony_name: str, check: ProvenanceCheck) -> ProvenanceCheck:
        """A run of transient refusals is surfaced, not retried forever (review 6). No re-bootstrap is started.

        Each transient refusal adds to the streak; a pass or a real refusal clears it. At
        ``TRANSIENT_STREAK_LIMIT`` the refusal reason says so, so the operator sees it in the activity feed and the log.
        """
        if check.ok or not check.transient:
            self._transient_streak.pop(symphony_name, None)
            return check
        count = self._transient_streak.get(symphony_name, 0) + 1
        self._transient_streak[symphony_name] = count
        if count < TRANSIENT_STREAK_LIMIT:
            return check
        message = (
            f"{check.reason}; {count} dispatches in a row failed transiently, so the frozen cache cannot be verified. "
            "No re-bootstrap has been started. Check the disk and the daemon's state directory, then the manifest file."
        )
        logger.error("env_cache.provenance_transient_stuck", symphony=symphony_name, count=count, reason=check.reason)
        return ProvenanceCheck(False, message, hashed=check.hashed, transient=True)

    async def _resolve_provenance(
        self, symphony_name: str, cache_state: EnvCacheState, *, container_path: str,
    ) -> tuple[ProvenanceCheck, dict[str, Any] | None]:
        """Verify the cache and return the consumer payload for the same digest (234 review 3).

        The digest is read again after each await, so a regeneration that clears or replaces it mid-dispatch refuses.
        """
        check, digest = await self._verify(symphony_name, cache_state)
        if not check.ok or digest is None:
            return check, None
        if cache_state.provenance_digest != digest:
            return ProvenanceCheck(False, "frozen provenance changed during the dispatch; retry"), None
        try:
            payload = await self._payload_for(symphony_name, cache_state, digest, container_path)
        except ProvenanceTransientError as exc:
            return ProvenanceCheck(False, str(exc), transient=True), None
        if payload is None:
            return ProvenanceCheck(False, "frozen provenance manifest is missing or altered; re-bootstrap"), None
        if cache_state.provenance_digest != digest:
            return ProvenanceCheck(False, "frozen provenance changed during the dispatch; retry"), None
        return check, payload

    async def check_provenance(
        self, symphony_name: str, cache_state: EnvCacheState,
    ) -> ProvenanceCheck:
        """Verify the cache against its frozen manifest before a consumer dispatch (234). The check alone."""
        check, _digest = await self._verify(symphony_name, cache_state)
        return check

    async def _fetch_declared_services(
        self,
        *,
        github_org: str,
        repo: str,
        github_service: _GitHubService,
        symphony_name: str,
        commit: str | None = None,
    ) -> list[dict[str, Any]]:
        """Best-effort fetch of durably-declared services from .coordinare/score.json (091).

        Returns each declared :class:`ServiceEntry` as a plain dict (model dump) so it
        rides BootstrapJobPayload → card_context into the persona builder, which derives
        the service-binary install items. Any failure (no file, parse/validation error)
        returns ``[]`` — the bootstrap simply derives no service-install item, exactly as
        before 091.
        """
        try:
            content = await _bootstrap_file_content(
                github_service, github_org, repo, ".coordinare/score.json", commit,
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
                error_type=type(exc).__name__,
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

    def prepare_bootstrap_generation(self, cache_state: EnvCacheState) -> None:
        """Prepare rejected execution's fresh generation before computing mount paths."""
        if cache_state.environment_attempt is None:
            return

        journal = journal_for(cache_state)
        if journal.source_commit == cache_state.bootstrap_commit_sha and journal.status == "repair_pending":
            prepare_repair(cache_state)

    async def _checkpoint_environment_attempt(self, cache_state: EnvCacheState, budget: Any) -> None:
        """Await durable reservations; a failed state write prevents external work."""

        save_attempt(cache_state, budget)
        save = self._kubernetes_state.get("snapshot_checkpoint_fn") or self._kubernetes_state.get("snapshot_save_fn")
        if callable(save):
            try:
                await save()
            except Exception:
                message = "environment attempt checkpoint could not be persisted"
                from coordinare.services.environment_attempt import EnvironmentDiscoveryError

                raise EnvironmentDiscoveryError(message, code="attempt_persistence") from None

    async def _build_manifest_artifacts(
        self,
        *,
        performer_mounts: Sequence[Path],
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
        cache_state: EnvCacheState | None = None,
        capabilities: Any = None,
    ) -> tuple[str | None, bool, bool]:
        """Derive the manifest, write an authoritative verify.sh AND activate.sh
        into the cache, and return ``(dependency_checklist, verify_provided,
        activate_provided)``.

        Discovery and independent review are mandatory. Any incomplete evidence
        holds bootstrap; no parser or agent-written script can supply readiness.
        """
        from coordinare_service_inference.environment.render import render_activate, render_verify

        from coordinare.services.environment_capabilities import EnvironmentCapabilityError
        from coordinare.services.environment_discovery import (
            DiscoveryLimits,
            EnvironmentDiscoveryError,
            discover_and_review,
        )

        if cache_state is None or not cache_state.bootstrap_commit_sha:
            message = "environment discovery requires an immutable source commit"
            raise EnvironmentDiscoveryError(message)
        cache_state.environment_capabilities = None
        cache_state.environment_manifest = None
        cache_state.environment_assessment = None
        cache_state.environment_verification = None
        cache_state.rendered_verify_sh = None
        cache_state.rendered_activate_sh = None
        cache_state.cache_dir_ready = False
        cache_state.last_bootstrap_succeeded = None
        commit = cache_state.bootstrap_commit_sha
        async def fetch(path: str) -> str | None:
            return await _bootstrap_file_content(github_service, github_org, repo, path, commit)

        cfg = getattr(self._coordinare_config.global_config, "env_cache", None)
        limits = DiscoveryLimits(
            max_reads=getattr(cfg, "manifest_max_reads", 256),
            max_file_bytes=getattr(cfg, "manifest_max_file_bytes", 256 * 1024),
            max_source_bytes=getattr(cfg, "manifest_max_source_bytes", 4 * 1024 * 1024),
            max_inventory_entries=getattr(cfg, "manifest_max_inventory_entries", 100_000),
            phase_timeout_seconds=getattr(cfg, "manifest_phase_timeout_seconds", 120.0),
            attempt_timeout_seconds=getattr(cfg, "manifest_attempt_timeout_seconds", 600.0),
            max_model_calls=getattr(cfg, "manifest_max_model_calls", 128),
            max_total_reads=getattr(cfg, "manifest_max_total_reads", 512),
            max_repairs=getattr(cfg, "manifest_max_repairs", 2),
        )
        outer_budget = getattr(cfg, "bootstrap_max_seconds",
            getattr(self._coordinare_config.global_config, "bootstrap_max_seconds", 3600))

        budget = begin_attempt(cache_state, limits, outer_seconds=outer_budget)
        previous, feedback = repair_context(cache_state)

        async def checkpoint() -> None:
            await self._checkpoint_environment_attempt(cache_state, budget)

        budget.checkpoint = checkpoint
        try:
            async with asyncio.timeout_at(budget.deadline):
                budget.stage = "capabilities"
                if capabilities is None:
                    from coordinare.services.environment_capabilities import (
                        resolve_execution_capabilities,
                    )

                    capability_state = dict(self._kubernetes_state)
                    capability_state.setdefault("coordinare_config", self._coordinare_config)
                    capability_state.setdefault("env_cache", {symphony_name: cache_state})
                    capabilities = await resolve_execution_capabilities(capability_state, symphony_name)
                from coordinare.services.environment_capabilities import require_cache_mount

                require_cache_mount(capabilities, cache_mount_path)
                budget.stage = "inventory"
                paths = await github_service.get_repository_paths(
                    github_org, repo, ref=commit, max_entries=limits.max_inventory_entries,
                )
                manifest, assessment = await discover_and_review(
                    symphony_name=symphony_name, commit=commit, paths=paths,
                    fetch=fetch, chat=llm_chat, capabilities=capabilities, declared_services=declared_services,
                    limits=limits, budget=budget, previous_manifest=previous, execution_feedback=feedback,
                )
        except TimeoutError:
            budget.error_code = "attempt_deadline"
            message = "environment discovery budget deadline exceeded"
            raise EnvironmentDiscoveryError(message, code="attempt_deadline") from None
        except EnvironmentDiscoveryError as exc:
            budget.error_code = exc.code
            raise
        except EnvironmentCapabilityError as exc:
            budget.error_code = exc.code
            raise EnvironmentDiscoveryError(str(exc), code=exc.code) from None
        except (ValueError, OSError, RuntimeError):
            budget.error_code = "capability_unavailable" if budget.stage == "capabilities" else "inventory_unavailable"
            message = "environment runtime or source inventory is unavailable"
            raise EnvironmentDiscoveryError(message, code=budget.error_code) from None
        finally:
            save_attempt(cache_state, budget)
            logger.info("env_cache.manifest_attempt", symphony=symphony_name, **budget.summary())
        activation = render_activate(manifest, cache_mount_path=cache_mount_path)
        verification = render_verify(manifest, cache_mount_path=cache_mount_path)
        mounts = _env_cache_mounts(cache_dir, performer_mounts)
        anchored_mkdir(cache_dir, mounts=mounts)
        try:
            for name, text in (("activate.sh", activation), ("verify.sh", verification),
                               ("manifest.json", manifest.model_dump_json())):
                replace_mounted_file(
                    cache_dir / name, text.encode(), mounts=mounts,
                    mode=_EXECUTABLE_MODE if name.endswith(".sh") else None,
                )
        except MountedFileRefusedError as exc:
            logger.error("env_cache.write_refused", symphony=symphony_name,
                         cache_dir=str(cache_dir), reason=str(exc))
            raise
        # Publication occurs only after the complete, independently accepted contract
        # and all informational cache copies have been written successfully.
        cache_state.environment_capabilities = capabilities.model_dump(mode="json")
        cache_state.environment_manifest = manifest.model_dump(mode="json")
        cache_state.environment_assessment = assessment.model_dump(mode="json")
        cache_state.manifest_items = [requirement.model_dump(mode="json") for requirement in manifest.requirements]
        cache_state.rendered_activate_sh = activation
        cache_state.rendered_verify_sh = verification
        save_attempt(cache_state, budget, executing=True)
        await self._checkpoint_environment_attempt(cache_state, budget)
        logger.info("env_cache.manifest_accepted", symphony=symphony_name,
                    digest=manifest.digest(), requirements=len(manifest.requirements))
        return "ACCEPTED ENVIRONMENT CONTRACT\n" + manifest.model_dump_json(indent=2), True, True

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
        if not cache_state.runtime_health_failed:
            cache_state.runtime_repair_budget = True
            cache_state.bootstrap_budget_sha = cache_state.bootstrap_budget_sha or cache_state.forced_manifest_sha or cache_state.readme_sha
            self._spend_breaker_attempt(symphony_name, cache_state, state)
        cache_state.runtime_health_failed = True
        cache_state.cache_dir_ready = False
        getattr(self, "_provenance_full_at", {}).pop(symphony_name, None)
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

    def refuse_verifier_configuration(
        self, cache_state: EnvCacheState, state: Mapping[str, Any], detail: str,
    ) -> str:
        """Refuse retries for invalid operator inputs and report the exhaustion transition once."""
        cache_state.cache_dir_ready = False
        cache_state.last_bootstrap_succeeded = False
        cache_state.last_bootstrap_error = redact_state_value(
            f"env verification configuration failure: {detail}; correct configuration and request env regeneration",
        )
        reason = str(cache_state.last_bootstrap_error)
        cache_state.verifier_provenance = None
        cache_state.bootstrap_exhaustion_reason = "configuration_refusal"
        if not cache_state.bootstrap_exhausted:
            cache_state.bootstrap_exhausted = True
            self._notify_bootstrap_exhausted(
                cache_state.symphony_name, cache_state, state,
                summary=f"Env bootstrap for '{cache_state.symphony_name}' halted: {reason}",
            )
            record_activity(state, cache_state.symphony_name, ACT_EXHAUSTED, reason)
        logger.error("env_cache.verifier_configuration_refused", symphony=cache_state.symphony_name, reason=reason)
        return reason

    def _notify_bootstrap_exhausted(
        self,
        symphony_name: str,
        cache_state: EnvCacheState,
        state: Mapping[str, Any],
        *,
        summary: str | None = None,
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
        event = NotificationEvent(
            event_type=EventType.circuit_breaker_trip,
            severity=NotificationSeverity.warning,
            payload={
                "summary": summary or (
                    f"🔌 Env bootstrap for '{symphony_name}' exhausted its "
                    f"{cache_state.bootstrap_attempts}-try budget; no further "
                    f"bootstraps until the env spec changes."
                ),
                "symphony": symphony_name,
                "attempts": str(cache_state.bootstrap_attempts),
                "last_error": redact_state_value(str(cache_state.last_bootstrap_error or "")),
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

    def _record_infra_failure(
        self,
        symphony_name: str,
        cache_state: EnvCacheState,
        state: dict[str, Any],
        error: str | None,
    ) -> None:
        """218 (review round 3): record one infrastructure failure, escalate, and cap it.

        The verify could not run, or the bootstrap container could not be proven quiet. The verdict
        budget is not spent. The cooldown escalates with this count (check_and_trigger). At the cap the
        cache is exhausted, and the reason says it was infrastructure, so an operator can see it.
        """
        cache_state.infra_failures += 1
        cache_state.last_infra_failure_at = datetime.now(UTC)
        logger.error(
            "env_cache.bootstrap_infra_failed",
            symphony=symphony_name,
            detail=error,
            infra_failures=cache_state.infra_failures,
            cap=_INFRA_FAILURE_CAP,
            hint="check the docker daemon and the performer image; the retry waits longer each time",
        )
        if cache_state.infra_failures >= _INFRA_FAILURE_CAP and not cache_state.bootstrap_exhausted:
            cache_state.bootstrap_exhausted = True
            cache_state.last_bootstrap_error = redact_state_value(
                f"infrastructure failures exhausted after {cache_state.infra_failures} runs: {error}",
            )
            logger.error(
                "env_cache.bootstrap_infra_exhausted",
                symphony=symphony_name,
                infra_failures=cache_state.infra_failures,
                last_error=cache_state.last_bootstrap_error,
            )
            self._notify_bootstrap_exhausted(symphony_name, cache_state, state)

    def _report_outcome(
        self,
        state: dict[str, Any],
        symphony_name: str,
        cache_state: EnvCacheState,
        facts: _Completion,
    ) -> None:
        """266: say how a bootstrap run ended, in the history and the feed. Read-only for the decisions.

        The retry time is the backoff the next cycle applies to this failure.
        """
        ended_at = facts.ended_at or datetime.now(UTC)
        retry_at = None
        if not facts.success and not cache_state.bootstrap_exhausted:
            backoff = BOOTSTRAP_RETRY_COOLDOWN_S * 2 ** (cache_state.bootstrap_attempts + cache_state.infra_failures)
            retry_at = ended_at + timedelta(seconds=backoff)
        report_completion(
            state,
            symphony_name,
            AttemptEnd(
                ended_at=ended_at, succeeded=facts.success, reason=cache_state.last_bootstrap_error,
                commit=cache_state.readme_sha or "", attempt=facts.dispatch_attempt, infra=facts.infra,
            ),
            BudgetNow(
                max_attempts=budget_max_attempts(self._coordinare_config),
                exhausted_now=cache_state.bootstrap_exhausted and not facts.was_exhausted,
                retry_at=retry_at,
            ),
        )

    def _spend_breaker_attempt(
        self, symphony_name: str, cache_state: EnvCacheState, state: dict[str, Any],
    ) -> None:
        """088 (FR-009): one real bootstrap failure spends the attempt budget; exhaustion trips it."""
        # The first runtime failure requests a repair; it must allow that repair even with a one-attempt budget.
        # Keep the charge so a failed repair or another refusal after a successful repair can trip the breaker.
        first_runtime_failure = cache_state.runtime_repair_budget and not cache_state.bootstrap_attempts
        cache_state.bootstrap_attempts += 1
        max_attempts = budget_max_attempts(self._coordinare_config)
        if not first_runtime_failure and cache_state.bootstrap_attempts >= max_attempts and not cache_state.bootstrap_exhausted:
            cache_state.bootstrap_exhausted = True
            logger.warning(
                "env_cache.bootstrap_exhausted",
                symphony=symphony_name,
                attempts=cache_state.bootstrap_attempts,
                max_attempts=max_attempts,
                last_error=cache_state.last_bootstrap_error,
            )
            self._notify_bootstrap_exhausted(symphony_name, cache_state, state)

    def _mark_bootstrap_success(self, symphony_name: str, cache_state: EnvCacheState) -> None:
        """A finished bootstrap succeeded: the cache is usable and the budgets it may clear are cleared."""
        cache_state.cache_dir_ready = True
        # 218 (review round 3): a success clears the infrastructure budget.
        cache_state.infra_failures = 0
        # A runtime repair keeps its refusal budget, including through successful rebuilds.
        if not cache_state.runtime_repair_budget:
            cache_state.bootstrap_attempts = 0
            cache_state.bootstrap_exhausted = False
            cache_state.bootstrap_exhaustion_reason = None
        # 088 (US5): an in-process success was already clean-verified by the
        # completion path, so the restart honor verify must not re-fire.
        self._restart_verified.add(symphony_name)

    def on_bootstrap_cancelled(self, symphony_name: str, state: dict[str, Any], actor: str) -> None:
        """Close an operator stop without changing either retry budget or the recorded SHA."""
        cache_state = (state.get("env_cache") or {}).get(symphony_name)
        if cache_state is None:
            return
        cache_state.bootstrap_in_flight = False
        cache_state.last_bootstrap_at = datetime.now(UTC)
        cache_state.last_bootstrap_succeeded = False
        cache_state.last_bootstrap_error = redact_state_value(f"cancelled by {actor}")
        report_completion(
            state, symphony_name,
            AttemptEnd(cache_state.last_bootstrap_at, False, cache_state.last_bootstrap_error,
                       cache_state.readme_sha or "", cache_state.bootstrap_attempts + 1, cancelled_by=actor),
            BudgetNow(budget_max_attempts(self._coordinare_config), False,
                      cache_state.last_bootstrap_at + timedelta(seconds=BOOTSTRAP_CANCEL_COOLDOWN_S)),
        )
        self._flush_bootstrap_snapshot(state, symphony_name)

    def record_bootstrap_execution(
        self, symphony_name: str, state: dict[str, Any], evidence: dict[str, Any] | None,
        *, cleanup_confirmed: bool,
    ) -> None:
        """Admit rejected execution separately from publishing bootstrap completion."""
        cache = (state.get("env_cache") or {}).get(symphony_name)
        if cache is not None and cache.environment_attempt is not None:
            finish_attempt(cache, False, evidence, cleanup_confirmed=cleanup_confirmed)

    def on_bootstrap_complete(
        self,
        symphony_name: str,
        success: bool,
        state: dict[str, Any],
        error: str | None = None,
        *,
        infra_failure: bool = False,
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

        # 266: the budget number this run was dispatched under, and the budget state, read before any change.
        pre_completion = (cache_state.bootstrap_attempts + 1, cache_state.bootstrap_exhausted)
        cache_state.bootstrap_in_flight = False
        cache_state.last_bootstrap_at = datetime.now(UTC)

        # A transport's boolean cannot waive independent review, executable
        # evidence or frozen provenance. The accepted generic contract decides
        # installed layout; a verified flat file is as valid as a directory.
        refusal_error = self._completion_refusal(symphony_name, cache_state) if success else None
        if refusal_error is not None:
            success, error = False, refusal_error

        try:
            finish_attempt(cache_state, success, None, cleanup_confirmed=False)
        except (ValueError, RuntimeError):
            success, error = False, "environment attempt deadline or evidence refused completion"

        cache_state.last_bootstrap_succeeded = success
        if not success:
            cache_state.cache_dir_ready = False
        # 077: surface WHY a bootstrap failed (dashboard); clear on success.
        cache_state.last_bootstrap_error = redact_state_value(None if success else (error or "bootstrap failed"))

        # 061: Mark the cache usable only after a successful bootstrap.
        # cache_dir_ready stays False until at least one bootstrap succeeds,
        # which prevents performers from mounting an unpopulated cache dir.
        if success:
            self._mark_bootstrap_success(symphony_name, cache_state)

        # 088 (FR-009): on failure, KEEP the recorded SHA. Clearing it (the
        # pre-088 behaviour) made the next cycle recompute needs_bootstrap=True
        # and redispatch immediately — the observed unbounded ~100s hammering.
        # With the SHA retained, retries flow through check_and_trigger's
        # escalating-cooldown branch (readme_sha == current ∧ succeeded=False),
        # and the consumer gate still holds (last_bootstrap_succeeded=False).
        if not success and infra_failure:
            # 218 (review round 3): an infrastructure failure spends its own budget, not the verdict budget.
            self._record_infra_failure(symphony_name, cache_state, state, error)
        elif not success:
            self._spend_breaker_attempt(symphony_name, cache_state, state)

        logger.info(
            "env_cache.bootstrap_complete",
            symphony=symphony_name,
            success=success,
        )
        # 266: the one place a finished run is closed in the history and the feed.
        self._report_outcome(
            state, symphony_name, cache_state,
            _Completion(
                success=success, infra=infra_failure, dispatch_attempt=pre_completion[0],
                was_exhausted=pre_completion[1], ended_at=cache_state.last_bootstrap_at,
            ),
        )

        # 400: a forced run was keyed on the forced key; the cache it built
        # corresponds to the manifest recorded at dispatch. Adopt that SHA so
        # the pending comparison below, and every later cycle, compares
        # manifest with manifest. Without this the forced key never matched
        # and the next cycle rebuilt an identical cache.
        if cache_state.bootstrap_forced:
            if cache_state.forced_manifest_sha:
                cache_state.readme_sha = cache_state.forced_manifest_sha
            cache_state.bootstrap_forced = False
            cache_state.forced_manifest_sha = None

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

        self._flush_bootstrap_snapshot(state, symphony_name)

    def _completion_refusal(self, symphony_name: str, cache_state: EnvCacheState) -> str | None:
        if accepted_environment(cache_state) is None:
            return "no independently accepted environment manifest; rebuild the cache"
        if not saved_execution_accepted(cache_state):
            return "required clean consumer execution is missing or failed; rebuild the cache"
        if cache_state.provenance_digest is None:
            return "frozen environment provenance is missing; rebuild the cache"
        return self._cache_refusal_error(symphony_name, cache_state)

    def _flush_bootstrap_snapshot(self, state: dict[str, Any], symphony_name: str) -> None:
        """Save completion without waiting for a later lifecycle transition."""
        # 088 (US5): flush the completion to disk NOW — it moves no lifecycle
        # signature, so the daemon's signature-gated save would defer it to the
        # next stage transition and a restart in that window would rewind it.
        save_fn = state.get("snapshot_save_fn")
        if callable(save_fn):
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                logger.warning(
                    "env_cache.snapshot_flush_skipped", symphony=symphony_name,
                )
            else:
                task = loop.create_task(save_fn())
                self._notify_tasks.add(task)
                task.add_done_callback(self._notify_tasks.discard)
