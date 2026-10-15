"""Reads and writes of files the daemon takes from disk (spec 208).

A performer can write inside a performer mount (performer log dir, endpoint volume, env-cache
root, a clone). It can rename a directory there and plant links at any level. The daemon
decides where a path lands in two stages, and only the first one touches the operator's side:

1. **Operator side.** Every component of the path is resolved from ``/`` one at a time.
   A link is followed only while the walk is outside every performer mount. Operator links
   (system links such as macOS ``/tmp``, a directory alias, a config symlink) are followed
   normally, and the walk checks whether it has reached a mount.
2. **Inside a mount.** The walk stops at the outermost mount it reaches. That mount root is
   resolved from the operator's config (a path the performer has not yet touched) and is
   opened as a trusted start. Every component below it, the leaf included, is opened with
   ``O_NOFOLLOW`` relative to the directory descriptor before it, and ``..`` is refused.
   A link anywhere below a mount root is refused, including a link that sits inside a mount
   nested in another one, because the walk reaches the outer mount first.
   A directory is a mount when its ``(st_dev, st_ino)`` equals a root's, so a second path to the
   same directory (a bind mount, a firmlink) is a mount too (220). The roots are resolved once per
   call.

A path that never reaches a mount is an operator path. It is opened normally, links followed,
and only the file type and size are checked. A non-regular file, a FIFO (opened non-blocking,
then type-checked) and a file over the bound are still refused.

Clones and the env-cache root are mounts too: their contents are performer-authored. Callers
pass them as the mount list.

A refusal is logged and reads as "no file". The bytes are untrusted: callers redact them
before showing them and never execute or import them.
"""

from __future__ import annotations

import contextlib
import errno
import hashlib
import os
import stat
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import structlog

