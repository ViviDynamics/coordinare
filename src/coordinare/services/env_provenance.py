"""Spec 234: the daemon freezes a verified env cache and checks it before every consumer dispatch.

The bootstrap performer writes the cache. After the daemon verifies it, consumers must run only what the
daemon verified. This module holds the mechanism:

- :func:`freeze_cache` walks the cache (anchored, see ``mounted_file.walk_mounted_tree``) and returns a
  :class:`ProvenanceManifest`: every entry with its kind, mode, size, inode, ``ctime_ns`` and sha256, the
  link targets, and the rendered scripts the consumers will run.
- :func:`save_manifest` and :func:`load_manifest` keep it in a daemon-owned file. A load trusts the file only
  when it hashes to the digest the snapshot holds.
- :func:`check_cache` compares the cache with the manifest. The cheap pass is stat-only. Any mismatch, and
  the periodic full pass, re-hash every file.
- :func:`build_payload` is the block a consumer receives: the frozen steps and the debs, never the cache's files.

Nothing here runs a file from the cache. The module reads bytes and compares them.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any, Literal

import structlog
from coordinare_service_inference.manual_override import SERVICES_SUBDIR
from pydantic import BaseModel

from coordinare.services.mounted_file import (
    MountedFileRefusedError,
    TreeEntry,
    anchored_mkdir,
    read_mounted_file,
    replace_mounted_file,
    walk_mounted_tree,
)

logger = structlog.get_logger(__name__)

# 234: bounds. A cache over a bound is refused with its reason, never checked partially.
PROVENANCE_MAX_ENTRIES = 100_000
PROVENANCE_MAX_BYTES = 2 * 1024 * 1024 * 1024
PROVENANCE_MAX_SCRIPT_BYTES = 256 * 1024
PROVENANCE_MAX_MANIFEST_BYTES = 64 * 1024 * 1024

# 234: the periodic full re-hash. The stat pass cannot see a rewrite that restores size, inode and ctime
# inside the filesystem's timestamp granularity, so a full pass runs at least this often.
PROVENANCE_FULL_RECHECK_S = 3600.0

# 234: the performer reports this on GET /status. An image without it cannot take a frozen payload.
PROVENANCE_CAPABILITY_VERSION = 1
OLD_IMAGE_REASON = "performer image too old for env-cache provenance (#234)"

# 234: the daemon-owned directory under the state directory, outside every performer mount.
PROVENANCE_DIR_NAME = "env-provenance"

_START_RELPATH = f"{SERVICES_SUBDIR}/services-start.sh"
_HEALTH_RELPATH = f"{SERVICES_SUBDIR}/services-health.sh"
_ACTIVATE_RELPATH = "activate.sh"
_DEBS_PREFIX = "debs/"
_REASON_LIMIT = 10


class ProvenanceRefusedError(Exception):
    """The cache cannot be frozen. The message says why, for the bootstrap failure and the log."""


class ProvenanceEntry(BaseModel, frozen=True):
    path: str
    kind: Literal["file", "dir", "link"]
    mode: int
    size: int
    ino: int
    ctime_ns: int
    sha256: str | None = None
    target: str | None = None


class ProvenanceManifest(BaseModel):
    slug: str
    symphony: str
    cache_key: str
    frozen_at: datetime
    activate_sh: str
    services_start_sh: str | None = None
    services_health_sh: str | None = None
    entries: list[ProvenanceEntry]

    @property
    def debs(self) -> dict[str, str]:
        """The debs the manifest froze, by cache-relative path, with their sha256."""
        return {
            e.path: e.sha256 or ""
            for e in self.entries
            if e.kind == "file" and e.path.startswith(_DEBS_PREFIX) and e.path.endswith(".deb")
        }


@dataclass(frozen=True)
class ProvenanceCheck:
    ok: bool
    reason: str | None = None
    hashed: bool = False


def _mounts_for(cache_dir: Path, performer_mounts: tuple[Path, ...] | list[Path]) -> tuple[Path, ...]:
    """The performer mounts plus the cache's parent (the env-cache root), as the 219 rules require."""
    return (*performer_mounts, cache_dir.parent)


