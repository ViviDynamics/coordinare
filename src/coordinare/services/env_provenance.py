"""Spec 234: the daemon freezes a verified env cache and checks it before every consumer dispatch.

The bootstrap performer writes the cache. After the daemon verifies it, consumers must run only what the daemon
verified. This module holds the mechanism:

- :func:`freeze_cache` walks the cache (anchored, see ``mounted_file.walk_mounted_tree``) and returns a
  :class:`ProvenanceManifest`: every entry with its kind, mode, size, inode, ``ctime_ns`` and sha256, the link
  targets, and the rendered scripts the consumers will run.
- :func:`save_manifest` and :func:`load_manifest` keep it in a daemon-owned file. A load trusts the file only
  when it hashes to the digest the snapshot holds.
- :func:`check_cache` compares the cache with the manifest. The cheap pass is stat-only. A mismatch, and the
  periodic full pass, re-hash every file. A passing full pass hands back the stat baseline it observed, so the
  next stat pass does not re-hash ctime-only churn.
- :func:`build_payload` is the block a consumer receives: the frozen steps and the debs, never the cache's files.

Nothing here runs a file from the cache. The module reads bytes and compares them.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, NamedTuple

import structlog
from coordinare_service_inference.manual_override import SERVICES_SUBDIR

from coordinare.services.mounted_file import (
    MountedFileRefusedError,
    TreeBoundError,
    TreeEntry,
    anchored_mkdir,
    read_mounted_file,
    replace_mounted_file,
    walk_mounted_tree,
)

logger = structlog.get_logger(__name__)

_GIB = 1024 * 1024 * 1024


@dataclass(frozen=True)
class ProvenanceLimits:
    """The bounds for one cache walk. A cache over a bound is refused, naming the config knob."""

    max_entries: int
    max_bytes: int


# 234: bounds sized for real toolchains (a Node install is about 40k entries and 470 MiB; an rbenv about
# 480k entries and 7.7 GB). The config knobs default to these. A refusal names the knob that was hit.
DEFAULT_LIMITS = ProvenanceLimits(max_entries=1_000_000, max_bytes=16 * _GIB)

PROVENANCE_MAX_SCRIPT_BYTES = 256 * 1024
# 234 review 2: the manifest is bounded by the entry bound. Each entry may carry a path up to
# PROVENANCE_MAX_PATH_BYTES, plus the fixed JSON overhead of one entry. The cap is their product, so a cache at the
# default bound fits and a cache over the entry bound is refused on the entry knob.
PROVENANCE_MAX_PATH_BYTES = 512
_MANIFEST_OVERHEAD_BYTES_PER_ENTRY = 256


def manifest_cap_bytes(max_entries: int) -> int:
    """The largest saved manifest accepted for a cache of *max_entries* entries (234 review 2)."""
    return max_entries * (PROVENANCE_MAX_PATH_BYTES + _MANIFEST_OVERHEAD_BYTES_PER_ENTRY)

# 234: the periodic full re-hash. The stat pass cannot see a rewrite that restores size, inode and ctime
# inside the filesystem's timestamp granularity, so a full pass runs at least this often.
PROVENANCE_FULL_RECHECK_S = 3600.0

# 234: the performer reports this on GET /status. An image without it cannot take a frozen payload.
PROVENANCE_CAPABILITY_VERSION = 1
OLD_IMAGE_REASON = "performer image too old for env-cache provenance (#234)"

# 234: the daemon-owned directory under the state directory, outside every performer mount.
PROVENANCE_DIR_NAME = "env-provenance"

# 234: a slug names the frozen directory on the performer side. It is derived from the cache directory name by
# :func:`provenance_slug`, which the performer implements identically (agent/performer/.../env_provenance.py).
PROVENANCE_SLUG_MAX = 120
PROVENANCE_SLUG_HASH_CHARS = 8
_SLUG_VALID = re.compile(r"[a-z0-9_][a-z0-9_-]*")
_SLUG_INVALID = re.compile(r"[^a-z0-9_-]")

_START_RELPATH = f"{SERVICES_SUBDIR}/services-start.sh"
_HEALTH_RELPATH = f"{SERVICES_SUBDIR}/services-health.sh"
_ACTIVATE_RELPATH = "activate.sh"
_DEBS_PREFIX = "debs/"
_REASON_LIMIT = 10
_MAX_LINK_HOPS = 40

Stats = dict[str, tuple[int, int, int]]


class ProvenanceRefusedError(Exception):
    """The cache cannot be frozen. The message says why, for the bootstrap failure and the log."""


def provenance_slug(cache_dir_name: str) -> str:
    """The performer-side directory name for a cache directory name (234).

    A name that already fits the slug pattern and the length bound is kept as it is. Any other name is cleaned to
    the pattern, truncated, and suffixed with a short hash of the original, so two long names never collide.
    The performer's ``provenance_slug`` returns the same string for every input.
    """
    if len(cache_dir_name) <= PROVENANCE_SLUG_MAX and _SLUG_VALID.fullmatch(cache_dir_name):
        return cache_dir_name
    cleaned = _SLUG_INVALID.sub("-", cache_dir_name.lower()).lstrip("-") or "symphony"
    head = cleaned[: PROVENANCE_SLUG_MAX - PROVENANCE_SLUG_HASH_CHARS - 1]
    digest = hashlib.sha256(cache_dir_name.encode()).hexdigest()[:PROVENANCE_SLUG_HASH_CHARS]
    return f"{head}-{digest}"


class ProvenanceEntry(NamedTuple):
    """One manifest row. A named tuple, not a model: a million validated models cost gigabytes on restart (234 review 2)."""

    path: str
    kind: str  # "file", "dir" or "link"
    mode: int
    size: int
    ino: int
    ctime_ns: int
    sha256: str | None = None
    target: str | None = None


_ENTRY_KINDS = frozenset({"file", "dir", "link"})


@dataclass(frozen=True)
class ProvenanceManifest:
    slug: str
    symphony: str
    cache_key: str
    frozen_at: datetime
    activate_sh: str
    services_start_sh: str | None = None
    services_health_sh: str | None = None
    entries: tuple[ProvenanceEntry, ...] = ()

    @property
    def debs(self) -> dict[str, str]:
        """The debs the manifest froze, by cache-relative path, with their sha256."""
        return {
            e.path: e.sha256 or ""
            for e in self.entries
            if e.kind == "file" and e.path.startswith(_DEBS_PREFIX) and e.path.endswith(".deb")
        }


@dataclass(frozen=True)
class ProvenanceScope:
    """Where a walk may read (the performer mounts) and how large a cache it may walk (the limits)."""

    mounts: tuple[Path, ...]
    limits: ProvenanceLimits = DEFAULT_LIMITS


@dataclass(frozen=True)
class ProvenanceCheck:
    ok: bool
    reason: str | None = None
    hashed: bool = False
    # The (size, ino, ctime_ns) of every file, as observed by the pass that hashed the cache. Set only on a
    # passing hash pass. The caller keeps it as the baseline for the next stat pass.
    stats: Stats | None = None


def _mounts_for(cache_dir: Path, performer_mounts: tuple[Path, ...] | list[Path]) -> tuple[Path, ...]:
    """The performer mounts plus the cache's parent (the env-cache root), as the 219 rules require."""
    return (*performer_mounts, cache_dir.parent)


