"""Fetch and unpack this carrier's own release packages without privileges, ready to build against.

    python3 -m coordinare_service_inference.environment.system_packages --into {cache}/deps PACKAGE...

The carrier has no Linux capabilities, so packages cannot be installed. apt can still download them without privileges,
and their contents are unpacked under ``--into``. Each step is one the build needs, in one command so none is dropped:
Dettmore e2e-57's authors were given the steps as a recipe and left out the relink, so OpenSSL's build linked the static
``libcrypto.a`` and failed on missing compression symbols, and the repair chased those symptoms for a generation.

1. ``apt-get update`` and ``apt-get install --download-only`` with the unprivileged settings, resolving dependencies
   against the carrier's installed packages, so only what the carrier lacks is fetched, at its own release.
2. ``dpkg-deb -x`` of each fetched package into ``--into``.
3. A ``-dev`` package's ``lib<name>.so`` link that dangles because the runtime library was not fetched (the carrier
   already has it) is pointed at the carrier's own copy.
4. Each unpacked pkg-config file's ``/usr`` paths are moved under ``--into``.
5. ``--into``/system-packages.env sets the compiler, linker (with an rpath, never LD_LIBRARY_PATH) and pkg-config paths.
   Source it in each command that builds against the packages.
"""
from __future__ import annotations

import argparse
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

ENV_FILE = "system-packages.env"
# The command the installation guidance and the static-rule correction give, with its placeholders.
COMMAND = "python3 -m coordinare_service_inference.environment.system_packages --into {cache}/deps <packages>"
# A Debian package name, optionally with an architecture or an exact version.
_PACKAGE = re.compile(r"[a-z0-9][a-z0-9+.-]+(:[a-z0-9-]+)?(=[A-Za-z0-9.+~:-]+)?")
_PKG_CONFIG_PATH_VARIABLE = re.compile(r"(?P<name>prefix|exec_prefix|libdir|includedir|sharedlibdir)=(?P<value>/usr(/.*)?)$")
_MACHINE_TRIPLETS = {"aarch64": "aarch64-linux-gnu", "arm64": "aarch64-linux-gnu", "x86_64": "x86_64-linux-gnu",
                     "amd64": "x86_64-linux-gnu"}


class SystemPackageError(RuntimeError):
    """A step failed; the message says which."""


def apt_options(apt_dir: Path) -> list[str]:
    """No drop to the _apt sandbox user (so no setgroups), no system locks, lists and downloads under ``apt_dir``."""
    return ["-o", "APT::Sandbox::User=root", "-o", "Debug::NoLocking=1", "-o", f"Dir::State={apt_dir}/state",
            "-o", "Dir::State::status=/var/lib/dpkg/status", "-o", f"Dir::Cache={apt_dir}/cache"]


# Debian container images clean /var/cache/apt after each update; without privileges that hook fails harmlessly
# ("rm: cannot remove ... Permission denied"), and the line is dropped so it is never mistaken for this fetch failing.
_IMAGE_CLEAN_HOOK = re.compile(r"rm: cannot remove '/var/cache/apt/.*': Permission denied")


def _run(argv: Sequence[str]) -> int:
    """Run one step; its output goes to stderr, so stdout carries only this command's summary."""
    done = subprocess.run(list(argv), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, check=False,
                          errors="replace")
    sys.stderr.writelines(line for line in done.stdout.splitlines(keepends=True)
                          if not _IMAGE_CLEAN_HOOK.fullmatch(line.rstrip("\n")))
    return done.returncode


def triplet() -> str:
    """The carrier's multiarch triplet, for example aarch64-linux-gnu."""
    for argv in (["dpkg-architecture", "-qDEB_HOST_MULTIARCH"], ["gcc", "-print-multiarch"]):
        try:
            found = subprocess.run(argv, capture_output=True, text=True, check=False, timeout=30).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            continue
        if found:
            return found
    machine = platform.machine().lower()
    if machine not in _MACHINE_TRIPLETS:
        raise SystemPackageError(f"cannot determine the multiarch triplet for machine {machine!r}")
    return _MACHINE_TRIPLETS[machine]


def relink_dangling(into: Path, library_dirs: Sequence[Path]) -> tuple[list[str], list[str]]:
    """Point each dangling link under ``into`` at the carrier's library of the same name; the relinked and the
    still-dangling shared-library links, relative to ``into``."""
    relinked: list[str] = []
    dangling: list[str] = []
    for path in sorted(into.rglob("*")):
        if not path.is_symlink() or path.exists():
            continue
        name = Path(os.readlink(path)).name
        target = next((directory / name for directory in library_dirs if (directory / name).exists()), None)
        if target is None:
            if ".so" in path.name:
                dangling.append(str(path.relative_to(into)))
            continue
        path.unlink()
        path.symlink_to(target)
        relinked.append(str(path.relative_to(into)))
    return relinked, dangling