def _link_stays_inside(rel: str, target: str) -> bool:
    """True when a relative link, resolved from its own directory, stays inside the cache dir."""
    if not target or target.startswith("/"):
        return False
    parts = list(PurePosixPath(rel).parent.parts)
    for part in target.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            if not parts:
                return False
            parts.pop()
        else:
            parts.append(part)
    return True


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


def freeze_cache(
    cache_dir: Path,
    *,
    symphony: str,
    cache_key: str,
    activate_sh: str,
    performer_mounts: tuple[Path, ...] | list[Path],
) -> ProvenanceManifest:
    """Walk the verified cache and return its manifest. Raises :class:`ProvenanceRefusedError` on any refusal."""
    if len(activate_sh.encode()) > PROVENANCE_MAX_SCRIPT_BYTES:
        message = f"the rendered activate.sh exceeds {PROVENANCE_MAX_SCRIPT_BYTES} bytes"
        raise ProvenanceRefusedError(message)
    mounts = _mounts_for(cache_dir, performer_mounts)
    try:
        tree = walk_mounted_tree(
            cache_dir,
            mounts=mounts,
            hash_files=True,
            max_entries=PROVENANCE_MAX_ENTRIES,
            max_bytes=PROVENANCE_MAX_BYTES,
        )
    except MountedFileRefusedError as exc:
        raise ProvenanceRefusedError(f"cache {exc}") from exc
    for entry in tree:
        if entry.kind == "link" and not _link_stays_inside(entry.rel, entry.target or ""):
            message = f"link {entry.rel} points outside the cache (target {entry.target!r})"
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
        entries=sorted(entries, key=lambda e: e.path),
    )


