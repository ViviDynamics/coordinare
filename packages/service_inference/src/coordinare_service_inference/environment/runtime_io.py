"""Owned, cooperative filesystem work under an existing runtime deadline."""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import math
import os
import stat
import threading
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path


class RuntimeIOStoppedError(Exception):
    """The owner requested that an in-progress filesystem worker stop."""


def check_io(deadline: float | None, stop: threading.Event | None) -> None:
    if stop is not None and stop.is_set():
        raise RuntimeIOStoppedError
    if deadline is not None and (not math.isfinite(deadline) or time.monotonic() >= deadline):
        message = 'private runtime filesystem deadline exhausted or invalid'
        raise TimeoutError(message)


def digest_file(path: Path, *, deadline: float | None, stop: threading.Event | None = None) -> str:
    """Read regular files in bounded chunks; reject mutation and special files."""
    check_io(deadline, stop)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            message = 'private runtime artifact must be a regular file'
            raise ValueError(message)
        digest = hashlib.sha256()
        while True:
            check_io(deadline, stop)
            block = os.read(descriptor, 1024*1024)
            check_io(deadline, stop)
            if not block:
                break
            digest.update(block)
        after = os.fstat(descriptor)
        current = path.lstat()
        identity = ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')
        if (not stat.S_ISREG(current.st_mode)
                or any(getattr(before, key) != getattr(value, key) for key in identity for value in (after, current))):
            message = 'private runtime artifact changed while hashing'
            raise ValueError(message)
        check_io(deadline, stop)
        return digest.hexdigest()
    finally:
        os.close(descriptor)


async def settle[T](future: asyncio.Future[T]) -> T:
    """Drain owned work through repeated cancellation without losing failure."""
    cancelled = False
    while not future.done():
        try:
            await asyncio.shield(future)
        except asyncio.CancelledError:
            cancelled = True
        except Exception:
            break
    result = future.result()
    if cancelled:
        raise asyncio.CancelledError
    return result


async def run_io[T](operation: Callable[..., T], *, deadline: float) -> T:
    """Do not release filesystem ownership until its cooperative worker stops."""
    check_io(deadline, None)
    stop = threading.Event()
    worker = asyncio.create_task(asyncio.to_thread(operation, deadline=deadline, stop=stop))
    try:
        async with asyncio.timeout(max(0, deadline-time.monotonic())):
            result = await asyncio.shield(worker)
            check_io(deadline, stop)
            return result
    finally:
        stop.set()
        with contextlib.suppress(RuntimeIOStoppedError):
            await settle(worker)