def _link_refusal(rel: str, target: str, links: dict[str, str]) -> str | None:
    """Why a link leaves the cache or cannot be resolved, or None when it stays inside (234 review).

    Each component of the target is resolved in turn. A component that names another link in the cache is
    followed through that link's own target, so a chain of links cannot reach outside by way of a ``..``
    that only the kernel would apply. The hop count bounds the chain.
    """
    if not target or target.startswith("/"):
        return "points outside the cache"
    current = list(PurePosixPath(rel).parent.parts)
    stack = list(reversed(target.split("/")))
    hops = 0
    while stack:
        part = stack.pop()
        if part in ("", "."):
            continue
        if part == "..":
            if not current:
                return "points outside the cache"
            current.pop()
            continue
        key = "/".join([*current, part])
        if key in links:
            hops += 1
            if hops > _MAX_LINK_HOPS:
                return f"is a link loop (more than {_MAX_LINK_HOPS} hops)"
            through = links[key]
            if not through or through.startswith("/"):
                return "points outside the cache"
            stack.extend(reversed(through.split("/")))
            continue
        current.append(part)
    return None


def _to_entries(tree: list[TreeEntry]) -> list[ProvenanceEntry]:
    return [
        ProvenanceEntry(
            path=t.rel,
            kind=t.kind,
            mode=t.mode,
            size=t.size,
            ino=t.ino,
            ctime_ns=t.ctime_ns,
            sha256=t.sha256,
            target=t.target,
        )
        for t in tree
    ]


