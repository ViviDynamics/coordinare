"""Spec 234: the daemon freezes a verified env cache and checks it before every consumer dispatch.

The bootstrap performer writes the cache. After the daemon verifies it, consumers must run only what the daemon
verified. Nothing here holds the manifest in memory for the life of the daemon (234 review 3):

- :func:`freeze_cache` walks the cache (anchored, streamed) and writes the manifest file: a metadata line, then one
  row per entry in walk order. It returns the file's digest and the cache's stat digest.
- :func:`open_manifest` verifies the file against its recorded digest, one streamed pass, and reads the metadata line.
- :func:`stat_digest_of` walks the cache, streams a stat digest, and holds O(1) memory. A dispatch compares it with the
  digest recorded at the last verified freeze or full pass.
- :func:`verify_content` streams the manifest rows beside the walk and compares every entry, content included. It runs
  on a stat mismatch, on the first dispatch after a restart, and in the background hourly pass.

The stat digest is an additive hash: the sum, modulo 2**256, of sha256 over each record. A record is
``(path, type, mode, size, inode, ctime_ns, link target)``. The sum does not depend on order, so it needs no sort and
no list, which keeps it O(1) in memory.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import IO, TYPE_CHECKING, Any, NamedTuple

if TYPE_CHECKING:
    from collections.abc import Iterator

import structlog
from coordinare_service_inference.manual_override import SERVICES_SUBDIR

from coordinare.services.mounted_file import (
    MountedFileRefusedError,
    TreeBoundError,
    TreeEntry,
    anchored_mkdir,
    iter_mounted_tree,
    open_mounted_file,
    read_mounted_file,
    replace_mounted_file,
)

logger = structlog.get_logger(__name__)

_GIB = 1024 * 1024 * 1024


@dataclass(frozen=True)
class ProvenanceLimits:
    """The bounds for one cache walk. A cache over a bound is refused, naming the config knob."""

    max_entries: int
    max_bytes: int


# 234: bounds sized for real toolchains (a Node install is about 40k entries and 470 MiB; an rbenv about 480k entries
# and 7.7 GB). The config knobs default to these. A refusal names the knob that was hit.
DEFAULT_LIMITS = ProvenanceLimits(max_entries=1_000_000, max_bytes=16 * _GIB)

PROVENANCE_MAX_SCRIPT_BYTES = 256 * 1024

# 234 review 2: the manifest is bounded by the entry bound. A path is at most PROVENANCE_MAX_PATH_BYTES, and each row
# has a fixed JSON overhead. The cap is their product, so a cache at the default bound fits.
PROVENANCE_MAX_PATH_BYTES = 512
_MANIFEST_OVERHEAD_BYTES_PER_ENTRY = 256
# One line of the manifest: the metadata line carries the rendered scripts, so it is larger than a row.
_MAX_LINE_BYTES = 4 * 1024 * 1024

# 234: the periodic full re-hash, run in the background (review 2). The stat digest cannot see a rewrite that restores
# size, inode and ctime inside the filesystem's timestamp granularity, so a full pass runs at least this often.
PROVENANCE_FULL_RECHECK_S = 3600.0

# 234: the performer reports this on GET /status. An image without it cannot take a frozen payload.
PROVENANCE_CAPABILITY_VERSION = 1
OLD_IMAGE_REASON = "performer image too old for env-cache provenance (#234)"

# 234: the daemon-owned directory under the state directory, outside every performer mount.
PROVENANCE_DIR_NAME = "env-provenance"

# 234: the manifest file format. Version 3 is metadata then rows, streamed; older versions were never shipped.
MANIFEST_FORMAT = 3

# 234: a slug names the frozen directory on the performer side. The performer implements the same function.
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
_ENTRY_KINDS = frozenset({"file", "dir", "link"})
_READ_CHUNK_BYTES = 1024 * 1024

_KNOB_FOR_BOUND = {
    "entries": "env_cache.provenance_max_entries",
    "bytes": "env_cache.provenance_max_bytes",
}


class ProvenanceRefusedError(Exception):
    """The cache cannot be frozen or checked. The message says why, for the bootstrap failure and the log."""


def manifest_cap_bytes(max_entries: int) -> int:
    """The largest manifest file accepted for a cache of *max_entries* entries (234 review 2)."""
    return max_entries * (PROVENANCE_MAX_PATH_BYTES + _MANIFEST_OVERHEAD_BYTES_PER_ENTRY)


def provenance_slug(cache_dir_name: str) -> str:
    """The performer-side directory name for a cache directory name (234).

    A name that already matches the slug pattern and the length bound is kept. Any other name is cleaned to the
    pattern, truncated, and suffixed with a short hash of the original, so two long names never collide. The
    performer's ``provenance_slug`` returns the same string for every input.
    """
    if len(cache_dir_name) <= PROVENANCE_SLUG_MAX and _SLUG_VALID.fullmatch(cache_dir_name):
        return cache_dir_name
    cleaned = _SLUG_INVALID.sub("-", cache_dir_name.lower()).lstrip("-") or "symphony"
    head = cleaned[: PROVENANCE_SLUG_MAX - PROVENANCE_SLUG_HASH_CHARS - 1]
    digest = hashlib.sha256(cache_dir_name.encode()).hexdigest()[:PROVENANCE_SLUG_HASH_CHARS]
    return f"{head}-{digest}"


class ProvenanceEntry(NamedTuple):
    """One manifest row. A named tuple: streamed, never a model per entry."""

    path: str
    kind: str  # "file", "dir" or "link"
    mode: int
    size: int
    ino: int
    ctime_ns: int
    sha256: str | None = None
    target: str | None = None


@dataclass(frozen=True)
class ProvenanceMeta:
    """The metadata line of the manifest: everything but the rows. Small, so it is what a payload is built from."""

    slug: str
    symphony: str
    cache_key: str
    frozen_at: str
    activate_sh: str
    services_start_sh: str | None
    services_health_sh: str | None
    debs: dict[str, str]


@dataclass(frozen=True)
class ManifestFile:
    """A manifest on disk whose digest has been verified. It holds the path and the metadata, never the rows."""

    path: Path
    digest: str
    meta: ProvenanceMeta


@dataclass(frozen=True)
class FreezeResult:
    manifest: ManifestFile
    stat_digest: str
    entries: int


@dataclass(frozen=True)
class ContentResult:
    ok: bool
    reason: str | None = None
    stat_digest: str | None = None


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


def _mounts_for(cache_dir: Path, performer_mounts: tuple[Path, ...] | list[Path]) -> tuple[Path, ...]:
    """The performer mounts plus the cache's parent (the env-cache root), as the 219 rules require."""
    return (*performer_mounts, cache_dir.parent)