def relocate_pkg_config(into: Path) -> list[str]:
    """Move each unpacked pkg-config file's absolute /usr paths under ``into``; the files changed."""
    changed: list[str] = []
    for path in sorted(into.glob("usr/**/pkgconfig/*.pc")):
        if path.is_symlink() or not path.is_file():
            continue
        lines = path.read_text(errors="surrogateescape").splitlines(keepends=True)
        updated = []
        for line in lines:
            match = _PKG_CONFIG_PATH_VARIABLE.match(line.rstrip("\r\n"))
            updated.append(f"{match['name']}={into}{match['value']}\n" if match else line)
        if updated != lines:
            path.write_text("".join(updated), errors="surrogateescape")
            changed.append(str(path.relative_to(into)))
    return changed


def environment_file(into: Path, triplet_name: str) -> str:
    """Shell exports pointing compilers, linkers and pkg-config at the unpacked packages, multiarch first."""
    usr = shlex.quote(str(into / "usr"))
    lib = f"{usr}/lib/{triplet_name}"
    include = f"{usr}/include"
    return "\n".join([
        "# Written by coordinare_service_inference.environment.system_packages. Source it in each command that",
        "# builds against these packages. Libraries load through the rpath, so nothing is needed at run time.",
        f'export CPATH="{include}/{triplet_name}:{include}${{CPATH:+:$CPATH}}"',
        f'export LIBRARY_PATH="{lib}:{usr}/lib${{LIBRARY_PATH:+:$LIBRARY_PATH}}"',
        f'export CPPFLAGS="-I{include}/{triplet_name} -I{include}${{CPPFLAGS:+ $CPPFLAGS}}"',
        f'export LDFLAGS="-L{lib} -L{usr}/lib -Wl,-rpath,{lib} -Wl,-rpath,{usr}/lib${{LDFLAGS:+ $LDFLAGS}}"',
        f'export PKG_CONFIG_PATH="{lib}/pkgconfig:{usr}/lib/pkgconfig:{usr}/share/pkgconfig'
        '${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}"',
        # Last, so a tool the packages add (pg_config, for example) never shadows one the carrier has.
        f'export PATH="$PATH:{usr}/bin"',
        "",
    ])


def fetch(packages: Sequence[str], into: Path, *, run: Callable[[Sequence[str]], int] = _run,
          triplet_name: str | None = None, library_root: Path = Path("/")) -> dict[str, Any]:
    """Fetch, unpack, relink and describe ``packages`` under ``into``; a summary of what was done."""
    if not packages:
        raise SystemPackageError("name at least one package")
    if bad := [name for name in packages if not _PACKAGE.fullmatch(name)]:
        raise SystemPackageError("not a Debian package name: " + ", ".join(bad))
    into = into.absolute()
    if ".." in into.parts or into.parts[1:2] == ("usr",):
        raise SystemPackageError("--into must be a plain directory outside /usr, normally {cache}/deps")
    apt_dir = into / ".apt"
    archives = apt_dir / "cache" / "archives"
    for directory in (apt_dir / "state" / "lists" / "partial", archives / "partial"):
        directory.mkdir(parents=True, exist_ok=True)
    options = apt_options(apt_dir)
    if run(["apt-get", *options, "update"]):
        raise SystemPackageError("apt-get update failed (output above)")
    # Archives a failed earlier call left are not this call's packages.
    for stale in archives.glob("*.deb"):
        stale.unlink()
    if run(["apt-get", *options, "install", "--download-only", "-y", "--no-install-recommends", *packages]):
        raise SystemPackageError("apt-get could not fetch the packages (output above): check each name exists in "
                                 "this carrier's release")
    debs = sorted(archives.glob("*.deb"))
    for deb in debs:
        if run(["dpkg-deb", "-x", str(deb), str(into)]):
            raise SystemPackageError(f"dpkg-deb could not unpack {deb.name}")
    for deb in debs:
        deb.unlink()
    name = triplet_name or triplet()
    library_dirs = [library_root / part for part in (f"usr/lib/{name}", f"lib/{name}", "usr/lib", "lib")]
    relinked, dangling = relink_dangling(into, library_dirs)
    relocated = relocate_pkg_config(into)
    env_path = into / ENV_FILE
    env_path.write_text(environment_file(into, name))
    return {"fetched": [deb.name for deb in debs], "relinked": relinked, "dangling": dangling,
            "pkg_config_relocated": relocated, "environment_file": str(env_path)}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m coordinare_service_inference.environment.system_packages",
                                     description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--into", type=Path, required=True, help="directory to unpack into, normally {cache}/deps")
    parser.add_argument("packages", nargs="+", help="Debian package names, for example libssl-dev zlib1g-dev")
    args = parser.parse_args(argv)
    if shutil.which("apt-get") is None or shutil.which("dpkg-deb") is None:
        sys.stderr.write("system_packages: this carrier has no apt-get or dpkg-deb\n")
        return 1
    try:
        summary = fetch(args.packages, args.into)
    except (SystemPackageError, OSError) as exc:
        sys.stderr.write(f"system_packages: {exc}\n")
        return 1
    sys.stdout.write(f"unpacked {len(summary['fetched'])} packages into {args.into}\n")
    for item in summary["dangling"]:
        sys.stderr.write(f"warning: {item} points at a library neither fetched nor on the carrier\n")
    sys.stdout.write(f"build with: . {shlex.quote(str(summary['environment_file']))}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