def _read_script(
    cache_dir: Path, rel: str, entries: dict[str, ProvenanceEntry], mounts: tuple[Path, ...],
) -> str | None:
    """The frozen bytes of one script, or None when the cache has no such file. Refused on a mismatch."""
    entry = entries.get(rel)
    if entry is None:
        return None
    if entry.kind != "file":
        message = f"{rel} is not a regular file"
        raise ProvenanceRefusedError(message)
    data = read_mounted_file(cache_dir / rel, max_bytes=PROVENANCE_MAX_SCRIPT_BYTES, mounts=mounts)
    if data is None:
        message = f"{rel} is missing, linked, or larger than {PROVENANCE_MAX_SCRIPT_BYTES} bytes"
        raise ProvenanceRefusedError(message)
    if hashlib.sha256(data).hexdigest() != entry.sha256:
        message = f"{rel} changed while the cache was frozen"
        raise ProvenanceRefusedError(message)
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        message = f"{rel} is not UTF-8"
        raise ProvenanceRefusedError(message) from exc


_KNOB_FOR_BOUND = {
    "entries": "env_cache.provenance_max_entries",
    "bytes": "env_cache.provenance_max_bytes",
}


def _bound_message(exc: MountedFileRefusedError) -> str:
    """A refusal from the walk. A bound names the knob for its kind (234 review 2: from the structured kind, not text)."""
    if isinstance(exc, TreeBoundError):
        knob = _KNOB_FOR_BOUND[exc.bound]
        return f"cache {exc}; raise {knob} if this cache is legitimately this large"
    return f"cache {exc}"


def freeze_cache(
    cache_dir: Path,
    *,
    symphony: str,
    cache_key: str,
    activate_sh: str,
    scope: ProvenanceScope,
) -> ProvenanceManifest:
    """Walk the verified cache and return its manifest. Raises :class:`ProvenanceRefusedError` on any refusal."""
    if len(activate_sh.encode()) > PROVENANCE_MAX_SCRIPT_BYTES:
        message = f"the rendered activate.sh exceeds {PROVENANCE_MAX_SCRIPT_BYTES} bytes"
        raise ProvenanceRefusedError(message)
    mounts = _mounts_for(cache_dir, scope.mounts)
    try:
        tree = walk_mounted_tree(
            cache_dir,
            mounts=mounts,
            hash_files=True,
            max_entries=scope.limits.max_entries,
            max_bytes=scope.limits.max_bytes,
        )
    except MountedFileRefusedError as exc:
        raise ProvenanceRefusedError(_bound_message(exc)) from exc
    longest = max((t for t in tree), key=lambda t: len(t.rel.encode()), default=None)
    if longest is not None and len(longest.rel.encode()) > PROVENANCE_MAX_PATH_BYTES:
        message = f"path longer than {PROVENANCE_MAX_PATH_BYTES} bytes cannot be frozen: {longest.rel[:80]}..."
        raise ProvenanceRefusedError(message)
    links = {t.rel: t.target or "" for t in tree if t.kind == "link"}
    for rel, target in links.items():
        reason = _link_refusal(rel, target, links)
        if reason is not None:
            message = f"link {rel} {reason} (target {target!r})"
            raise ProvenanceRefusedError(message)
    entries = _to_entries(tree)
    by_path = {e.path: e for e in entries}
    activate_entry = by_path.get(_ACTIVATE_RELPATH)
    if activate_entry is None or activate_entry.sha256 != hashlib.sha256(activate_sh.encode()).hexdigest():
        message = "activate.sh in the cache does not match the daemon-rendered text"
        raise ProvenanceRefusedError(message)
    return ProvenanceManifest(
        slug=cache_dir.name,
        symphony=symphony,
        cache_key=cache_key,
        frozen_at=datetime.now(UTC),
        activate_sh=activate_sh,
        services_start_sh=_read_script(cache_dir, _START_RELPATH, by_path, mounts),
        services_health_sh=_read_script(cache_dir, _HEALTH_RELPATH, by_path, mounts),
        entries=tuple(sorted(entries, key=lambda e: e.path)),
    )


def _canonical(manifest: ProvenanceManifest) -> bytes:
    """The saved form: metadata, then one JSON array per entry. Compact, and canonical for the digest."""
    doc = {
        "v": _MANIFEST_FORMAT,
        "slug": manifest.slug,
        "symphony": manifest.symphony,
        "cache_key": manifest.cache_key,
        "frozen_at": manifest.frozen_at.isoformat(),
        "activate_sh": manifest.activate_sh,
        "services_start_sh": manifest.services_start_sh,
        "services_health_sh": manifest.services_health_sh,
        "entries": [list(e) for e in manifest.entries],
    }
    return json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()


_MANIFEST_FORMAT = 2


