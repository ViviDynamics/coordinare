"""Inference cache-key composition for spec 063 Phase 3.

The env-cache build is invalidated when any path the agent *actually read* (the
manifest's ``cache_inputs``) changes. This module computes the cache key used
to decide whether to reuse a sealed env-cache or rebuild.

Composition (plan.md):

    inference_cache_key = sha256(
        agent_version
        + sha256(concat(read(p) for p in prior_cache_inputs))
    )

First-run fallback (no prior manifest): caller supplies ``fallback_paths`` —
typically the symphony's ``env_spec_files``. Operating without any input list
would make the key degenerate to ``sha256(agent_version)`` which would defeat
invalidation entirely, so a non-empty fallback list is required.

Forced regeneration (Phase 4, self-healing): :func:`forced_regen_cache_key`
returns a unique key including a cryptographic random nonce so the next
bootstrap is guaranteed to miss the cache.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

import structlog

from coordinare_service_inference.manual_override import SERVICES_SUBDIR
from coordinare_service_inference.schema import ServicesManifest

_log = structlog.get_logger(__name__)

if TYPE_CHECKING:
    from pathlib import Path

__all__ = [
    "ContentFetcher",
    "compute_inference_cache_key",
    "forced_regen_cache_key",
    "load_prior_manifest",
]


ContentFetcher = Callable[[str], Awaitable[str | None]]
"""Async callable returning file content (or None if missing) for a repo-relative path."""


def load_prior_manifest(cache_dir: Path) -> ServicesManifest | None:
    """Read the persisted services.json from a sealed env-cache, or return None.

    Returns None for any of: missing file, unreadable file, JSON parse error,
    schema-invalid manifest. Callers treat None as "first run" — they fall back
    to the broader cache-input set.
    """
    path = cache_dir / SERVICES_SUBDIR / "services.json"
    try:
        text = path.read_text()
    except (FileNotFoundError, NotADirectoryError, PermissionError):
        return None
    try:
        return ServicesManifest.model_validate_json(text)
    except Exception:
        return None


async def compute_inference_cache_key(
    *,
    agent_version: str,
    prior_manifest: ServicesManifest | None,
    content_fetcher: ContentFetcher,
    fallback_paths: list[str] | None = None,
) -> str:
    """Compute the SHA-256 hex digest used as the env-cache invalidation key.

    Args:
      agent_version: Identifier for the agent/prompt revision. Mixed into the
        key so an agent upgrade forces a rebuild even with identical inputs.
      prior_manifest: The manifest from the previous successful bootstrap, or
        None on first run.
      content_fetcher: Async callable that returns file content for a
        repo-relative path, or None if the path is missing. Used uniformly for
        prior cache_inputs and fallback paths so both backends (GitHub blob,
        local filesystem) plug in identically.
      fallback_paths: Paths to hash when ``prior_manifest`` is None. Must be
        a non-empty list — passing an empty list raises ValueError to prevent
        the key from collapsing to ``sha256(agent_version)``.

    A path that the fetcher reports as missing (None) is recorded with a
    distinct ``"missing"`` marker (vs. ``"sha256"`` for present files) so a
    file whose literal content is the string ``<missing>`` cannot collide
    with a truly absent file. Per-file hashing also bounds memory: large
    inputs are reduced to 64-hex digests before bundling.
    """
    paths = (
        sorted(prior_manifest.cache_inputs)
        if prior_manifest is not None
        else sorted(fallback_paths or [])
    )

    if not paths:
        if prior_manifest is not None:
            # An empty cache_inputs is operator-actionable: the agent read
            # nothing, which would defeat invalidation. Force a miss by
            # mixing in a sentinel rather than silently reusing.
            _log.warning(
                "inference_cache_key.empty_cache_inputs",
                agent_version=agent_version,
                note="prior manifest declared no cache_inputs; forcing cache miss every build",
            )
            paths_marker = "<empty-cache-inputs>"
        else:
            raise ValueError(
                "compute_inference_cache_key requires non-empty fallback_paths "
                "when prior_manifest is None"
            )
    else:
        paths_marker = None

    bundle: dict[str, list[str]] = {}
    for path in paths:
        content = await content_fetcher(path)
        if content is None:
            bundle[path] = ["missing", ""]
        else:
            digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
            bundle[path] = ["sha256", digest]

    if paths_marker is not None:
        bundle[paths_marker] = ["marker", ""]

    inner = hashlib.sha256(
        json.dumps(bundle, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return hashlib.sha256(f"{agent_version}|{inner}".encode()).hexdigest()


def forced_regen_cache_key(agent_version: str) -> str:
    """Return a unique key that forces a cache miss on the next bootstrap.

    Used by Phase 4 self-healing on runtime health failure. Mixes in a
    cryptographic-quality random nonce so concurrent self-heal events on the
    same host cannot collide (which a wall-clock nonce could in theory do
    under coarse timers).
    """
    nonce = secrets.token_hex(16)
    return hashlib.sha256(f"{agent_version}|{nonce}|forced-regen".encode()).hexdigest()


def local_path_fetcher(project_root: Path) -> ContentFetcher:
    """Build a :class:`ContentFetcher` that reads from the local filesystem.

    Resolves repo-relative paths against ``project_root``. Refuses any path
    whose realpath escapes the project root (defends against ``..`` traversal
    or symlink escape in malicious manifests) by returning None.
    """
    root_resolved = project_root.resolve()

    async def _fetch(path: str) -> str | None:
        candidate = (project_root / path).resolve()
        try:
            candidate.relative_to(root_resolved)
        except ValueError:
            return None
        try:
            return candidate.read_text()
        except (FileNotFoundError, IsADirectoryError, PermissionError):
            return None

    return _fetch


__all__ += ["local_path_fetcher"]