from coordinare.services.run_transcript import (
    _NOFOLLOW,
    _NONBLOCK,
    RootGuard,
    _case_insensitive,
    _write_all,
    comparable_part,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence
    from typing import BinaryIO

    from coordinare.config import ProjectConfiguration

logger = structlog.get_logger(__name__)

_CHUNK_BYTES = 64 * 1024
_DEFAULT_FILE_MODE = 0o644
_INHERITED_MODE_MASK = 0o755
_MAX_LINKS = 40


class MountedFileRefusedError(OSError):
    """A read or write the daemon will not perform on a performer-writable path."""


def performer_mount_hosts(config: ProjectConfiguration) -> list[Path]:
    """Every host directory a performer container can see (129, 208).

    The performer log dir is bind-mounted into ephemeral containers, each endpoint may declare
    more volumes, and the env-cache root holds per-symphony caches mounted into performers.
    Used by the transcript and run-log guards and by the file readers here.
    """
    hosts: list[Path] = []
    log_dir = getattr(config, "performer_log_dir", None)
    if log_dir is not None:
        hosts.append(Path(log_dir).expanduser())
    for endpoint in config.performer_endpoints:
        # Expanded the way the config validator expands it, so both agree on ``~`` volumes.
        hosts.extend(Path(volume.host_path).expanduser() for volume in endpoint.volumes)
    hosts.append(Path(config.env_cache_root).expanduser())
    return hosts


def transcript_root_guard(config: ProjectConfiguration | None, *, protected: Iterable[Path]) -> RootGuard:
    """The guard a transcripts root is checked against (129, 208, 242).

    Every host directory a performer can see, plus the directories the owner must not be
    swallowed by. The daemon and the bench both build their guard here, so they cannot
    disagree about what a performer can reach.
    """
    mounts = tuple(performer_mount_hosts(config)) if config is not None else ()
    return RootGuard(performer_mounts=mounts, protected=tuple(protected))


def loaded_performer_mounts(global_config: ProjectConfiguration | None) -> tuple[Path, ...]:
    """The performer mount list from the loaded config, or a clear refusal when there is none.

    Every env-cache read and write takes this list (219). A missing config is a wiring fault, so
    it raises rather than reading with a shorter list.
    """
    if global_config is None:
        msg = "no loaded config; cannot resolve the performer mounts for env-cache reads"
        raise RuntimeError(msg)
    return tuple(performer_mount_hosts(global_config))


@dataclass(frozen=True)
class _Walk:
    """A path inside a mount: open ``start`` (a trusted, resolved directory), then walk ``parts``."""

    start: Path
    parts: tuple[str, ...]


@dataclass(frozen=True)
class _MountRoot:
    """A mount root resolved once for one call (220): its path, comparable parts and identity."""

    path: Path
    key: tuple[str, ...]
    insensitive: bool
    identity: tuple[int, int] | None


def _comparable(parts: Sequence[str], *, insensitive: bool) -> tuple[str, ...]:
    return tuple(comparable_part(part, insensitive=insensitive) for part in parts)


def _parts_below(path: Path, base: Path) -> tuple[str, ...] | None:
    """The components of *path* below *base*, or None when *base* is not an ancestor of it.

    Compared case-insensitively where the filesystem that holds *base* is case-insensitive.
    """
    insensitive = _case_insensitive(base)
    depth = len(base.parts)
    if len(path.parts) < depth:
        return None
    head = _comparable(path.parts[:depth], insensitive=insensitive)
    return path.parts[depth:] if head == _comparable(base.parts, insensitive=insensitive) else None


def _lstat_or_none(path: Path) -> os.stat_result | None:
    try:
        return path.lstat()
    except OSError:
        return None


def _stat_or_none(path: Path) -> os.stat_result | None:
    try:
        return path.stat()
    except OSError:
        return None


def _resolve_roots(mounts: Sequence[Path]) -> tuple[_MountRoot, ...]:
    """Every mount root, resolved, case-probed and identified once for a call (220, item 3).

    The walk used to resolve every root at every path component. Resolving here, once, is the
    whole cost of the root side of a read.
    """
    roots: list[_MountRoot] = []
    for mount in mounts:
        resolved = mount.resolve()
        insensitive = _case_insensitive(resolved)
        info = _stat_or_none(resolved)
        roots.append(
            _MountRoot(
                path=resolved,
                key=_comparable(resolved.parts, insensitive=insensitive),
                insensitive=insensitive,
                identity=None if info is None else (info.st_dev, info.st_ino),
            ),
        )
    return tuple(roots)


def _mount_hit(
    current: Path,
    here: os.stat_result | None,
    roots: Sequence[_MountRoot],
) -> tuple[Path, tuple[str, ...]] | None:
    """Where the walk enters a mount at *current*, as ``(start, parts below start)``, or None.

    A string match (*current* is at or below a root's resolved path) and an identity match
    (*current* is the same directory, ``st_dev``/``st_ino``, as a root reached through a second
    path such as a bind mount or a firmlink) both count. The outermost match wins, so the walk
    always opens from the outermost mount it reaches.
    """
    parts = current.parts
    hits: list[tuple[int, Path, tuple[str, ...]]] = []
    keys: dict[bool, tuple[str, ...]] = {}
    for root in roots:
        comparable = keys.get(root.insensitive)
        if comparable is None:
            comparable = _comparable(parts, insensitive=root.insensitive)
            keys[root.insensitive] = comparable
        depth = len(root.key)
        if comparable[:depth] == root.key:
            hits.append((depth, root.path, parts[depth:]))
    if not hits and here is not None:
        identity = (here.st_dev, here.st_ino)
        if any(root.identity == identity for root in roots):
            hits.append((len(parts), current, ()))
    if not hits:
        return None
    _depth, start, inside = min(hits, key=lambda hit: hit[0])
    return start, inside


def _classify(path: Path, mounts: Sequence[Path]) -> tuple[_Walk | None, Path]:
    """Resolve *path* on the operator side. Returns ``(walk, resolved)``.

    ``walk`` is set when the path reaches below a performer mount, and is then the walk to take
    from the mount start. Otherwise ``resolved`` is the operator path to open. Links are followed
    only while outside every mount: a link found on the way into a mount is the operator's, and a
    link below a mount root is never followed here (it is left for the ``O_NOFOLLOW`` walk). A
    path that is exactly a mount root (a directory, or a single-file mount) is an operator path,
    as it was before 220. The mount roots are resolved once per call.
    """
    roots = _resolve_roots(mounts)
    absolute = path.absolute()
    current = Path(absolute.anchor)
    queue = list(absolute.parts[1:])
    here = _lstat_or_none(current)
    links = 0
    while True:
        hit = _mount_hit(current, here, roots)
        if hit is not None and hit[1] == () and not any(part not in ("", ".") for part in queue):
            # The path is the mount root itself: nothing below it to anchor, so it is opened
            # as the operator's path. A mount root cannot be replaced by a link from inside.
            return None, current
        if hit is not None:
            start, inside = hit
            rest = [part for part in queue if part not in ("", ".")]
            if ".." in rest:
                message = "'..' below a performer mount"
                raise MountedFileRefusedError(message)
            return _Walk(start=start, parts=(*inside, *rest)), current
        if not queue:
            return None, current
        part = queue.pop(0)
        if part in ("", "."):
            continue
        if part == "..":
            current = current.parent
            here = _lstat_or_none(current)
            continue
        candidate = current / part
        info = _lstat_or_none(candidate)
        if info is not None and stat.S_ISLNK(info.st_mode):
            links += 1
            if links > _MAX_LINKS:
                message = "too many links"
                raise MountedFileRefusedError(message)
            target = candidate.readlink()
            if target.is_absolute():
                current = Path(target.anchor)
                queue = [*target.parts[1:], *queue]
            else:
                queue = [*target.parts, *queue]
            here = _lstat_or_none(current)
            continue
        current = candidate
        here = info


def _open_dir(name: str, dir_fd: int) -> int:
    try:
        return os.open(name, os.O_RDONLY | os.O_DIRECTORY | _NOFOLLOW | _NONBLOCK, dir_fd=dir_fd)
    except FileNotFoundError:
        raise
    except OSError as exc:
        message = "a directory below a performer mount is a link or not a directory"
        raise MountedFileRefusedError(message) from exc


def _open_chain(walk: _Walk, *, create: bool) -> int:
    """Open the directory at the end of *walk* and return its descriptor (the caller closes it).

    Every component below the trusted start is opened with ``O_NOFOLLOW``. With *create*, a
    missing component is made first (mode 0777 less the umask, as ``Path.mkdir`` does), and the
    start is made with its parents. A link, a regular file or a FIFO where a directory is
    expected is refused without being followed.
    """
    if create:
        walk.start.mkdir(parents=True, exist_ok=True)
    fd = os.open(walk.start, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in walk.parts:
            if create:
                _make_child_dir(part, fd)
            nxt = _open_dir(part, fd)
            os.close(fd)
            fd = nxt
    except BaseException:
        os.close(fd)
        raise
    return fd


def _make_child_dir(name: str, dir_fd: int) -> None:
    """Create *name* under *dir_fd* unless it already exists. Whatever it is gets checked after."""
    try:
        os.mkdir(name, 0o777, dir_fd=dir_fd)
    except FileExistsError:
        return


def _open_walk(walk: _Walk) -> tuple[int, str]:
    """Open the directory holding the leaf, walking every component below the trusted start.

    Returns ``(dir_fd, leaf_name)``. The caller closes ``dir_fd``. Raises ``FileNotFoundError``
    for a missing directory, and :class:`MountedFileRefusedError` for a link on the way.
    """
    fd = os.open(walk.start, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in walk.parts[:-1]:
            nxt = _open_dir(part, fd)
            os.close(fd)
            fd = nxt
    except BaseException:
        os.close(fd)
        raise
    return fd, walk.parts[-1]


def _check_regular(info: os.stat_result, max_bytes: int | None) -> None:
    """A regular file, and within *max_bytes* when a bound is given (None streams unbounded)."""
    if not stat.S_ISREG(info.st_mode):
        message = "not a regular file"
        raise MountedFileRefusedError(message)
    if max_bytes is not None and info.st_size > max_bytes:
        message = f"larger than {max_bytes} bytes"
        raise MountedFileRefusedError(message)


def _check_anchored(info: os.stat_result, max_bytes: int | None) -> None:
    if info.st_nlink != 1:
        message = "hard linked"
        raise MountedFileRefusedError(message)
    _check_regular(info, max_bytes)


def _drain(fd: int, max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = os.read(fd, _CHUNK_BYTES)
        if not chunk:
            return b"".join(chunks)
        total += len(chunk)
        if total > max_bytes:
            message = f"larger than {max_bytes} bytes"
            raise MountedFileRefusedError(message)
        chunks.append(chunk)


def mounted_file_refusal(path: Path, *, mounts: Iterable[Path]) -> str | None:
    """Why *path* cannot be read or replaced, or None when it can.

    ``"missing"`` when nothing is there. Any other refusal, including a mount root that is not
    a directory, is returned as a reason rather than raised, and logged. Never opens the file
    for reading.
    """
    reason = _file_refusal(path, tuple(mounts))
    if reason is not None and reason != "missing":
        _refused(path, reason)
    return reason


def _file_refusal(path: Path, mounts: tuple[Path, ...]) -> str | None:
    try:
        walk, resolved = _classify(path, mounts)
        if walk is None:
            return _operator_refusal(resolved)
        return _walk_refusal(walk)
    except FileNotFoundError:
        return "missing"
    except MountedFileRefusedError as exc:
        return str(exc)
    except OSError:
        return "unreadable"


def mounted_dir_refusal(path: Path, *, mounts: Iterable[Path]) -> str | None:
    """Why *path* is not a directory the daemon may use, or None when it is one (219).

    Every component is opened without following links below a mount, so a link planted at
    the directory or above it is refused. ``"missing"`` when nothing is there.
    """
    reason = _dir_refusal(path, tuple(mounts))
    if reason is not None and reason != "missing":
        _refused(path, reason)
    return reason


def _dir_refusal(path: Path, mounts: tuple[Path, ...]) -> str | None:
    try:
        walk, resolved = _classify(path, mounts)
        if walk is None:
            info = resolved.stat()
            return None if stat.S_ISDIR(info.st_mode) else "not a directory"
        os.close(_open_chain(walk, create=False))
    except FileNotFoundError:
        return "missing"
    except MountedFileRefusedError as exc:
        return str(exc)
    except OSError:
        return "unreadable"
    return None


def read_mounted_dir_names(path: Path, *, mounts: Iterable[Path]) -> list[str] | None:
    """The entry names of the directory at *path*, or None when it is missing or refused (219).

    The directory is opened through the same anchored walk as :func:`mounted_dir_refusal`, so
    a link at the directory or above it is never listed through.
    """
    try:
        walk, resolved = _classify(path, tuple(mounts))
        if walk is None:
            with os.scandir(resolved) as entries:
                return [entry.name for entry in entries]
        fd = _open_chain(walk, create=False)
        try:
            with os.scandir(fd) as entries:
                return [entry.name for entry in entries]
        finally:
            os.close(fd)
    except FileNotFoundError:
        return None
    except MountedFileRefusedError as exc:
        _refused(path, str(exc))
        return None
    except OSError as exc:
        _refused(path, f"unreadable: {exc.strerror}")
        return None


def anchored_mkdir(path: Path, *, mounts: Iterable[Path]) -> None:
    """Create the directory *path* with its missing parents, the way ``Path.mkdir`` does (219).

    Inside a mount every component is created and opened by descriptor with ``O_NOFOLLOW``, so
    a link, a regular file or a FIFO where a directory is expected is refused (and logged) and
    nothing is created through it. Raises an ``OSError``, including :class:`MountedFileRefusedError`.
    """
    try:
        walk, resolved = _classify(path, tuple(mounts))
        if walk is None:
            resolved.mkdir(parents=True, exist_ok=True)
            return
        os.close(_open_chain(walk, create=True))
    except MountedFileRefusedError as exc:
        _refused(path, str(exc))
        raise


def _walk_refusal(walk: _Walk) -> str | None:
    dir_fd, leaf = _open_walk(walk)
    try:
        return _leaf_refusal(dir_fd, leaf)
    finally:
        os.close(dir_fd)


def _operator_refusal(path: Path) -> str | None:
    try:
        info = path.stat()
    except FileNotFoundError:
        return "missing"
    return None if stat.S_ISREG(info.st_mode) else "not a regular file"


def _leaf_refusal(dir_fd: int, leaf: str) -> str | None:
    try:
        info = os.stat(leaf, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return "missing"
    if stat.S_ISLNK(info.st_mode):
        return "symlink"
    if not stat.S_ISREG(info.st_mode):
        return "not a regular file"
    return "hard linked" if info.st_nlink != 1 else None


def read_mounted_file(path: Path, *, max_bytes: int, mounts: Iterable[Path]) -> bytes | None:
    """Read a file, or None when it is missing or refused.

    *mounts* decides the rule (see the module docstring). Both rules refuse a non-regular file,
    a FIFO (opened non-blocking, then type-checked) and a file over *max_bytes*. The size is
    bounded while reading as well as before it.
    """
    try:
        return _read(path, max_bytes, tuple(mounts))
    except FileNotFoundError:
        return None
    except MountedFileRefusedError as exc:
        _refused(path, str(exc))
        return None
    except OSError as exc:
        _refused(path, f"unreadable: {exc.strerror}")
        return None


def open_mounted_file(path: Path, *, mounts: Iterable[Path]) -> BinaryIO | None:
    """Open a regular, single-link file for streamed reading, or None when it is refused (220).

    The same walk and refusals as :func:`read_mounted_file`, without a size bound: the caller
    reads a line at a time under its own bound (the transcript ledger). The returned file owns
    its descriptor and is closed by the caller.
    """
    try:
        return _open_stream(path, tuple(mounts))
    except FileNotFoundError:
        return None
    except MountedFileRefusedError as exc:
        _refused(path, str(exc))
        return None
    except OSError as exc:
        _refused(path, f"unreadable: {exc.strerror}")
        return None


def _open_stream(path: Path, mounts: tuple[Path, ...]) -> BinaryIO:
    walk, resolved = _classify(path, mounts)
    if walk is None:
        fd = os.open(resolved, os.O_RDONLY | _NONBLOCK)
        try:
            _check_regular(os.fstat(fd), None)
        except BaseException:
            os.close(fd)
            raise
        return os.fdopen(fd, "rb")
    dir_fd, leaf = _open_walk(walk)
    try:
        fd = _open_leaf(dir_fd, leaf)
    finally:
        os.close(dir_fd)
    try:
        _check_anchored(os.fstat(fd), None)
    except BaseException:
        os.close(fd)
        raise
    return os.fdopen(fd, "rb")


def _read(path: Path, max_bytes: int, mounts: tuple[Path, ...]) -> bytes:
    walk, resolved = _classify(path, mounts)
    if walk is None:
        fd = os.open(resolved, os.O_RDONLY | _NONBLOCK)
        try:
            _check_regular(os.fstat(fd), max_bytes)
            return _drain(fd, max_bytes)
        finally:
            os.close(fd)
    dir_fd, leaf = _open_walk(walk)
    try:
        fd = _open_leaf(dir_fd, leaf)
        try:
            _check_anchored(os.fstat(fd), max_bytes)
            return _drain(fd, max_bytes)
        finally:
            os.close(fd)
    finally:
        os.close(dir_fd)


def _open_leaf(dir_fd: int, leaf: str) -> int:
    try:
        return os.open(leaf, os.O_RDONLY | _NOFOLLOW | _NONBLOCK, dir_fd=dir_fd)
    except FileNotFoundError:
        raise
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            message = "symlink"
            raise MountedFileRefusedError(message) from exc
        message = f"open failed: {exc.strerror}"
        raise MountedFileRefusedError(message) from exc


def replace_mounted_file(
    path: Path,
    data: bytes,
    *,
    mounts: Iterable[Path],
    mode: int | None = None,
) -> None:
    """Atomically replace a file with *data*.

    Inside a mount, every step goes through directory descriptors with ``O_NOFOLLOW``: the
    temporary file is created ``O_EXCL`` and renamed over the leaf by descriptor, and an
    existing leaf must be a plain single-name regular file. On an operator path the resolved
    path is written by name. The mode is set on the temporary descriptor before the rename:
    *mode* when given (set even on an existing file), else the existing file's mode masked to
    0755, else 0644.
    Raises an ``OSError`` on failure, including :class:`MountedFileRefusedError` (logged).
    """
    try:
        walk, resolved = _classify(path, tuple(mounts))
        if walk is None:
            dir_fd = os.open(resolved.parent, os.O_RDONLY | os.O_DIRECTORY)
            leaf = resolved.name
            operator = True
        else:
            dir_fd, leaf = _open_walk(walk)
            operator = False
        try:
            existing = _existing_mode(dir_fd, leaf, operator=operator)
            _rename_into(dir_fd, leaf, data, _target_mode(mode, existing))
        finally:
            os.close(dir_fd)
    except MountedFileRefusedError as exc:
        _refused(path, str(exc))
        raise


def _target_mode(mode: int | None, existing: int | None) -> int:
    """The mode a replacement is created with: explicit, else inherited (masked), else 0644.

    An inherited mode is masked to ``0o755``: a performer that planted the leaf with setuid,
    setgid, sticky or group/other-write bits does not get them carried into the daemon's file.
    """
    if mode is not None:
        return mode
    if existing is not None:
        return existing & _INHERITED_MODE_MASK
    return _DEFAULT_FILE_MODE


def _existing_mode(dir_fd: int, leaf: str, *, operator: bool) -> int | None:
    """The mode of a regular leaf to replace, or None when there is none. Refuses the rest."""
    try:
        info = os.stat(leaf, dir_fd=dir_fd, follow_symlinks=operator)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        message = "not a regular file"
        raise MountedFileRefusedError(message)
    if not operator and info.st_nlink != 1:
        message = "hard linked"
        raise MountedFileRefusedError(message)
    return stat.S_IMODE(info.st_mode)


def _name_is_entry(dir_fd: int, name: str, fd: int) -> bool:
    """Does *name* in *dir_fd* still refer to the regular file that *fd* holds? (220, item 4).

    Compares the descriptor's identity (fstat) with the name's identity (lstat, so a link at the
    name is not followed and never matches).
    """
    try:
        info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    held = os.fstat(fd)
    return stat.S_ISREG(info.st_mode) and (info.st_dev, info.st_ino) == (held.st_dev, held.st_ino)


def _require_entry(dir_fd: int, name: str, fd: int, message: str, *, remove: bool) -> None:
    """Refuse unless *name* still refers to the file *fd* holds. ``remove`` drops the entry first."""
    if _name_is_entry(dir_fd, name, fd):
        return
    if remove:
        with contextlib.suppress(OSError):
            os.unlink(name, dir_fd=dir_fd)
    raise MountedFileRefusedError(message)


def _rename_into(dir_fd: int, leaf: str, data: bytes, mode: int) -> None:
    """Write *data* to a temporary file and rename it over *leaf*, checking the name by descriptor.

    The temporary name is checked against the descriptor it was written through, before the
    rename and again after it. A performer that replaces the name with a link or another file in
    that window is refused: the planted entry is removed, the host file it points at is never
    written, and the replacement fails. The rename itself acts on names, so a race between the
    check and the rename can only be detected, not prevented, by POSIX calls.
    """
    tmp = f".{leaf}.{uuid.uuid4().hex}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o600, dir_fd=dir_fd)
    try:
        os.fchmod(fd, mode)
        _write_all(fd, data)
        os.fsync(fd)
        _require_entry(dir_fd, tmp, fd, "temporary file replaced before the rename", remove=False)
        os.replace(tmp, leaf, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        _require_entry(dir_fd, leaf, fd, "replacement swapped after the rename", remove=True)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp, dir_fd=dir_fd)
        raise
    finally:
        os.close(fd)


def _refused(path: Path, reason: str) -> None:
    logger.error("mounted_file.refused", path=str(path), reason=reason)


@dataclass(frozen=True)
class TreeEntry:
    """One entry under a walked tree (234). Links are recorded by their target text, never followed."""

    rel: str
    kind: Literal["file", "dir", "link"]
    mode: int
    size: int
    ino: int
    ctime_ns: int
    sha256: str | None = None
    target: str | None = None


def walk_mounted_tree(
    path: Path,
    *,
    mounts: Iterable[Path],
    hash_files: bool,
    max_entries: int,
    max_bytes: int,
) -> list[TreeEntry]:
    """Every entry below *path*, anchored like the reads above (234).

    Each entry is stat'ed with ``follow_symlinks=False`` relative to its directory descriptor, and a link is read
    with ``readlink``. A directory is opened with ``O_NOFOLLOW``. A regular file is opened the same way and is
    refused when it is hard linked or when its identity changes between the stat and the open. A FIFO, socket or
    device is refused. With *hash_files* the bytes are streamed into sha256; without it only ``stat`` is taken.

    The walk refuses, with a :class:`MountedFileRefusedError`, when it reaches *max_entries* entries or
    *max_bytes* of file data. A bound is a refusal, never a truncated walk.
    """
    walk, resolved = _classify(path, tuple(mounts))
    fd = os.open(resolved, os.O_RDONLY | os.O_DIRECTORY) if walk is None else _open_chain(walk, create=False)
    tree = _TreeWalk(hash_files=hash_files, max_entries=max_entries, max_bytes=max_bytes)
    try:
        tree.walk(fd, "")
    finally:
        os.close(fd)
    return tree.entries


@dataclass
class _TreeWalk:
    """The state of one anchored walk: the bounds, the entries collected so far, and the bytes counted."""

    hash_files: bool
    max_entries: int
    max_bytes: int
    entries: list[TreeEntry] = field(default_factory=list)
    total: int = 0

    def walk(self, dir_fd: int, prefix: str) -> None:
        with os.scandir(dir_fd) as scan:
            names = sorted(entry.name for entry in scan)
        for name in names:
            if len(self.entries) >= self.max_entries:
                message = f"has more than {self.max_entries} entries"
                raise MountedFileRefusedError(message)
            self._entry(dir_fd, name, f"{prefix}{name}")

    def _entry(self, dir_fd: int, name: str, rel: str) -> None:
        info = os.stat(name, dir_fd=dir_fd, follow_symlinks=False)
        mode = stat.S_IMODE(info.st_mode)
        if stat.S_ISLNK(info.st_mode):
            target = os.readlink(name, dir_fd=dir_fd)
            self.entries.append(TreeEntry(rel, "link", mode, len(target), info.st_ino, info.st_ctime_ns, None, target))
        elif stat.S_ISDIR(info.st_mode):
            self.entries.append(TreeEntry(rel, "dir", mode, 0, info.st_ino, info.st_ctime_ns))
            child = _open_dir(name, dir_fd)
            try:
                self.walk(child, f"{rel}/")
            finally:
                os.close(child)
        elif stat.S_ISREG(info.st_mode):
            self._file(dir_fd, name, rel, info, mode)
        else:
            message = f"{rel} is not a regular file, directory or link"
            raise MountedFileRefusedError(message)

    def _file(self, dir_fd: int, name: str, rel: str, info: os.stat_result, mode: int) -> None:
        if info.st_nlink != 1:
            message = f"{rel} is hard linked"
            raise MountedFileRefusedError(message)
        self.total += info.st_size
        if self.total > self.max_bytes:
            message = f"holds more than {self.max_bytes} bytes"
            raise MountedFileRefusedError(message)
        digest = _hash_leaf(dir_fd, name, info, rel, self.max_bytes) if self.hash_files else None
        self.entries.append(TreeEntry(rel, "file", mode, info.st_size, info.st_ino, info.st_ctime_ns, digest))


def _hash_leaf(dir_fd: int, name: str, info: os.stat_result, rel: str, max_bytes: int) -> str:
    """sha256 of one regular file, opened without following a link and checked against its stat."""
    fd = _open_leaf(dir_fd, name)
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino) or not stat.S_ISREG(opened.st_mode):
            message = f"{rel} was replaced while it was read"
            raise MountedFileRefusedError(message)
        digest = hashlib.sha256()
        remaining = max_bytes
        while True:
            chunk = os.read(fd, _CHUNK_BYTES)
            if not chunk:
                break
            remaining -= len(chunk)
            if remaining < 0:
                message = f"{rel} grew past the bound while it was read"
                raise MountedFileRefusedError(message)
            digest.update(chunk)
        return digest.hexdigest()
    finally:
        os.close(fd)