def _parse_manifest(data: bytes) -> ProvenanceManifest | None:
    """The manifest from its saved form, or None for anything malformed. Rows are checked for shape, not re-validated."""
    try:
        doc = json.loads(data)
        if doc.get("v") != _MANIFEST_FORMAT:
            return None
        entries = tuple(ProvenanceEntry(*row) for row in doc["entries"])
        if any(e.kind not in _ENTRY_KINDS for e in entries):
            return None
        return ProvenanceManifest(
            slug=str(doc["slug"]),
            symphony=str(doc["symphony"]),
            cache_key=str(doc["cache_key"]),
            frozen_at=datetime.fromisoformat(doc["frozen_at"]),
            activate_sh=str(doc["activate_sh"]),
            services_start_sh=doc["services_start_sh"],
            services_health_sh=doc["services_health_sh"],
            entries=entries,
        )
    except (ValueError, TypeError, KeyError):
        return None


def _inside_any(path: Path, mounts: tuple[Path, ...] | list[Path]) -> bool:
    resolved = path.resolve()
    return any(resolved == m.resolve() or m.resolve() in resolved.parents for m in mounts)


def save_manifest(
    path: Path,
    manifest: ProvenanceManifest,
    *,
    performer_mounts: tuple[Path, ...] | list[Path],
    max_bytes: int,
) -> str:
    """Write the manifest to the daemon-owned file and return its digest. Refused inside a performer mount.

    *max_bytes* is :func:`manifest_cap_bytes` of the configured entry bound. Over it, the refusal names the knob.
    """
    if _inside_any(path, performer_mounts):
        message = f"{path} is inside a performer mount; the frozen manifest must be daemon-owned"
        raise ProvenanceRefusedError(message)
    data = _canonical(manifest)
    if len(data) > max_bytes:
        message = (
            f"the frozen manifest is {len(data)} bytes, over the {max_bytes} bytes its entry bound allows; "
            "raise env_cache.provenance_max_entries if this cache is legitimately this large"
        )
        raise ProvenanceRefusedError(message)
    anchored_mkdir(path.parent, mounts=performer_mounts)
    replace_mounted_file(path, data, mounts=performer_mounts)
    return hashlib.sha256(data).hexdigest()


def load_manifest(
    path: Path,
    *,
    digest: str,
    performer_mounts: tuple[Path, ...] | list[Path],
    max_bytes: int,
) -> ProvenanceManifest | None:
    """The saved manifest, but only when it hashes to *digest*. Anything else is None (not trusted)."""
    if _inside_any(path, performer_mounts):
        logger.error("env_cache.provenance_refused", path=str(path), reason="inside a performer mount")
        return None
    data = read_mounted_file(path, max_bytes=max_bytes, mounts=performer_mounts)
    if data is None:
        return None
    if hashlib.sha256(data).hexdigest() != digest:
        logger.error("env_cache.provenance_digest_mismatch", path=str(path))
        return None
    return _parse_manifest(data)


def _changed(rel: str, exp: ProvenanceEntry, cur: TreeEntry) -> str | None:
    """The reason one entry differs from the manifest, judged without hashing, or None."""
    if exp.kind != cur.kind:
        return f"changed {rel} (kind)"
    if exp.mode != cur.mode:
        return f"changed {rel} (mode)"
    if exp.kind == "link" and exp.target != cur.target:
        return f"changed link {rel}"
    return None


def _differences(expected: dict[str, ProvenanceEntry], current: dict[str, TreeEntry]) -> list[str]:
    """Structural differences between the manifest and the walked cache (no hashing)."""
    reasons = [f"removed {rel}" for rel in sorted(expected.keys() - current.keys())]
    reasons += [f"added {rel}" for rel in sorted(current.keys() - expected.keys())]
    reasons += [
        reason
        for rel in sorted(expected.keys() & current.keys())
        if (reason := _changed(rel, expected[rel], current[rel])) is not None
    ]
    return reasons


def _file_stats(tree: list[TreeEntry]) -> Stats:
    return {t.rel: (t.size, t.ino, t.ctime_ns) for t in tree if t.kind == "file"}


def _touched(expected: dict[str, ProvenanceEntry], current: dict[str, TreeEntry], baseline: Stats) -> bool:
    """True when a file's size, inode or ctime differs from its stat baseline (the stat pass found a candidate)."""
    for rel, exp in expected.items():
        if exp.kind != "file":
            continue
        cur = current[rel]
        if baseline.get(rel, (exp.size, exp.ino, exp.ctime_ns)) != (cur.size, cur.ino, cur.ctime_ns):
            return True
    return False


