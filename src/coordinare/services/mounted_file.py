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
import os
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

import structlog

from coordinare.services.run_transcript import (
    _NOFOLLOW,
    _NONBLOCK,
    _case_insensitive,
    _write_all,
    comparable_part,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

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


def _parts_below(path: Path, base: Path) -> tuple[str, ...] | None:
    """The components of *path* below *base*, or None when *base* is not an ancestor of it.

    Compared case-insensitively where the filesystem that holds *base* is case-insensitive.
    """
    insensitive = _case_insensitive(base)
    path_parts = path.parts
    base_parts = base.parts
    if len(path_parts) < len(base_parts):
        return None
    head = tuple(comparable_part(p, insensitive=insensitive) for p in path_parts[: len(base_parts)])
    base_key = tuple(comparable_part(p, insensitive=insensitive) for p in base_parts)
    return path_parts[len(base_parts) :] if head == base_key else None


def _mount_entered(current: Path, mounts: Sequence[Path]) -> Path | None:
    """The outermost mount root that *current* is at or below, resolved now, or None."""
    found: Path | None = None
    for mount in mounts:
        root = mount.resolve()
        if _parts_below(current, root) is None:
            continue
        if found is None or len(root.parts) < len(found.parts):
            found = root
    return found


def _classify(
    path: Path,
    mounts: Sequence[Path],
    *,
    leaf: bool = True,
) -> tuple[_Walk | None, Path]:
    """Resolve *path* on the operator side. Returns ``(walk, resolved)``.

    ``walk`` is set when the path reaches a performer mount, and is then the walk to take from
    the mount root. Otherwise ``resolved`` is the operator path to open. Links are followed only
    while outside every mount: a link found on the way into a mount is the operator's, and a
    link below a mount root is never followed here (it is left for the ``O_NOFOLLOW`` walk).
    ``leaf=False`` allows the walk to end on the mount root itself (a directory-only operation).
    """
    absolute = path.absolute()
    current = Path(absolute.anchor)
    queue = list(absolute.parts[1:])
    links = 0
    while queue:
        root = _mount_entered(current, mounts)
        if root is not None:
            inside = _parts_below(current, root) or ()
            rest = [part for part in queue if part not in ("", ".")]
            if ".." in rest or (leaf and not rest):
                message = "'..' or an empty leaf below a performer mount"
                raise MountedFileRefusedError(message)
            return _Walk(start=root, parts=(*inside, *rest)), current
        part = queue.pop(0)
        if part in ("", "."):
            continue
        if part == "..":
            current = current.parent
            continue
        candidate = current / part
        if candidate.is_symlink():
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
            continue
        current = candidate
    return None, current


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


def _check_regular(info: os.stat_result, max_bytes: int) -> None:
    if not stat.S_ISREG(info.st_mode):
        message = "not a regular file"
        raise MountedFileRefusedError(message)
    if info.st_size > max_bytes:
        message = f"larger than {max_bytes} bytes"
        raise MountedFileRefusedError(message)


def _check_anchored(info: os.stat_result, max_bytes: int) -> None:
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
        walk, resolved = _classify(path, mounts, leaf=False)
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
        walk, resolved = _classify(path, tuple(mounts), leaf=False)
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
        walk, resolved = _classify(path, tuple(mounts), leaf=False)
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


def _rename_into(dir_fd: int, leaf: str, data: bytes, mode: int) -> None:
    tmp = f".{leaf}.{uuid.uuid4().hex}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o600, dir_fd=dir_fd)
    try:
        os.fchmod(fd, mode)
        _write_all(fd, data)
        os.fsync(fd)
    except BaseException:
        os.close(fd)
        with contextlib.suppress(OSError):
            os.unlink(tmp, dir_fd=dir_fd)
        raise
    os.close(fd)
    try:
        os.replace(tmp, leaf, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp, dir_fd=dir_fd)
        raise


def _refused(path: Path, reason: str) -> None:
    logger.error("mounted_file.refused", path=str(path), reason=reason)
