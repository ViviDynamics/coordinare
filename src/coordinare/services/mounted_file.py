"""Reads and writes in a directory a performer can write (spec 208).

A file under ``performer_log_dir``, or under any other directory mounted read-write
into a performer, holds whatever the performer put there. Every path component below
the trusted anchor can be a link the performer made, so each one is opened with
``O_NOFOLLOW`` relative to the directory descriptor before it, and the leaf is opened
relative to the last one. A link above the leaf is refused the same way a link at the
leaf is. Nothing is resolved by name after a check, so a component swapped after the
check is still refused at open.

Two anchors:

* ``root`` given: the directory is trusted (the operator's configured mount host, or a
  clone the daemon made). It may itself be a link. Components below it are checked.
* ``root`` omitted: the whole parent path is checked. A parent that contains any link
  is refused, because nothing above it is trusted.

Reads are bounded and refuse anything that is not a plain single-name regular file
(a FIFO would block, a directory is not content). A refusal is logged and reads as
"no file". The bytes are untrusted: callers redact them before showing them and never
execute or import them.
"""

from __future__ import annotations

import contextlib
import errno
import os
import stat
import uuid
from typing import TYPE_CHECKING

import structlog

from coordinare.services.run_transcript import _NOFOLLOW, _NONBLOCK, _write_all

if TYPE_CHECKING:
    from pathlib import Path

logger = structlog.get_logger(__name__)

_CHUNK_BYTES = 64 * 1024
_DEFAULT_FILE_MODE = 0o644


class MountedFileRefusedError(OSError):
    """A write or read the daemon will not perform on a performer-writable path."""


def _classify(info: os.stat_result) -> str | None:
    """Why an lstat result is not a plain single-name regular file, or None when it is one."""
    if stat.S_ISLNK(info.st_mode):
        return "symlink"
    if not stat.S_ISREG(info.st_mode):
        return "not a regular file"
    if info.st_nlink != 1:
        return "hard linked"
    return None


def _open_parent(path: Path, root: Path | None) -> tuple[int, str]:
    """Open the directory that holds *path* and return ``(dir_fd, leaf_name)``.

    Raises ``FileNotFoundError`` when a directory on the way is missing, and
    :class:`MountedFileRefusedError` when a directory on the way is a link or is not a
    directory. The caller closes ``dir_fd``.
    """
    absolute = path.absolute()
    parent = absolute.parent
    if root is None:
        if parent.resolve() != parent:
            message = "a parent directory is a link"
            raise MountedFileRefusedError(message)
        start, parts = "/", parent.parts[1:]
    else:
        anchor = root.absolute()
        if not absolute.is_relative_to(anchor):
            message = "outside its root"
            raise MountedFileRefusedError(message)
        start, parts = str(anchor), parent.relative_to(anchor).parts
    fd = os.open(start, os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts:
            nxt = _open_dir(part, fd)
            os.close(fd)
            fd = nxt
    except BaseException:
        os.close(fd)
        raise
    return fd, absolute.name


def _open_dir(name: str, dir_fd: int) -> int:
    try:
        return os.open(name, os.O_RDONLY | os.O_DIRECTORY | _NOFOLLOW, dir_fd=dir_fd)
    except FileNotFoundError:
        raise
    except OSError as exc:
        message = "a parent directory is a link or not a directory"
        raise MountedFileRefusedError(message) from exc


def mounted_file_refusal(path: Path, *, root: Path | None = None) -> str | None:
    """Why *path* is not a plain single-name regular file, or None when it is one.

    ``"missing"`` when nothing is there or a directory on the way is missing. Uses
    ``lstat`` relative to the directory descriptor and never opens the file, so it is
    safe to call before a write as well as before a read.
    """
    try:
        dir_fd, leaf = _open_parent(path, root)
    except FileNotFoundError:
        return "missing"
    except MountedFileRefusedError as exc:
        return str(exc)
    try:
        return _leaf_refusal(dir_fd, leaf)
    finally:
        os.close(dir_fd)


def _leaf_refusal(dir_fd: int, leaf: str) -> str | None:
    try:
        info = os.stat(leaf, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return "missing"
    return _classify(info)


def read_mounted_file(path: Path, *, max_bytes: int, root: Path | None = None) -> bytes | None:
    """Read a performer-writable file, or None when it is missing or refused.

    The open uses ``O_NOFOLLOW`` and ``O_NONBLOCK``, so a planted link or FIFO is refused
    without being followed or waited on. The descriptor is then checked as a regular file
    with one name, and the size is bounded while reading, so a file that grows after the
    ``fstat`` still cannot exceed *max_bytes*.
    """
    try:
        return _read_checked(path, max_bytes, root)
    except FileNotFoundError:
        return None
    except MountedFileRefusedError as exc:
        _refused(path, str(exc))
        return None
    except OSError as exc:
        _refused(path, f"unreadable: {exc.strerror}")
        return None


def _read_checked(path: Path, max_bytes: int, root: Path | None) -> bytes:
    dir_fd, leaf = _open_parent(path, root)
    try:
        fd = _open_leaf(dir_fd, leaf)
        try:
            _check_descriptor(os.fstat(fd), max_bytes)
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


def _check_descriptor(info: os.stat_result, max_bytes: int) -> None:
    reason = _classify(info)
    if reason is not None:
        raise MountedFileRefusedError(reason)
    if info.st_size > max_bytes:
        message = f"larger than {max_bytes} bytes"
        raise MountedFileRefusedError(message)


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


def replace_mounted_file(path: Path, data: bytes, *, root: Path | None = None) -> None:
    """Atomically replace a performer-writable file with *data*, through the directory fd.

    The leaf must be a plain regular file or absent (a new file gets 0644; an existing one
    keeps its mode). The temporary file is created ``O_EXCL`` and ``O_NOFOLLOW`` in the
    same directory and renamed over the leaf with ``dir_fd`` arguments, so no step follows
    a name. Raises :class:`MountedFileRefusedError` (an ``OSError``) on a refusal.
    """
    dir_fd, leaf = _open_parent(path, root)
    try:
        mode = _existing_mode(dir_fd, leaf)
        tmp = f".{leaf}.{uuid.uuid4().hex}.tmp"
        _write_tmp(dir_fd, tmp, data, mode)
        try:
            os.replace(tmp, leaf, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp, dir_fd=dir_fd)
            raise
    finally:
        os.close(dir_fd)


def _existing_mode(dir_fd: int, leaf: str) -> int:
    try:
        info = os.stat(leaf, dir_fd=dir_fd, follow_symlinks=False)
    except FileNotFoundError:
        return _DEFAULT_FILE_MODE
    reason = _classify(info)
    if reason is not None:
        raise MountedFileRefusedError(reason)
    return stat.S_IMODE(info.st_mode)


def _write_tmp(dir_fd: int, tmp: str, data: bytes, mode: int) -> None:
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


def _refused(path: Path, reason: str) -> None:
    logger.warning("mounted_file.refused", path=str(path), reason=reason)