def _walk_or_reason(
    cache_dir: Path,
    mounts: tuple[Path, ...],
    *,
    hash_files: bool,
    limits: ProvenanceLimits,
) -> tuple[list[TreeEntry] | None, str | None]:
    """The walked tree, or None with the reason the walk refused (a bound, a link, a missing dir)."""
    try:
        tree = walk_mounted_tree(
            cache_dir,
            mounts=mounts,
            hash_files=hash_files,
            max_entries=limits.max_entries,
            max_bytes=limits.max_bytes,
        )
    except FileNotFoundError:
        return None, "cache directory is missing"
    except MountedFileRefusedError as exc:
        return None, _bound_message(exc)
    return tree, None


def check_cache(
    cache_dir: Path,
    manifest: ProvenanceManifest,
    *,
    full: bool,
    scope: ProvenanceScope,
    baseline: Stats | None = None,
) -> ProvenanceCheck:
    """Compare the cache with the frozen manifest. ``full`` forces a re-hash of every file.

    The first pass is stat-only. It compares kinds, modes and link targets exactly, and each file's size, inode
    and ``ctime_ns`` against the stat *baseline* (the manifest's own values when no baseline is given). A
    structural difference is a refusal at once. A stat mismatch, or *full*, re-hashes every file and compares
    its sha256. A passing hash pass returns the stats it observed, which the caller keeps as the next baseline.
    """
    mounts = _mounts_for(cache_dir, scope.mounts)
    limits = scope.limits
    stat_tree, reason = _walk_or_reason(cache_dir, tuple(mounts), hash_files=False, limits=limits)
    if stat_tree is None:
        return ProvenanceCheck(False, reason)
    expected = {e.path: e for e in manifest.entries}
    current = {t.rel: t for t in stat_tree}
    structural = _differences(expected, current)
    if structural:
        return ProvenanceCheck(False, _reason(structural))
    if not full and not _touched(expected, current, baseline or {}):
        return ProvenanceCheck(True, None, hashed=False)
    hashed_tree, reason = _walk_or_reason(cache_dir, tuple(mounts), hash_files=True, limits=limits)
    if hashed_tree is None:
        return ProvenanceCheck(False, reason, hashed=True)
    digests = {t.rel: t.sha256 for t in hashed_tree}
    changed = [
        f"changed {rel}"
        for rel, exp in sorted(expected.items())
        if exp.kind == "file" and digests.get(rel) != exp.sha256
    ]
    if changed:
        return ProvenanceCheck(False, _reason(changed), hashed=True)
    return ProvenanceCheck(True, None, hashed=True, stats=_file_stats(hashed_tree))


def _reason(parts: list[str]) -> str:
    shown = parts[:_REASON_LIMIT]
    more = len(parts) - len(shown)
    text = ", ".join(shown)
    return text + (f" (+{more} more)" if more > 0 else "")


def build_payload(manifest: ProvenanceManifest, *, cache_path: str, digest: str) -> dict[str, Any]:
    """The block a consumer receives: the frozen steps and the frozen debs. Nothing is read from the cache.

    *digest* is the digest the snapshot holds for this manifest, passed in rather than recomputed, because the
    manifest can hold a million entries and re-serialising it on every dispatch is the cost this avoids.
    """
    return {
        "slug": provenance_slug(manifest.slug),
        "cache_path": cache_path,
        "activate_sh": manifest.activate_sh,
        "services_start_sh": manifest.services_start_sh,
        "services_health_sh": manifest.services_health_sh,
        "debs": manifest.debs,
        "digest": digest,
    }


def image_capability_refusal(*, provenance_sent: bool, performer_version: int) -> str | None:
    """The refusal reason when a frozen payload would reach an image that cannot take it, or None."""
    if provenance_sent and performer_version < PROVENANCE_CAPABILITY_VERSION:
        return OLD_IMAGE_REASON
    return None


__all__ = [
    "DEFAULT_LIMITS",
    "OLD_IMAGE_REASON",
    "PROVENANCE_CAPABILITY_VERSION",
    "PROVENANCE_DIR_NAME",
    "PROVENANCE_FULL_RECHECK_S",
    "PROVENANCE_MAX_SCRIPT_BYTES",
    "ProvenanceCheck",
    "ProvenanceEntry",
    "ProvenanceLimits",
    "ProvenanceManifest",
    "ProvenanceRefusedError",
    "ProvenanceScope",
    "build_payload",
    "check_cache",
    "freeze_cache",
    "image_capability_refusal",
    "load_manifest",
    "provenance_slug",
    "save_manifest",
]
