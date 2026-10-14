"""Spec 220 item 4: a performer swaps the temporary file's name during a replacement.

``_rename_into`` writes a temporary file in the target directory, then renames it over the leaf
by name. A performer that can write there can replace the temporary name with a link between
the write and the rename, so the link becomes the leaf. The daemon now checks that the name still
refers to the descriptor it wrote (fstat against lstat on the name) before the rename, and checks
again after it. A mismatch is refused, the planted entry is removed, and the host file is untouched.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from coordinare.services.mounted_file import replace_mounted_file

_HOST_TEXT = "HOST SECRET\n"
_OLD_TEXT = "old manifest\n"


def _setup(tmp: Path) -> tuple[Path, Path, Path]:
    """A mount ``ws`` holding ``manifest.json``, and a host file the performer wants written to."""
    ws = tmp / "ws"
    ws.mkdir()
    target = ws / "manifest.json"
    target.write_text(_OLD_TEXT)
    host = tmp / "host" / "secret.env"
    host.parent.mkdir()
    host.write_text(_HOST_TEXT)
    return ws, target, host


def _temp_names(ws: Path) -> list[str]:
    return [name for name in os.listdir(ws) if name.endswith(".tmp")]


def test_temp_name_swapped_before_the_rename_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    ws, target, host = _setup(tmp_path)
    real_fsync = os.fsync

    def swap_after_fsync(fd: int) -> None:
        real_fsync(fd)
        for name in _temp_names(ws):
            (ws / name).unlink()
            (ws / name).symlink_to(host)

    monkeypatch.setattr(os, "fsync", swap_after_fsync)

    with pytest.raises(OSError):
        replace_mounted_file(target, b"new\n", mounts=[ws])

    assert host.read_text() == _HOST_TEXT
    assert target.read_text() == _OLD_TEXT
    assert not target.is_symlink()


def test_temp_name_swapped_after_the_check_is_detected_and_removed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The swap lands between the check and ``os.replace``: the post-rename check catches it."""
    ws, target, host = _setup(tmp_path)
    real_replace = os.replace

    def swap_then_replace(
        src: str,
        dst: str,
        *,
        src_dir_fd: int | None = None,
        dst_dir_fd: int | None = None,
    ) -> None:
        if str(src).endswith(".tmp") and src_dir_fd is not None:
            os.unlink(src, dir_fd=src_dir_fd)
            os.symlink(host, src, dir_fd=src_dir_fd)
        real_replace(src, dst, src_dir_fd=src_dir_fd, dst_dir_fd=dst_dir_fd)

    monkeypatch.setattr(os, "replace", swap_then_replace)

    with pytest.raises(OSError):
        replace_mounted_file(target, b"new\n", mounts=[ws])

    assert host.read_text() == _HOST_TEXT
    assert not target.is_symlink()
    assert not target.exists()
    assert _temp_names(ws) == []


def test_an_honest_replacement_still_lands(tmp_path: Path) -> None:
    ws, target, host = _setup(tmp_path)

    replace_mounted_file(target, b"new\n", mounts=[ws])

    assert target.read_text() == "new\n"
    assert host.read_text() == _HOST_TEXT
    assert _temp_names(ws) == []
