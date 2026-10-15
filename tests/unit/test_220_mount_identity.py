"""Spec 220 items 2 and 3: mount identity by (st_dev, st_ino), and the cost of the walk.

A performer-writable directory can be reachable at a second path that a string comparison does
not connect to the configured mount root: a bind mount, or a volume mounted twice. Item 2 makes
the walk compare the identity of every directory it reaches with the identity of each root.

Local coverage (macOS): the data-volume firmlink ``/System/Volumes/Data/<path>`` is a second
path to the same directory that ``Path.resolve()`` does not unify. That is the test below. On
Linux the same test uses ``/proc/self/root/<path>``, which ``resolve()`` does unify, so there the
test exercises the string path rather than the identity check. A real bind mount needs root or
a user namespace, so the identity branch is not exercised by a bind mount in this suite.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from coordinare.services.mounted_file import read_mounted_dir_names, read_mounted_file

_MAX_BYTES = 4096
_SECRET = "HOST SECRET\n"


def _second_path(path: Path) -> Path:
    """A second spelling of *path*, reaching the same directory (see the module docstring)."""
    firmlink = Path("/System/Volumes/Data")
    relative = str(path.resolve()).lstrip("/")
    if firmlink.is_dir():
        return firmlink / relative
    return Path("/proc/self/root") / relative


def _planted_link(tmp: Path) -> tuple[Path, Path, Path]:
    """A mount ``ws`` holding a performer-planted link to a host secret. Returns (ws, link, secret)."""
    secret = tmp / "host" / "secret.env"
    secret.parent.mkdir()
    secret.write_text(_SECRET)
    ws = tmp / "ws"
    ws.mkdir()
    link = ws / "planted.env"
    link.symlink_to(secret)
    return ws, link, secret


def test_symlinked_alias_of_a_mount_is_still_treated_as_the_mount(tmp_path: Path) -> None:
    ws, _link, secret = _planted_link(tmp_path)
    alias = tmp_path / "alias"
    alias.symlink_to(ws)

    assert read_mounted_file(alias / "planted.env", max_bytes=_MAX_BYTES, mounts=[ws]) is None
    assert secret.read_text() == _SECRET


def test_second_path_to_a_mount_is_treated_as_the_mount(tmp_path: Path) -> None:
    """A link planted inside a mount, reached through a second path, is refused (item 2).

    Before the identity check this read followed the link on macOS: the second path is not under
    the mount root by string, so the walk treated it as operator space.
    """
    ws, _link, secret = _planted_link(tmp_path)
    second = _second_path(ws) / "planted.env"

    assert read_mounted_file(second, max_bytes=_MAX_BYTES, mounts=[ws]) is None
    assert secret.read_text() == _SECRET


def test_second_path_to_a_mount_root_lists_no_planted_directory(tmp_path: Path) -> None:
    """The mount root reached through a second path is anchored: a link below it is not followed."""
    ws = tmp_path / "ws"
    host_dir = tmp_path / "host-dir"
    ws.mkdir()
    host_dir.mkdir()
    (host_dir / "inside.txt").write_text("x")
    (ws / "sub").symlink_to(host_dir)

    assert read_mounted_dir_names(_second_path(ws) / "sub", mounts=[ws]) is None


def test_the_walk_resolves_each_mount_root_once_per_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Item 3: roots are resolved once per call, not once per path component per root."""
    mounts = [tmp_path / f"mnt{i}" for i in range(7)]
    for mount in mounts:
        mount.mkdir()
    deep = tmp_path
    for i in range(9):
        deep = deep / f"d{i}"
    deep.mkdir(parents=True)
    target = deep / "file.txt"
    target.write_text("hello")

    calls: list[Path] = []
    real_resolve = Path.resolve

    def counting_resolve(self: Path, strict: bool = False) -> Path:
        calls.append(self)
        return real_resolve(self, strict=strict)

    monkeypatch.setattr(Path, "resolve", counting_resolve)
    assert read_mounted_file(target, max_bytes=_MAX_BYTES, mounts=mounts) == b"hello"

    assert len(calls) <= len(mounts)
