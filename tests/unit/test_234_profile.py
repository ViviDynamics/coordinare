"""Spec 234: the image profile runs only what the daemon froze, never the cache's own scripts.

These run the real ``agent/performer/devenv-profile.sh`` as ``BASH_ENV`` of a non-interactive bash, the way the
image does. Every path the profile writes is redirected into ``tmp_path`` (``_DEVENV_LIB_BASE``,
``_DEVENV_SYSROOT``, ``_DEVENV_ROOT``, ``_DEVENV_PROVENANCE_DIR``), so a symlink step can never touch the host.
The cache is ``<root>/sym-abc``; the frozen copy is ``<provenance>/sym-abc``. Each attack plants its marker
in the cache and checks that the marker never appears.
"""

from __future__ import annotations

import io
import os
import subprocess
import tarfile
from pathlib import Path

PROFILE = Path(__file__).resolve().parents[2] / "agent" / "performer" / "devenv-profile.sh"
SLUG = "sym-abc"


def _ar(members: list[tuple[str, bytes]]) -> bytes:
    """A minimal ar archive, the format a .deb uses. Built in Python so no ar binary is needed."""
    out = bytearray(b"!<arch>\n")
    for name, data in members:
        out += f"{name:<16}{0:<12}{0:<6}{0:<6}{644:<8o}{len(data):<10}`\n".encode()
        out += data
        if len(data) % 2:
            out += b"\n"
    return bytes(out)


def _deb(lib_name: str) -> bytes:
    """A .deb whose data member carries one shared object named *lib_name*."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        payload = b"fake-so"
        info = tarfile.TarInfo(f"usr/lib/{lib_name}")
        info.size = len(payload)
        tar.addfile(info, io.BytesIO(payload))
    return _ar([("debian-binary", b"2.0\n"), ("data.tar", buf.getvalue())])


def _marker_script(marker: Path) -> str:
    return f"echo ran > {marker}\n"


def _layout(tmp_path: Path) -> dict[str, Path]:
    """Two trees: the cache the bootstrap wrote (hostile) and the daemon's frozen copy (trusted)."""
    root = tmp_path / "devenv"
    cache = root / SLUG
    (cache / "services").mkdir(parents=True)
    (cache / "debs").mkdir()
    prov = tmp_path / "provenance"
    frozen = prov / SLUG
    (frozen / "services").mkdir(parents=True)
    (frozen / "debs").mkdir()
    return {
        "root": root,
        "cache": cache,
        "prov": prov,
        "frozen": frozen,
        "lib_base": tmp_path / "libbase",
        "sysroot": tmp_path / "sysroot",
        "markers": tmp_path / "markers",
    }


def _env(paths: dict[str, Path], **extra: str) -> dict[str, str]:
    paths["markers"].mkdir(parents=True, exist_ok=True)
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(paths["root"].parent),
        "BASH_ENV": str(PROFILE),
        "_DEVENV_LIB_BASE": str(paths["lib_base"]),
        "_DEVENV_SYSROOT": str(paths["sysroot"]),
        "_DEVENV_ROOT": str(paths["root"]),
    }
    env.update(extra)
    return env


def _run(env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", "-c", "printf '%s' \"$LD_LIBRARY_PATH\""],
        capture_output=True, text=True, env=env, timeout=60, check=False,
    )


def test_swapped_cache_activate_is_not_sourced_by_a_consumer(tmp_path: Path) -> None:
    paths = _layout(tmp_path)
    frozen_marker = paths["markers"] / "frozen-activate"
    swapped_marker = paths["markers"] / "swapped-activate"
    (paths["frozen"] / "activate.sh").write_text(_marker_script(frozen_marker))
    (paths["cache"] / "activate.sh").write_text(_marker_script(swapped_marker))
    env = _env(paths, _DEVENV_PROVENANCE_DIR=str(paths["prov"]), _DEVENV_SKIP_SERVICES="1")
    _run(env)
    assert frozen_marker.exists(), "the daemon's frozen activate.sh is the one a consumer sources"
    assert not swapped_marker.exists(), "the cache's swapped activate.sh is never sourced"


def test_services_start_added_to_the_cache_is_not_run(tmp_path: Path) -> None:
    paths = _layout(tmp_path)
    frozen_marker = paths["markers"] / "frozen-services"
    planted_marker = paths["markers"] / "planted-services"
    (paths["frozen"] / "services" / "services-start.sh").write_text(_marker_script(frozen_marker))
    (paths["cache"] / "services" / "services-start.sh").write_text(_marker_script(planted_marker))
    env = _env(paths, _DEVENV_PROVENANCE_DIR=str(paths["prov"]))
    _run(env)
    assert frozen_marker.exists(), "the frozen services-start.sh is the one that runs"
    assert not planted_marker.exists(), "a services-start.sh added to the cache is never run"


def test_a_deb_outside_the_frozen_set_is_never_unpacked(tmp_path: Path) -> None:
    paths = _layout(tmp_path)
    (paths["frozen"] / "debs" / "libgood.deb").write_bytes(_deb("libgood.so.1"))
    (paths["cache"] / "debs" / "libevil.deb").write_bytes(_deb("libevil.so.1"))
    env = _env(paths, _DEVENV_PROVENANCE_DIR=str(paths["prov"]), _DEVENV_SKIP_SERVICES="1")
    _run(env)
    libdir = paths["lib_base"] / SLUG / "lib"
    assert (libdir / "libgood.so.1").exists(), "the frozen deb is unpacked"
    assert not (libdir / "libevil.so.1").exists(), "a deb planted in the cache is never unpacked"


def test_a_consumer_with_no_provenance_activates_nothing_from_the_cache(tmp_path: Path) -> None:
    paths = _layout(tmp_path)
    marker = paths["markers"] / "cache-activate"
    (paths["cache"] / "activate.sh").write_text(_marker_script(marker))
    (paths["cache"] / "services" / "services-start.sh").write_text(_marker_script(paths["markers"] / "svc"))
    env = _env(paths)
    _run(env)
    assert not marker.exists()
    assert not (paths["markers"] / "svc").exists()


def test_the_bootstrap_still_activates_its_own_cache(tmp_path: Path) -> None:
    paths = _layout(tmp_path)
    marker = paths["markers"] / "bootstrap-activate"
    (paths["cache"] / "activate.sh").write_text(_marker_script(marker))
    env = _env(paths, _DEVENV_BOOTSTRAP="1", _DEVENV_SKIP_SERVICES="1")
    _run(env)
    assert marker.exists(), "the env_bootstrap job reads the cache it writes"


def test_a_consumer_sources_the_frozen_activation_and_unpacks_its_frozen_debs(tmp_path: Path) -> None:
    paths = _layout(tmp_path)
    marker = paths["markers"] / "frozen-activate"
    (paths["frozen"] / "activate.sh").write_text(_marker_script(marker))
    (paths["frozen"] / "debs" / "libgood.deb").write_bytes(_deb("libgood.so.1"))
    env = _env(paths, _DEVENV_PROVENANCE_DIR=str(paths["prov"]), _DEVENV_SKIP_SERVICES="1")
    result = _run(env)
    assert marker.exists()
    libdir = str(paths["lib_base"] / SLUG / "lib")
    assert libdir in result.stdout, f"LD_LIBRARY_PATH carries the frozen lib dir: {result.stdout!r}"
    assert os.path.isdir(libdir)
