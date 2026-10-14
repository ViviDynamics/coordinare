"""Reads and writes of files the daemon takes from disk (spec 208).

There are two kinds of path, and the rule depends on which one a file is:

* **Inside a performer mount** (performer log dir, endpoint volume, env-cache root). A
  performer can write there, so it can plant a link at any level. The walk starts at the
  mount's root, which the operator configured and which is resolved once. Every component
  below it is opened with ``O_NOFOLLOW`` relative to the directory descriptor before it,
  and the leaf is opened relative to the last one. A link anywhere below the mount root
  is refused. Links above the mount root are not refused: they are the operator's.
* **Outside every performer mount.** The operator controls the path, so it is opened
  normally and links in it are followed. Non-regular files (a FIFO is refused by a
  non-blocking open and the type check), and files over the caller's bound, are still
  refused.

A clone is the same as a mount: its contents are performer-authored, so the caller anchors
the walk at the clone root (:func:`anchor_at`), and links below it are refused.

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
)

if TYPE_CHECKING:
    from collections.abc import Iterable

    from coordinare.config import ProjectConfiguration

logger = structlog.get_logger(__name__)

_CHUNK_BYTES = 64 * 1024
_DEFAULT_FILE_MODE = 0o644


class MountedFileRefusedError(OSError):
    """A read or write the daemon will not perform on a performer-writable path."""


@dataclass(frozen=True)
class Anchor:
    """A directory below which every component is checked for links.

    ``start`` is the resolved directory the walk opens first. ``base`` is the same directory
    spelled the way the caller's paths spell it, so a path can be measured against it.
    """

    start: Path
    base: Path


def anchor_at(root: Path) -> Anchor:
    """An anchor at *root*, which is trusted (operator config or a clone the daemon made).

    *root* itself may be a link. Components below it are checked.
    """
    return Anchor(start=root.resolve(), base=root.absolute())


def performer_mount_hosts(config: ProjectConfiguration) -> list[Path]:
    """Every host directory a performer container can see (129, 208).

    The performer log dir is bind-mounted into ephemeral containers, each endpoint may
    declare more volumes, and the env-cache root holds per-symphony caches mounted into
    performers. Used by the transcript and run-log guards and by the file readers here.
    """
    hosts: list[Path] = []
    log_dir = getattr(config, "performer_log_dir", None)
    if log_dir is not None:
        hosts.append(Path(log_dir))
    for endpoint in config.performer_endpoints:
        hosts.extend(Path(volume.host_path) for volume in endpoint.volumes)
    hosts.append(Path(config.env_cache_root).expanduser())
    return hosts


def _parts_below(path: Path, base: Path) -> tuple[str, ...] | None:
    """The components of *path* below *base*, or None when *base* is not an ancestor.

    Compared case-insensitively where the filesystem that holds *base* is case-insensitive.
    """
    insensitive = _case_insensitive(base)
    path_parts = path.parts
    base_parts = base.parts
    if len(path_parts) < len(base_parts):
        return None
    head = path_parts[: len(base_parts)]
    if insensitive:
        same = tuple(p.casefold() for p in head) == tuple(p.casefold() for p in base_parts)
    else:
        same = head == base_parts
    return path_parts[len(base_parts) :] if same else None


def mount_anchor(path: Path, mounts: Iterable[Path]) -> Anchor | None:
    """The anchor of the deepest performer mount that holds *path*, or None.

    A path is inside a mount when it lies strictly below the mount's root, spelled either
    as the operator wrote it or resolved. Containment is decided on the path as given, not
    on its resolved form, so a link planted inside a mount cannot move a path out of it.
    """
    absolute = path.absolute()
    best: Anchor | None = None
    best_depth = 0
    for mount in mounts:
        resolved = mount.resolve()
        for base in (mount.absolute(), resolved):
            below = _parts_below(absolute, base)
            if below and len(base.parts) > best_depth:
                best = Anchor(start=resolved, base=base)
                best_depth = len(base.parts)
    return best


def _open_dir(name: str, dir_fd: int) -> int:
    try:
        return os.open(name, os.O_RDONLY | os.O_DIRECTORY | _NOFOLLOW, dir_fd=dir_fd)
    except FileNotFoundError:
        raise
    except OSError as exc:
        message = "a parent directory is a link or not a directory"
        raise MountedFileRefusedError(message) from exc


def _open_parent(path: Path, anchor: Anchor) -> tuple[int, str]:
    """Open the directory that holds *path*, component by component, under *anchor*.

    Returns ``(dir_fd, leaf_name)``. Raises ``FileNotFoundError`` when a directory on the way
    is missing and :class:`MountedFileRefusedError` when one is a link or not a directory.
    The caller closes ``dir_fd``.
    """
    absolute = path.absolute()
    below = _parts_below(absolute.parent, anchor.base)
    if below is None:
        message = "outside its anchor"
        raise MountedFileRefusedError(message)
    fd = os.open(anchor.start, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in below:
            nxt = _open_dir(part, fd)
            os.close(fd)
            fd = nxt
    except BaseException:
        os.close(fd)
        raise
    return fd, absolute.name


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


def mounted_file_refusal(path: Path, *, anchor: Anchor | None) -> str | None:
    """Why *path* cannot be read or replaced, or None when it can.

    ``"missing"`` when nothing is there or a directory on the way is missing. With an
    anchor, the check is link-refusing below it. Without one (an operator path), only the
    type is checked, and links are followed. Never opens the file.
    """
    if anchor is None:
        return _operator_refusal(path)
    try:
        dir_fd, leaf = _open_parent(path, anchor)
    except FileNotFoundError:
        return "missing"
    except MountedFileRefusedError as exc:
        return str(exc)
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


def read_mounted_file(path: Path, *, max_bytes: int, anchor: Anchor | None) -> bytes | None:
    """Read a file, or None when it is missing or refused.

    *anchor* decides the rule: an anchor (see :func:`mount_anchor` and :func:`anchor_at`)
    means the walk refuses links below it; ``None`` means an operator path, opened normally.
    Both refuse a non-regular file, a FIFO (the open is non-blocking, and the type is then
    checked) and a file larger than *max_bytes*, and the size is bounded while reading.
    """
    try:
        return _read(path, max_bytes, anchor)
    except FileNotFoundError:
        return None
    except MountedFileRefusedError as exc:
        _refused(path, str(exc))
        return None
    except OSError as exc:
        _refused(path, f"unreadable: {exc.strerror}")
        return None


def _read(path: Path, max_bytes: int, anchor: Anchor | None) -> bytes:
    if anchor is None:
        fd = os.open(path, os.O_RDONLY | _NONBLOCK)
        try:
            _check_regular(os.fstat(fd), max_bytes)
            return _drain(fd, max_bytes)
        finally:
            os.close(fd)
    dir_fd, leaf = _open_parent(path, anchor)
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


def replace_mounted_file(path: Path, data: bytes, *, anchor: Anchor | None) -> None:
    """Atomically replace a file with *data*.

    Under an anchor, the leaf must be a plain regular file or absent, and every step goes
    through directory descriptors with ``O_NOFOLLOW``. The temporary file is created
    ``O_EXCL`` and renamed over the leaf by descriptor. Without an anchor (an operator
    path) the parent is opened normally and the rename is by name, as before 208. An
    existing file keeps its mode. Raises an ``OSError`` on failure, including
    :class:`MountedFileRefusedError` for a refusal.
    """
    if anchor is None:
        dir_fd = os.open(path.absolute().parent, os.O_RDONLY | os.O_DIRECTORY)
        leaf = path.name
        follow = True
    else:
        dir_fd, leaf = _open_parent(path, anchor)
        follow = False
    try:
        mode = _existing_mode(dir_fd, leaf, follow=follow, operator=anchor is None)
        _rename_into(dir_fd, leaf, data, mode)
    finally:
        os.close(dir_fd)


def _existing_mode(dir_fd: int, leaf: str, *, follow: bool, operator: bool) -> int:
    try:
        info = os.stat(leaf, dir_fd=dir_fd, follow_symlinks=follow)
    except FileNotFoundError:
        return _DEFAULT_FILE_MODE
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
    logger.warning("mounted_file.refused", path=str(path), reason=reason)