def _canonical(manifest: ProvenanceManifest) -> bytes:
    return json.dumps(manifest.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()


def manifest_digest(manifest: ProvenanceManifest) -> str:
    """The digest the snapshot records. It is the sha256 of the canonical JSON the file holds."""
    return hashlib.sha256(_canonical(manifest)).hexdigest()


def _inside_any(path: Path, mounts: tuple[Path, ...] | list[Path]) -> bool:
    resolved = path.resolve()
    return any(resolved == m.resolve() or m.resolve() in resolved.parents for m in mounts)


def save_manifest(
    path: Path,
    manifest: ProvenanceManifest,
    *,
    performer_mounts: tuple[Path, ...] | list[Path],
) -> str:
    """Write the manifest to the daemon-owned file and return its digest. Refused inside a performer mount."""
    if _inside_any(path, performer_mounts):
        message = f"{path} is inside a performer mount; the frozen manifest must be daemon-owned"
        raise ProvenanceRefusedError(message)
    data = _canonical(manifest)
    if len(data) > PROVENANCE_MAX_MANIFEST_BYTES:
        message = f"manifest exceeds {PROVENANCE_MAX_MANIFEST_BYTES} bytes"
        raise ProvenanceRefusedError(message)
    anchored_mkdir(path.parent, mounts=performer_mounts)
    replace_mounted_file(path, data, mounts=performer_mounts)
    return hashlib.sha256(data).hexdigest()


def load_manifest(
    path: Path,
    *,
    digest: str,
    performer_mounts: tuple[Path, ...] | list[Path],
) -> ProvenanceManifest | None:
    """The saved manifest, but only when it hashes to *digest*. Anything else is None (not trusted)."""
    if _inside_any(path, performer_mounts):
        logger.error("env_cache.provenance_refused", path=str(path), reason="inside a performer mount")
        return None
    data = read_mounted_file(path, max_bytes=PROVENANCE_MAX_MANIFEST_BYTES, mounts=performer_mounts)
    if data is None:
        return None
    if hashlib.sha256(data).hexdigest() != digest:
        logger.error("env_cache.provenance_digest_mismatch", path=str(path))
        return None
    try:
        return ProvenanceManifest.model_validate_json(data)
    except ValueError:
        return None


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


def _touched(expected: dict[str, ProvenanceEntry], current: dict[str, TreeEntry]) -> bool:
    """True when a file's size, inode or ctime differs from the manifest (the stat pass found a candidate)."""
    return any(
        exp.kind == "file"
        and (exp.size, exp.ino, exp.ctime_ns) != (current[rel].size, current[rel].ino, current[rel].ctime_ns)
        for rel, exp in expected.items()
    )


def _walk_or_reason(
    cache_dir: Path,
    mounts: tuple[Path, ...],
    *,
    hash_files: bool,
) -> tuple[list[TreeEntry] | None, str | None]:
    """The walked tree, or None with the reason the walk refused (a bound, a link, a missing dir)."""
    try:
        tree = walk_mounted_tree(
            cache_dir,
            mounts=mounts,
            hash_files=hash_files,
            max_entries=PROVENANCE_MAX_ENTRIES,
            max_bytes=PROVENANCE_MAX_BYTES,
        )
    except FileNotFoundError:
        return None, "cache directory is missing"
    except MountedFileRefusedError as exc:
        return None, f"cache {exc}"
    return tree, None


def check_cache(
    cache_dir: Path,
    manifest: ProvenanceManifest,
    *,
    full: bool,
    performer_mounts: tuple[Path, ...] | list[Path],
) -> ProvenanceCheck:
    """Compare the cache with the frozen manifest. ``full`` forces a re-hash of every file.

    The first pass is stat-only. It compares kinds, modes, link targets, and for each file its size, inode and
    ``ctime_ns``. Any stat mismatch, any structural difference or *full* triggers the re-hash pass, which
    compares every file's sha256. A structural difference is a refusal at once, with the paths named.
    """
    mounts = _mounts_for(cache_dir, performer_mounts)
    stat_tree, reason = _walk_or_reason(cache_dir, tuple(mounts), hash_files=False)
    if stat_tree is None:
        return ProvenanceCheck(False, reason)
    expected = {e.path: e for e in manifest.entries}
    current = {t.rel: t for t in stat_tree}
    structural = _differences(expected, current)
    if structural:
        return ProvenanceCheck(False, _reason(structural))
    if not full and not _touched(expected, current):
        return ProvenanceCheck(True, None, hashed=False)
    hashed_tree, reason = _walk_or_reason(cache_dir, tuple(mounts), hash_files=True)
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
    return ProvenanceCheck(True, None, hashed=True)


def _reason(parts: list[str]) -> str:
    shown = parts[:_REASON_LIMIT]
    more = len(parts) - len(shown)
    text = ", ".join(shown)
    return text + (f" (+{more} more)" if more > 0 else "")


def build_payload(manifest: ProvenanceManifest, *, cache_path: str) -> dict[str, Any]:
    """The block a consumer receives: the frozen steps and the frozen debs. Nothing is read from the cache."""
    return {
        "slug": manifest.slug,
        "cache_path": cache_path,
        "activate_sh": manifest.activate_sh,
        "services_start_sh": manifest.services_start_sh,
        "services_health_sh": manifest.services_health_sh,
        "debs": manifest.debs,
        "digest": manifest_digest(manifest),
    }


def image_capability_refusal(*, provenance_sent: bool, performer_version: int) -> str | None:
    """The refusal reason when a frozen payload would reach an image that cannot take it, or None."""
    if provenance_sent and performer_version < PROVENANCE_CAPABILITY_VERSION:
        return OLD_IMAGE_REASON
    return None


__all__ = [
    "OLD_IMAGE_REASON",
    "PROVENANCE_CAPABILITY_VERSION",
    "PROVENANCE_DIR_NAME",
    "PROVENANCE_FULL_RECHECK_S",
    "PROVENANCE_MAX_BYTES",
    "PROVENANCE_MAX_ENTRIES",
    "PROVENANCE_MAX_MANIFEST_BYTES",
    "PROVENANCE_MAX_SCRIPT_BYTES",
    "ProvenanceCheck",
    "ProvenanceEntry",
    "ProvenanceManifest",
    "ProvenanceRefusedError",
    "build_payload",
    "check_cache",
    "freeze_cache",
    "image_capability_refusal",
    "load_manifest",
    "manifest_digest",
    "save_manifest",
]
