"""Bounded removal of one owned runtime tree, without following links."""
from __future__ import annotations

import contextlib
import os
import stat
from typing import TYPE_CHECKING

from .runtime_io import check_io

if TYPE_CHECKING:
    from pathlib import Path
    from threading import Event


def _directory_flags() -> int:
    # Resolve platform flags only after Linux runtime admission, never at import.
    return os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW


def _remove_entries(descriptor: int, *, deadline: float, stop: Event | None) -> None:
    check_io(deadline, stop)
    with os.scandir(descriptor) as members:
        for member in members:
            check_io(deadline, stop)
            metadata = member.stat(follow_symlinks=False)
            check_io(deadline, stop)
            if stat.S_ISDIR(metadata.st_mode):
                child = os.open(member.name, _directory_flags(), dir_fd=descriptor)
                try:
                    _remove_entries(child, deadline=deadline, stop=stop)
                finally:
                    os.close(child)
                check_io(deadline, stop)
                os.rmdir(member.name, dir_fd=descriptor)
            else:
                os.unlink(member.name, dir_fd=descriptor)
            check_io(deadline, stop)


def remove_owned_tree(path: Path, *, deadline: float, stop: Event | None = None) -> None:
    """Cooperate with the original deadline and the owning worker's stop signal."""
    check_io(deadline, stop)
    descriptor = os.open(path, _directory_flags())
    try:
        _remove_entries(descriptor, deadline=deadline, stop=stop)
        check_io(deadline, stop)
    finally:
        os.close(descriptor)
    os.rmdir(path)
    check_io(deadline, stop)


def remove_control_material(path: Path) -> None:
    """Emergency cleanup is finite: only the adapter's known paths, never a walk.

    This may run after expiry to remove credentials. It cannot turn an expired or
    incomplete tree removal into accepted execution evidence.
    """
    try:
        descriptor = os.open(path, _directory_flags())
    except OSError:
        return
    try:
        for name in ('client-key', 'client-key.pub', 'known-hosts', 'ssh-config',
                     'user-data', 'meta-data', 'seed.iso', 'serial.log', 'working.qcow2'):
            with contextlib.suppress(OSError):
                os.unlink(name, dir_fd=descriptor)
        for directory, names in (('bin', ('ssh',)), ('docker', ('config.json',)),
                                 ('guest-control', ('host-key', 'host-key.pub', 'authorized_keys'))):
            try:
                child = os.open(directory, _directory_flags(), dir_fd=descriptor)
            except OSError:
                continue
            try:
                for name in names:
                    with contextlib.suppress(OSError):
                        os.unlink(name, dir_fd=child)
            finally:
                os.close(child)
            with contextlib.suppress(OSError):
                os.rmdir(directory, dir_fd=descriptor)
    finally:
        os.close(descriptor)
    with contextlib.suppress(OSError):
        os.rmdir(path)