def _bound_message(exc: MountedFileRefusedError) -> str:
    """A refusal from the walk. A bound names the knob for its kind (structured, never from the text)."""
    if isinstance(exc, TreeBoundError):
        return f"cache {exc}; raise {_KNOB_FOR_BOUND[exc.bound]} if this cache is legitimately this large"
    return f"cache {exc}"


def _link_refusal(rel: str, target: str, links: dict[str, str]) -> str | None:
    """Why a link leaves the cache or cannot be resolved, or None when it stays inside.

    Each component of the target is resolved in turn. A component that names another link in the cache is followed
    through that link's own target, so a chain of links cannot reach outside by way of a ``..`` that only the kernel
    would apply. The hop count bounds the chain.
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


class _StatDigest:
    """The stat digest: one sha256 over the records in walk order. O(1) memory.

    The walk is deterministic (each directory's names are sorted, depth first), so the records arrive in the same order
    every time and need no sort. Each record is seven NUL-terminated fields. A POSIX name and a link target cannot
    contain NUL, and every record has the same field count, so the byte stream parses back into exactly these records.
    Reordering, swapping or dropping a record changes the digest.
    """

    def __init__(self) -> None:
        self._hash = hashlib.sha256()

    def add(self, entry: TreeEntry) -> None:
        fields = (
            entry.rel,
            entry.kind,
            str(entry.mode),
            str(entry.size),
            str(entry.ino),
            str(entry.ctime_ns),
            entry.target or "",
        )
        self._hash.update("".join(f"{field}\0" for field in fields).encode("utf-8", "surrogateescape"))

    def hexdigest(self) -> str:
        return self._hash.hexdigest()


def _row_bytes(entry: TreeEntry) -> bytes:
    row = [entry.rel, entry.kind, entry.mode, entry.size, entry.ino, entry.ctime_ns, entry.sha256, entry.target]
    return (json.dumps(row, separators=(",", ":")) + "\n").encode("utf-8", "surrogateescape")


def _read_script(cache_dir: Path, rel: str, hashes: dict[str, str | None], mounts: tuple[Path, ...]) -> str | None:
    """The frozen bytes of one script, or None when the cache has no such file. Refused on a mismatch."""
    if hashes.get(rel) is None:
        return None
    data = read_mounted_file(cache_dir / rel, max_bytes=PROVENANCE_MAX_SCRIPT_BYTES, mounts=mounts)
    if data is None:
        message = f"{rel} is missing, linked, or larger than {PROVENANCE_MAX_SCRIPT_BYTES} bytes"
        raise ProvenanceRefusedError(message)
    if hashlib.sha256(data).hexdigest() != hashes[rel]:
        message = f"{rel} changed while the cache was frozen"
        raise ProvenanceRefusedError(message)
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        message = f"{rel} is not UTF-8"
        raise ProvenanceRefusedError(message) from exc


def _write_manifest(out_path: Path, meta_line: bytes, spool: IO[bytes], max_bytes: int) -> str:
    """Write the metadata line, then the spooled rows, to *out_path* atomically. Returns the file's sha256."""
    anchored_mkdir(out_path.parent, mounts=())
    spool_size = spool.seek(0, os.SEEK_END)
    if len(meta_line) + spool_size > max_bytes:
        message = (
            f"the frozen manifest is over the {max_bytes} bytes its entry bound allows; "
            "raise env_cache.provenance_max_entries if this cache is legitimately this large"
        )
        raise ProvenanceRefusedError(message)
    digest = hashlib.sha256()
    fd, tmp_name = tempfile.mkstemp(dir=out_path.parent, prefix=".manifest-", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as out:
            for chunk in (meta_line, *_chunks(spool)):
                digest.update(chunk)
                out.write(chunk)
            out.flush()
            os.fsync(out.fileno())
        tmp.replace(out_path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return digest.hexdigest()


def _chunks(spool: IO[bytes]) -> Iterator[bytes]:
    spool.seek(0)
    while True:
        chunk = spool.read(_READ_CHUNK_BYTES)
        if not chunk:
            return
        yield chunk


def freeze_cache(
    cache_dir: Path,
    identity: tuple[str, str],
    activate_sh: str,
    scope: ProvenanceScope,
    out_path: Path,
) -> FreezeResult:
    """Walk the verified cache and write its manifest to *out_path*. Raises :class:`ProvenanceRefusedError` on any refusal.

    Memory is bounded by the link count (the links are checked after the walk) and the rendered scripts, not by the
    entry count: rows are spooled to a temporary file as they are walked.
    """
    symphony, cache_key = identity
    if len(activate_sh.encode()) > PROVENANCE_MAX_SCRIPT_BYTES:
        message = f"the rendered activate.sh exceeds {PROVENANCE_MAX_SCRIPT_BYTES} bytes"
        raise ProvenanceRefusedError(message)
    if _inside_any(out_path, scope.mounts):
        message = f"{out_path} is inside a performer mount; the frozen manifest must be daemon-owned"
        raise ProvenanceRefusedError(message)
    anchored_mkdir(out_path.parent, mounts=scope.mounts)
    mounts = _mounts_for(cache_dir, scope.mounts)
    stat = _StatDigest()
    links: dict[str, str] = {}
    debs: dict[str, str] = {}
    special: dict[str, str | None] = {_ACTIVATE_RELPATH: None, _START_RELPATH: None, _HEALTH_RELPATH: None}
    count = 0
    with tempfile.TemporaryFile(dir=out_path.parent) as spool:
        try:
            for entry in iter_mounted_tree(
                cache_dir,
                mounts=mounts,
                hash_files=True,
                max_entries=scope.limits.max_entries,
                max_bytes=scope.limits.max_bytes,
            ):
                count += 1
                _check_path_length(entry.rel)
                stat.add(entry)
                if entry.kind == "link":
                    links[entry.rel] = entry.target or ""
                if entry.kind == "file" and entry.rel.startswith(_DEBS_PREFIX) and entry.rel.endswith(".deb"):
                    debs[entry.rel] = entry.sha256 or ""
                if entry.rel in special:
                    special[entry.rel] = entry.sha256
                spool.write(_row_bytes(entry))
        except MountedFileRefusedError as exc:
            raise ProvenanceRefusedError(_bound_message(exc)) from exc
        for rel, target in links.items():
            reason = _link_refusal(rel, target, links)
            if reason is not None:
                message = f"link {rel} {reason} (target {target!r})"
                raise ProvenanceRefusedError(message)
        if special[_ACTIVATE_RELPATH] != hashlib.sha256(activate_sh.encode()).hexdigest():
            message = "activate.sh in the cache does not match the daemon-rendered text"
            raise ProvenanceRefusedError(message)
        meta = ProvenanceMeta(
            slug=cache_dir.name,
            symphony=symphony,
            cache_key=cache_key,
            frozen_at=datetime.now(UTC).isoformat(),
            activate_sh=activate_sh,
            services_start_sh=_read_script(cache_dir, _START_RELPATH, special, mounts),
            services_health_sh=_read_script(cache_dir, _HEALTH_RELPATH, special, mounts),
            debs=debs,
        )
        meta_line = (_meta_json(meta) + "\n").encode("utf-8")
        digest = _write_manifest(out_path, meta_line, spool, manifest_cap_bytes(scope.limits.max_entries))
    manifest = ManifestFile(path=out_path, digest=digest, meta=meta)
    return FreezeResult(manifest=manifest, stat_digest=stat.hexdigest(), entries=count)


def _check_path_length(rel: str) -> None:
    if len(rel.encode()) > PROVENANCE_MAX_PATH_BYTES:
        message = f"path longer than {PROVENANCE_MAX_PATH_BYTES} bytes cannot be frozen: {rel[:80]}..."
        raise ProvenanceRefusedError(message)


def _meta_json(meta: ProvenanceMeta) -> str:
    doc: dict[str, Any] = {
        "v": MANIFEST_FORMAT,
        "slug": meta.slug,
        "symphony": meta.symphony,
        "cache_key": meta.cache_key,
        "frozen_at": meta.frozen_at,
        "activate_sh": meta.activate_sh,
        "services_start_sh": meta.services_start_sh,
        "services_health_sh": meta.services_health_sh,
        "debs": meta.debs,
    }
    return json.dumps(doc, sort_keys=True, separators=(",", ":"))


def _parse_meta(line: bytes) -> ProvenanceMeta | None:
    try:
        doc = json.loads(line)
        if doc.get("v") != MANIFEST_FORMAT:
            return None
        return ProvenanceMeta(
            slug=str(doc["slug"]),
            symphony=str(doc["symphony"]),
            cache_key=str(doc["cache_key"]),
            frozen_at=str(doc["frozen_at"]),
            activate_sh=str(doc["activate_sh"]),
            services_start_sh=doc["services_start_sh"],
            services_health_sh=doc["services_health_sh"],
            debs={str(k): str(v) for k, v in doc["debs"].items()},
        )
    except (ValueError, TypeError, KeyError, AttributeError):
        return None


def _bounded_line(fh: IO[bytes]) -> bytes:
    line = fh.readline(_MAX_LINE_BYTES + 1)
    if len(line) > _MAX_LINE_BYTES:
        message = "a manifest line is over the line bound"
        raise ProvenanceRefusedError(message)
    return line


def open_manifest(
    path: Path,
    *,
    digest: str,
    performer_mounts: tuple[Path, ...] | list[Path],
    max_bytes: int,
) -> ManifestFile | None:
    """The manifest at *path*, but only when its bytes hash to *digest*. One streamed pass; O(1) memory. None otherwise."""
    if _inside_any(path, performer_mounts):
        logger.error("env_cache.provenance_refused", path=str(path), reason="inside a performer mount")
        return None
    fh = open_mounted_file(path, mounts=performer_mounts)
    if fh is None:
        return None
    with fh:
        if os.fstat(fh.fileno()).st_size > max_bytes:
            logger.error("env_cache.provenance_refused", path=str(path), reason="manifest over its cap")
            return None
        hasher = hashlib.sha256()
        first = _bounded_line(fh)
        hasher.update(first)
        while chunk := fh.read(_READ_CHUNK_BYTES):
            hasher.update(chunk)
    if hasher.hexdigest() != digest:
        logger.error("env_cache.provenance_digest_mismatch", path=str(path))
        return None
    meta = _parse_meta(first)
    if meta is None:
        return None
    return ManifestFile(path=path, digest=digest, meta=meta)


class _RowStream:
    """The rows of a manifest, one at a time, with the file's digest checked as they are read (O(1) memory)."""

    def __init__(self, manifest: ManifestFile, performer_mounts: tuple[Path, ...]) -> None:
        fh = open_mounted_file(manifest.path, mounts=performer_mounts)
        if fh is None:
            message = "the frozen manifest could not be read"
            raise ProvenanceRefusedError(message)
        self._fh: IO[bytes] | None = fh
        self._hasher = hashlib.sha256()
        self._expected = manifest.digest
        self._hasher.update(_bounded_line(fh))

    def __iter__(self) -> _RowStream:
        return self

    def __next__(self) -> ProvenanceEntry:
        if self._fh is None:
            raise StopIteration
        line = _bounded_line(self._fh)
        if not line:
            self._finish()
            raise StopIteration
        self._hasher.update(line)
        try:
            row = json.loads(line)
            entry = ProvenanceEntry(*row)
        except (ValueError, TypeError) as exc:
            message = "a manifest row is malformed"
            raise ProvenanceRefusedError(message) from exc
        if entry.kind not in _ENTRY_KINDS:
            message = "a manifest row has an unknown kind"
            raise ProvenanceRefusedError(message)
        return entry

    def _finish(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None
        if self._hasher.hexdigest() != self._expected:
            message = "the frozen manifest changed while it was read"
            raise ProvenanceRefusedError(message)

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None


def verify_content(cache_dir: Path, manifest: ManifestFile, scope: ProvenanceScope) -> ContentResult:
    """Compare the cache with the manifest, entry by entry, content included (O(1) memory).

    The manifest rows and the walk are both in walk order, so they are compared as two streams. A structural difference,
    a changed hash, or a manifest that changed while it was read, fails the check. On success the walk's stat digest is
    returned, to be recorded as the new baseline.
    """
    mounts = _mounts_for(cache_dir, scope.mounts)
    rows = _RowStream(manifest, mounts)
    stat = _StatDigest()
    walk = iter_mounted_tree(
        cache_dir,
        mounts=mounts,
        hash_files=True,
        max_entries=scope.limits.max_entries,
        max_bytes=scope.limits.max_bytes,
    )
    try:
        while True:
            try:
                current = next(walk, None)
            except MountedFileRefusedError as exc:
                return ContentResult(False, _bound_message(exc))
            expected = next(rows, None)
            if current is None:
                if expected is None:
                    break
                return ContentResult(False, f"removed {expected.path}")
            if expected is None:
                return ContentResult(False, f"added {current.rel}")
            reason = _compare(expected, current)
            if reason is not None:
                return ContentResult(False, reason)
            stat.add(current)
    except ProvenanceRefusedError as exc:
        return ContentResult(False, str(exc))
    finally:
        rows.close()
        walk.close()
    return ContentResult(True, None, stat.hexdigest())


def content_pass(
    cache_dir: Path,
    manifest_path: Path,
    digest: str,
    performer_mounts: tuple[Path, ...],
    scope: ProvenanceScope,
) -> ContentResult:
    """The full content check as one call: open the manifest by its digest, then compare. Picklable arguments, so a
    worker process can run it (review round 4)."""
    manifest = open_manifest(
        manifest_path,
        digest=digest,
        performer_mounts=performer_mounts,
        max_bytes=manifest_cap_bytes(scope.limits.max_entries),
    )
    if manifest is None:
        return ContentResult(False, "frozen provenance manifest is missing or altered")
    try:
        return verify_content(cache_dir, manifest, scope)
    except ProvenanceRefusedError as exc:
        return ContentResult(False, str(exc))


def _compare(exp: ProvenanceEntry, cur: TreeEntry) -> str | None:
    """Why the walked entry differs from its manifest row, or None (path, then content)."""
    if cur.rel != exp.path:
        return f"structure changed at {cur.rel} (expected {exp.path})"
    return _changed(exp, cur)


def _changed(exp: ProvenanceEntry, cur: TreeEntry) -> str | None:
    """The reason one entry differs from its manifest row, or None when it matches (content included)."""
    if exp.kind != cur.kind:
        return f"changed {cur.rel} (kind)"
    if exp.mode != cur.mode:
        return f"changed {cur.rel} (mode)"
    if exp.kind == "link" and exp.target != cur.target:
        return f"changed link {cur.rel}"
    if exp.kind == "file" and exp.sha256 != cur.sha256:
        return f"changed {cur.rel}"
    return None


def stat_digest_of(cache_dir: Path, scope: ProvenanceScope) -> str:
    """The stat digest of the cache as it is now: one stat walk, O(1) memory (the dispatch fast path).

    Raises :class:`ProvenanceRefusedError` for a missing cache, a link or file the walk refuses, or a bound.
    """
    stat = _StatDigest()
    mounts = _mounts_for(cache_dir, scope.mounts)
    try:
        for entry in iter_mounted_tree(
            cache_dir,
            mounts=mounts,
            hash_files=False,
            max_entries=scope.limits.max_entries,
            max_bytes=scope.limits.max_bytes,
        ):
            stat.add(entry)
    except FileNotFoundError as exc:
        message = "cache directory is missing"
        raise ProvenanceRefusedError(message) from exc
    except MountedFileRefusedError as exc:
        raise ProvenanceRefusedError(_bound_message(exc)) from exc
    return stat.hexdigest()


def write_stat_sidecar(
    path: Path, *, manifest_digest: str, stat_digest: str, performer_mounts: tuple[Path, ...] | list[Path],
) -> None:
    """Record the stat digest beside the manifest, bound to its digest. Daemon-owned; refused inside a mount."""
    if _inside_any(path, performer_mounts):
        message = f"{path} is inside a performer mount; the stat record must be daemon-owned"
        raise ProvenanceRefusedError(message)
    anchored_mkdir(path.parent, mounts=performer_mounts)
    data = json.dumps({"manifest_digest": manifest_digest, "stat_digest": stat_digest}, sort_keys=True).encode()
    replace_mounted_file(path, data, mounts=performer_mounts)


def read_stat_sidecar(
    path: Path, *, manifest_digest: str, performer_mounts: tuple[Path, ...] | list[Path],
) -> str | None:
    """The recorded stat digest, but only for *manifest_digest*. None when missing, altered, or for another manifest."""
    if _inside_any(path, performer_mounts):
        return None
    data = read_mounted_file(path, max_bytes=4096, mounts=performer_mounts)
    if data is None:
        return None
    try:
        doc = json.loads(data)
    except ValueError:
        return None
    if not isinstance(doc, dict) or doc.get("manifest_digest") != manifest_digest:
        return None
    stat_digest = doc.get("stat_digest")
    return stat_digest if isinstance(stat_digest, str) else None


def _inside_any(path: Path, mounts: tuple[Path, ...] | list[Path]) -> bool:
    resolved = path.resolve()
    return any(resolved == m.resolve() or m.resolve() in resolved.parents for m in mounts)


def build_payload(meta: ProvenanceMeta, *, cache_path: str, digest: str) -> dict[str, Any]:
    """The payload a consumer receives: the frozen scripts, the frozen debs and the digest. Never the rows (234 review 3).

    The rows are the whole manifest and can be hundreds of MB at the default bound, so they stay on the daemon's disk.
    The performer materialises only what is here.
    """
    return {
        "slug": provenance_slug(meta.slug),
        "cache_path": cache_path,
        "activate_sh": meta.activate_sh,
        "services_start_sh": meta.services_start_sh,
        "services_health_sh": meta.services_health_sh,
        "debs": dict(meta.debs),
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
    "ContentResult",
    "FreezeResult",
    "ManifestFile",
    "ProvenanceCheck",
    "ProvenanceEntry",
    "ProvenanceLimits",
    "ProvenanceMeta",
    "ProvenanceRefusedError",
    "ProvenanceScope",
    "build_payload",
    "freeze_cache",
    "image_capability_refusal",
    "manifest_cap_bytes",
    "open_manifest",
    "provenance_slug",
    "read_stat_sidecar",
    "stat_digest_of",
    "verify_content",
    "write_stat_sidecar",
]
