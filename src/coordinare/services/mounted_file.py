"""Reads from a directory a performer can write (spec 208).

A file under ``performer_log_dir``, or under any other directory mounted read-write
into a performer, holds whatever the performer put there. The daemon reads such a file
only through :func:`read_mounted_file`, which refuses a symlink (the open does not
follow it), a hard link (the same inode is reachable from elsewhere), anything that is
not a regular file (a FIFO would block, a directory is not content), and a file larger
than the caller's bound. A refusal is logged and reads as "no file". The bytes are
untrusted: callers redact them before showing them and never execute or import them.
"""

from __future__ import annotations

import errno
import os
import stat
from typing import TYPE_CHECKING

import structlog

from coordinare.services.run_transcript import _NOFOLLOW, _NONBLOCK

if TYPE_CHECKING:
    from pathlib import Path

logger = structlog.get_logger(__name__)

_CHUNK_BYTES = 64 * 1024


def mounted_file_refusal(path: Path) -> str | None:
    """Why *path* is not a plain single-name regular file, or None when it is one.

    Uses ``lstat`` only and never opens the file, so it is safe to call before a write
    as well as before a read. A missing path is reported as ``"missing"``.
    """
    try:
        info = os.lstat(path)
    except OSError:
        return "missing"
    if stat.S_ISLNK(info.st_mode):
        return "symlink"
    if not stat.S_ISREG(info.st_mode):
        return "not a regular file"
    if info.st_nlink != 1:
        return "hard linked"
    return None


class _RefusedReadError(Exception):
    """A read the daemon will not perform. The message is the reason that is logged."""


def read_mounted_file(path: Path, *, max_bytes: int) -> bytes | None:
    """Read a performer-writable file, or None when it is missing or refused.

    The open uses ``O_NOFOLLOW`` and ``O_NONBLOCK``, so a planted link or FIFO is refused
    without being followed or waited on. The descriptor is then checked as a regular file
    with one name, and the size is bounded while reading, so a file that grows after the
    ``fstat`` still cannot exceed *max_bytes*.
    """
    try:
        return _read_checked(path, max_bytes)
    except FileNotFoundError:
        return None
    except _RefusedReadError as exc:
        _refused(path, str(exc))
        return None


def _read_checked(path: Path, max_bytes: int) -> bytes:
    try:
        fd = os.open(path, os.O_RDONLY | _NOFOLLOW | _NONBLOCK)
    except FileNotFoundError:
        raise
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise _RefusedReadError("symlink") from exc
        message = f"open failed: {exc.strerror}"
        raise _RefusedReadError(message) from exc
    try:
        _check_descriptor(os.fstat(fd), max_bytes)
        return _drain(fd, max_bytes)
    finally:
        os.close(fd)


def _check_descriptor(info: os.stat_result, max_bytes: int) -> None:
    if not stat.S_ISREG(info.st_mode):
        message = "not a regular file"
        raise _RefusedReadError(message)
    if info.st_nlink != 1:
        message = "hard linked"
        raise _RefusedReadError(message)
    if info.st_size > max_bytes:
        message = f"larger than {max_bytes} bytes"
        raise _RefusedReadError(message)


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
            raise _RefusedReadError(message)
        chunks.append(chunk)


def _refused(path: Path, reason: str) -> None:
    logger.warning("mounted_file.refused", path=str(path), reason=reason)
