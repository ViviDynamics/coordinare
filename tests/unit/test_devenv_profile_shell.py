"""Shell-level tests for agent/performer/devenv-profile.sh.

The profile script must be safe to source across the three shell invocation
modes that agent CLI backends spawn (login bash, non-login bash, dash -c)
and must short-circuit on re-entry to avoid recursing when activate.sh runs
its own command substitutions.
"""
from __future__ import annotations

import io
import os
import shlex
import shutil
import subprocess
import tarfile
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PROFILE = REPO_ROOT / "agent" / "performer" / "devenv-profile.sh"


pytestmark = pytest.mark.skipif(
    not shutil.which("bash"), reason="bash required for these shell tests",
)

# Native-lib extraction tests need a tool to *build* a synthetic .deb (ar) and
# a tool to unpack its data member (tar). dpkg-deb is optional — the profile
# falls back to ar+tar, which is what the macOS dev host uses.
_DEB_TOOLS = bool(shutil.which("ar") and shutil.which("tar"))
requires_deb_tools = pytest.mark.skipif(
    not _DEB_TOOLS, reason="ar + tar required to build/extract synthetic .deb",
)

# US3: the profile must source cleanly under dash (the `sh` agent CLIs spawn).
_DASH = shutil.which("dash") or (
    "/bin/sh" if not (os.path.realpath("/bin/sh").endswith("bash")) else None
)
requires_dash = pytest.mark.skipif(
    not _DASH, reason="dash (or a non-bash /bin/sh) required for POSIX-sh tests",
)


def _run(cmd: list[str], env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd, capture_output=True, text=True, env=env, timeout=10, check=False,
    )


def _build_synthetic_deb(
    deb_path: Path,
    *,
    soname: str | None = "libfake.so.1",
    triplet: str = "aarch64-linux-gnu",
    corrupt: bool = False,
    symlink: str | None = None,
    extra_file: str | None = None,
    binary: str | None = None,
) -> None:
    """Build a minimal valid .deb at *deb_path* carrying one fake shared object.

    A .deb is an ``ar`` archive of (debian-binary, control.tar.gz, data.tar.gz).
    ``dpkg-deb -x`` and the ``ar p <deb> data.tar.* | tar -x`` fallback both
    extract the data member, so the same fixture exercises either code path.

    - *soname* names the real shared object placed under ``usr/lib/<triplet>/``.
      Pass ``None`` to build a deb that carries NO ``.so`` at all (only
      *extra_file*), used to assert the profile does not cache an empty libdir.
    - *symlink* (e.g. ``libfake.so``) adds a relative symlink in the same dir
      pointing at *soname*, mirroring Debian's ``libfoo.so -> libfoo.so.1``;
      used to assert the extractor follows symlinked shared objects.
    - *extra_file* (e.g. ``usr/share/doc/libfake/README``) adds a non-``.so``
      regular file, so a deb can carry payload without any shared object.
    - *binary* (e.g. ``usr/bin/foo``) adds an executable regular file (mode
      0o755), mirroring a deb that ships a CLI tool the runtime expects on
      PATH (e.g. chromium); used to assert the profile exposes captured
      executables, not just shared objects.

    When *corrupt* is set, the file is just garbage bytes (no valid ar archive),
    to assert the profile never aborts the shell on an unreadable package.
    """
    deb_path.parent.mkdir(parents=True, exist_ok=True)
    if corrupt:
        deb_path.write_bytes(b"not a real deb archive\x00\x01\x02")
        return

    def _targz(name: str, content: bytes) -> bytes:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            info = tarfile.TarInfo(name)
            info.size = len(content)
            info.mode = 0o644
            tf.addfile(info, io.BytesIO(content))
        return buf.getvalue()

    def _data_targz() -> bytes:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w:gz") as tf:
            if soname is not None:
                content = b"\x7fELF\x02\x01\x01 fake\n"
                info = tarfile.TarInfo(f"usr/lib/{triplet}/{soname}")
                info.size = len(content)
                info.mode = 0o644
                tf.addfile(info, io.BytesIO(content))
            if symlink is not None:
                link = tarfile.TarInfo(f"usr/lib/{triplet}/{symlink}")
                link.type = tarfile.SYMTYPE
                link.linkname = soname or ""  # relative, same directory
                tf.addfile(link)
            if extra_file is not None:
                content = b"not a shared object\n"
                info = tarfile.TarInfo(extra_file)
                info.size = len(content)
                info.mode = 0o644
                tf.addfile(info, io.BytesIO(content))
            if binary is not None:
                content = b"#!/bin/sh\necho fake-binary \"$@\"\n"
                info = tarfile.TarInfo(binary)
                info.size = len(content)
                info.mode = 0o755
                tf.addfile(info, io.BytesIO(content))
        return buf.getvalue()

    def _ar_member(name: str, data: bytes) -> bytes:
        # Portable common-format ar member header (60 bytes). Built by hand so
        # we don't depend on the host `ar` (BSD `ar` on macOS injects a
        # __.SYMDEF symbol table that breaks `ar t` listing of real members).
        header = "{:<16}{:<12}{:<6}{:<6}{:<8}{:<10}`\n".format(
            name, 0, 0, 0, "100644", len(data),
        ).encode()
        out = header + data
        if len(data) % 2:  # members are padded to an even byte boundary
            out += b"\n"
        return out

    data_tar = _data_targz()
    control_tar = _targz(
        "./control", b"Package: libfake\nVersion: 1.0\nArchitecture: arm64\n",
    )

    with open(deb_path, "wb") as fh:
        fh.write(b"!<arch>\n")
        fh.write(_ar_member("debian-binary", b"2.0\n"))
        fh.write(_ar_member("control.tar.gz", control_tar))
        fh.write(_ar_member("data.tar.gz", data_tar))


@pytest.fixture
def fake_cache(tmp_path: Path) -> SimpleNamespace:
    """Build a fake env cache tree: ``<tmp>/devenv/<slug>/{debs/*.deb, activate.sh}``.

    Returns a namespace describing the tree plus a writable ``lib_base`` tmpdir
    and the env the profile should be sourced with (``_DEVENV_LIB_BASE`` set,
    re-entry guard cleared).
    """
    if not _DEB_TOOLS:
        pytest.skip("ar + tar required to build synthetic .deb")
    slug = "website"
    soname = "libfake.so.1"
    triplet = "aarch64-linux-gnu"
    devenv = tmp_path / "devenv"
    cache = devenv / slug
    debs = cache / "debs"
    debs.mkdir(parents=True)
    _build_synthetic_deb(
        debs / "libfake_1.0_arm64.deb", soname=soname, triplet=triplet,
    )
    activate = cache / "activate.sh"
    activate.write_text("export DEVENV_ACTIVATED=1\n")

    lib_base = tmp_path / "libbase"
    env = {**os.environ, "_DEVENV_LIB_BASE": str(lib_base)}
    env.pop("_DEVENV_SOURCED", None)
    env.pop("LD_LIBRARY_PATH", None)

    return SimpleNamespace(
        devenv=devenv,
        slug=slug,
        cache=cache,
        debs=debs,
        soname=soname,
        triplet=triplet,
        lib_base=lib_base,
        libdir=lib_base / slug / "lib",
        sentinel=lib_base / slug / ".extracted",
        env=env,
    )


@pytest.fixture
def fake_devenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Create a fake /devenv-style tree the profile script can glob.

    The profile script hard-codes ``/devenv/*/activate.sh``, so we point it
    at a writable tmpdir by patching the glob via a wrapper script that
    re-executes the body with our path.
    """
    devenv = tmp_path / "devenv"
    (devenv / "sym").mkdir(parents=True)
    activate = devenv / "sym" / "activate.sh"
    activate.write_text(
        "export DEVENV_ACTIVATED=1\n"
        'export PATH="/fake/devenv/bin:$PATH"\n',
    )
    return devenv


def _patched_profile(tmp_path: Path, devenv_root: Path) -> Path:
    """Copy profile.sh and point its cache-root glob at the fixture.

    The profile globs cache dirs as ``"${_DEVENV_ROOT:-/devenv}"/*/`` (spec 117
    made the root overridable, mirroring ``_DEVENV_SYSROOT``/``_DEVENV_LIB_BASE``).
    We prepend an ``export _DEVENV_ROOT=<fixture>`` so the body globs the fixture
    instead of the real ``/devenv`` — no fragile text-replacement of the glob
    token. The writable lib base is redirected separately via ``_DEVENV_LIB_BASE``.
    """
    original = PROFILE.read_text()
    patched = f"export _DEVENV_ROOT={shlex.quote(str(devenv_root))}\n" + original
    out = tmp_path / "devenv-profile.sh"
    out.write_text(patched)
    return out


def test_profile_sources_under_bash_login(
    tmp_path: Path, fake_devenv: Path,
) -> None:
    """bash -lc reads /etc/profile.d/*; the profile must activate env."""
    profile = _patched_profile(tmp_path, fake_devenv)
    env = {**os.environ, "PATH": os.environ.get("PATH", "")}
    env.pop("_DEVENV_SOURCED", None)

    result = _run(
        ["bash", "-c", f". {profile} && echo SOURCED=$DEVENV_ACTIVATED && echo PATH=$PATH"],
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert "SOURCED=1" in result.stdout
    assert "/fake/devenv/bin" in result.stdout


def test_profile_sources_under_bash_env(
    tmp_path: Path, fake_devenv: Path,
) -> None:
    """Non-login bash honors $BASH_ENV; verify activation via that path."""
    profile = _patched_profile(tmp_path, fake_devenv)
    env = {**os.environ, "BASH_ENV": str(profile)}
    env.pop("_DEVENV_SOURCED", None)

    result = _run(
        ["bash", "-c", "echo SOURCED=$DEVENV_ACTIVATED"],
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert "SOURCED=1" in result.stdout


def test_profile_skips_services_start_when_flag_set(
    tmp_path: Path, fake_devenv: Path,
) -> None:
    """spec 120: `_DEVENV_SKIP_SERVICES=1` skips the spec-117 services-start block
    while STILL activating the env. `_activate_env_cache` sets this so it captures
    the toolchain (PATH/LD_LIBRARY_PATH) without paying the slow postgres `initdb`
    that otherwise blew its timeout. Other shells (no flag) keep starting services.
    """
    svc_dir = fake_devenv / "sym" / "services"
    svc_dir.mkdir(parents=True)
    marker = tmp_path / "services-ran.marker"
    (svc_dir / "services-start.sh").write_text(
        f"#!/usr/bin/env bash\ntouch {shlex.quote(str(marker))}\n",
    )
    profile = _patched_profile(tmp_path, fake_devenv)
    base_env = {
        **os.environ,
        "BASH_ENV": str(profile),
        "_DEVENV_LIB_BASE": str(tmp_path / "libbase"),
    }
    base_env.pop("_DEVENV_SOURCED", None)

    # Without the flag: env activates AND services-start runs.
    r1 = _run(["bash", "-c", "echo SOURCED=$DEVENV_ACTIVATED"], env=dict(base_env))
    assert "SOURCED=1" in r1.stdout, r1.stderr
    assert marker.exists(), "services-start should run without the skip flag"

    marker.unlink()

    # With the flag: env still activates, services-start is skipped.
    env2 = {**base_env, "_DEVENV_SKIP_SERVICES": "1"}
    r2 = _run(["bash", "-c", "echo SOURCED=$DEVENV_ACTIVATED"], env=env2)
    assert "SOURCED=1" in r2.stdout, r2.stderr
    assert not marker.exists(), (
        "services-start must be skipped when _DEVENV_SKIP_SERVICES=1"
    )


def test_profile_reentry_guard_prevents_recursion(
    tmp_path: Path, fake_devenv: Path,
) -> None:
    """When activate.sh itself runs a subshell (e.g. rbenv init), bash will
    re-source BASH_ENV.  The exported _DEVENV_SOURCED sentinel must short
    out the second pass so we don't recurse infinitely."""
    devenv = fake_devenv
    # Replace activate.sh with one that runs a command substitution — this
    # mimics ``eval "$(rbenv init - bash)"`` which is the production trigger.
    activate = devenv / "sym" / "activate.sh"
    activate.write_text(
        "export DEVENV_ACTIVATED=1\n"
        # Command substitution: bash spawns a subshell which re-reads BASH_ENV
        'export REENTRY_CHECK="$(echo inner)"\n',
    )
    profile = _patched_profile(tmp_path, devenv)
    env = {**os.environ, "BASH_ENV": str(profile)}
    env.pop("_DEVENV_SOURCED", None)

    result = _run(
        ["bash", "-c", "echo OK=$DEVENV_ACTIVATED REENTRY=$REENTRY_CHECK"],
        env=env,
        # If recursion happened, the subprocess would hang and timeout.
    )

    assert result.returncode == 0, result.stderr
    assert "OK=1" in result.stdout
    assert "REENTRY=inner" in result.stdout


def test_profile_no_op_when_no_activate_files(tmp_path: Path) -> None:
    """Empty /devenv dir must not error — the glob simply matches nothing."""
    empty = tmp_path / "empty-devenv"
    empty.mkdir()
    profile = _patched_profile(tmp_path, empty)
    env = {**os.environ}
    env.pop("_DEVENV_SOURCED", None)

    result = _run(
        ["bash", "-c", f". {profile} && echo OK"],
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


def test_profile_sentinel_skips_subsequent_sources(
    tmp_path: Path, fake_devenv: Path,
) -> None:
    """If _DEVENV_SOURCED is already set when the profile runs, it must
    return without touching the environment a second time."""
    profile = _patched_profile(tmp_path, fake_devenv)
    env = {**os.environ, "_DEVENV_SOURCED": "1"}

    result = _run(
        ["bash", "-c", f". {profile} && echo DA=${{DEVENV_ACTIVATED:-unset}}"],
        env=env,
    )

    assert result.returncode == 0, result.stderr
    # Activate.sh should not have run, so DEVENV_ACTIVATED stays unset.
    assert "DA=unset" in result.stdout


# ---------------------------------------------------------------------------
# User Story 1: native libs loadable from captured debs (deterministic layer)
# ---------------------------------------------------------------------------


@requires_deb_tools
def test_profile_extracts_debs_and_sets_ld_library_path(
    tmp_path: Path, fake_cache: SimpleNamespace,
) -> None:
    """Contract C1: a cache with a .so-bearing .deb gets its shared object
    extracted to <lib_base>/<slug>/lib and that dir prepended to
    LD_LIBRARY_PATH."""
    profile = _patched_profile(tmp_path, fake_cache.devenv)

    result = _run(
        ["bash", "-c", f". {profile} && echo LLP=$LD_LIBRARY_PATH"],
        env=fake_cache.env,
    )

    assert result.returncode == 0, result.stderr
    extracted = fake_cache.libdir / fake_cache.soname
    assert extracted.is_file(), (
        f"expected {extracted} to exist; tree:\n"
        + "\n".join(str(p) for p in fake_cache.lib_base.rglob("*"))
    )
    assert f"LLP={fake_cache.libdir}" in result.stdout or (
        f"LLP={fake_cache.libdir}:" in result.stdout
    )
    assert str(fake_cache.libdir) in result.stdout


@requires_deb_tools
def test_profile_skips_extraction_when_sentinel_present(
    tmp_path: Path, fake_cache: SimpleNamespace,
) -> None:
    """Contract C3: with the sentinel already present, extraction does NOT
    re-run (a shimmed dpkg-deb/ar would leave a marker) yet the lib dir is
    still placed on LD_LIBRARY_PATH."""
    profile = _patched_profile(tmp_path, fake_cache.devenv)

    # Pre-create a published lib dir + sentinel, but NO extracted .so, so we
    # can detect whether extraction re-ran (it must not).
    fake_cache.libdir.mkdir(parents=True)
    (fake_cache.libdir / "preexisting.so").write_bytes(b"x")
    fake_cache.sentinel.write_text("")

    # Shim ar/dpkg-deb on PATH to drop a marker if extraction is attempted.
    shimdir = tmp_path / "shimbin"
    shimdir.mkdir()
    marker = tmp_path / "extraction_ran"
    for tool in ("ar", "dpkg-deb"):
        shim = shimdir / tool
        shim.write_text(f'#!/bin/sh\necho ran >> "{marker}"\nexit 0\n')
        shim.chmod(0o755)

    env = {**fake_cache.env, "PATH": f"{shimdir}:{fake_cache.env['PATH']}"}
    result = _run(
        ["bash", "-c", f". {profile} && echo LLP=$LD_LIBRARY_PATH"],
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert not marker.exists(), "extraction re-ran despite sentinel present"
    assert str(fake_cache.libdir) in result.stdout


@requires_deb_tools
def test_profile_no_debs_dir_leaves_ld_library_path_unchanged(
    tmp_path: Path, fake_cache: SimpleNamespace,
) -> None:
    """Contract C4: a cache with no debs/ dir adds no lib entry and starts
    cleanly."""
    shutil.rmtree(fake_cache.debs)  # remove the debs/ dir entirely
    profile = _patched_profile(tmp_path, fake_cache.devenv)

    env = {**fake_cache.env, "LD_LIBRARY_PATH": "/preexisting/only"}
    result = _run(
        ["bash", "-c", f". {profile} && echo LLP=$LD_LIBRARY_PATH"],
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert str(fake_cache.libdir) not in result.stdout
    assert "LLP=/preexisting/only" in result.stdout


@requires_deb_tools
def test_profile_preserves_existing_ld_library_path(
    tmp_path: Path, fake_cache: SimpleNamespace,
) -> None:
    """The new lib dir is prepended; a pre-existing LD_LIBRARY_PATH is kept."""
    profile = _patched_profile(tmp_path, fake_cache.devenv)

    env = {**fake_cache.env, "LD_LIBRARY_PATH": "/opt/preexisting/lib"}
    result = _run(
        ["bash", "-c", f". {profile} && echo LLP=$LD_LIBRARY_PATH"],
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert "/opt/preexisting/lib" in result.stdout
    assert f"LLP={fake_cache.libdir}:/opt/preexisting/lib" in result.stdout


# ---------------------------------------------------------------------------
# User Story 2: activation independent of (and ordered before) activate.sh
# ---------------------------------------------------------------------------


@requires_deb_tools
def test_ld_library_path_set_before_activate_sh_sourced(
    tmp_path: Path, fake_cache: SimpleNamespace,
) -> None:
    """Contract C2: the lib dir is already on LD_LIBRARY_PATH at the moment a
    no-deb-handling activate.sh is sourced (deterministic step runs first)."""
    capture = tmp_path / "llp_at_activate.txt"
    # Today's verbatim shape: activate.sh does NO deb/LD_LIBRARY_PATH handling.
    (fake_cache.cache / "activate.sh").write_text(
        f'printf "%s" "$LD_LIBRARY_PATH" > "{capture}"\n'
        "export DEVENV_ACTIVATED=1\n",
    )
    profile = _patched_profile(tmp_path, fake_cache.devenv)

    result = _run(["bash", "-c", f". {profile} && echo OK"], env=fake_cache.env)

    assert result.returncode == 0, result.stderr
    assert capture.is_file(), "activate.sh did not run"
    assert str(fake_cache.libdir) in capture.read_text()


@requires_deb_tools
def test_activate_sh_appending_ld_library_path_is_well_formed(
    tmp_path: Path, fake_cache: SimpleNamespace,
) -> None:
    """Contract C7: when activate.sh ALSO appends to LD_LIBRARY_PATH, the final
    value contains both our lib dir and theirs and is not malformed."""
    (fake_cache.cache / "activate.sh").write_text(
        'export LD_LIBRARY_PATH="${LD_LIBRARY_PATH:+$LD_LIBRARY_PATH:}/opt/their/lib"\n',
    )
    profile = _patched_profile(tmp_path, fake_cache.devenv)

    result = _run(
        ["bash", "-c", f". {profile} && echo LLP=$LD_LIBRARY_PATH"],
        env=fake_cache.env,
    )

    assert result.returncode == 0, result.stderr
    assert str(fake_cache.libdir) in result.stdout
    assert "/opt/their/lib" in result.stdout
    # No empty path segments (leading/trailing/double colon).
    line = next(ln for ln in result.stdout.splitlines() if ln.startswith("LLP="))
    value = line[len("LLP=") :]
    assert "::" not in value and not value.startswith(":") and not value.endswith(":")


# ---------------------------------------------------------------------------
# User Story 3: non-interactive `sh -c` (dash) sees the activated environment
# ---------------------------------------------------------------------------


@requires_deb_tools
@requires_dash
def test_dash_sh_c_sees_activated_environment(
    tmp_path: Path, fake_cache: SimpleNamespace,
) -> None:
    """Contract C8: sourcing the profile under dash runs the deterministic
    extraction with no bashism syntax errors and the lib dir lands on
    LD_LIBRARY_PATH (POSIX-sh safety)."""
    profile = _patched_profile(tmp_path, fake_cache.devenv)

    result = _run(
        [_DASH, "-c", f". {profile} && echo LLP=$LD_LIBRARY_PATH"],
        env=fake_cache.env,
    )

    assert result.returncode == 0, result.stderr
    # No dash parse/runtime complaints leaked to stderr.
    assert "Syntax error" not in result.stderr
    assert "not found" not in result.stderr or "ar:" in result.stderr
    extracted = fake_cache.libdir / fake_cache.soname
    assert extracted.is_file(), result.stderr
    assert str(fake_cache.libdir) in result.stdout


@requires_deb_tools
@requires_dash
def test_sh_c_inherits_activated_env_from_parent(
    tmp_path: Path, fake_cache: SimpleNamespace,
) -> None:
    """Contract C8 (practical guarantee): a parent that sourced the profile
    exports LD_LIBRARY_PATH; a child `sh -c` inherits it unchanged, so the
    cross-shell guarantee holds even where dash would not auto-source $ENV."""
    profile = _patched_profile(tmp_path, fake_cache.devenv)

    # Parent bash sources the profile, then hands off to a child `sh -c` that
    # only echoes the inherited value (it does NOT re-source anything).
    result = _run(
        [
            "bash",
            "-c",
            f". {profile} && {_DASH} -c 'echo LLP=$LD_LIBRARY_PATH'",
        ],
        env=fake_cache.env,
    )

    assert result.returncode == 0, result.stderr
    assert str(fake_cache.libdir) in result.stdout


# ---------------------------------------------------------------------------
# Polish: safety + concurrency regressions
# ---------------------------------------------------------------------------


@requires_deb_tools
def test_profile_never_aborts_on_corrupt_deb(
    tmp_path: Path, fake_cache: SimpleNamespace,
) -> None:
    """Contract C5: a corrupt/unreadable .deb must not abort the shell — the
    profile sources cleanly and the cache's activate.sh still runs, so the
    command following the source still executes."""
    for deb in fake_cache.debs.glob("*.deb"):
        deb.unlink()
    _build_synthetic_deb(fake_cache.debs / "broken_1.0_arm64.deb", corrupt=True)
    profile = _patched_profile(tmp_path, fake_cache.devenv)

    result = _run(
        ["bash", "-c", f". {profile} && echo STILL_ALIVE=$DEVENV_ACTIVATED"],
        env=fake_cache.env,
    )

    assert result.returncode == 0, result.stderr
    assert "STILL_ALIVE=1" in result.stdout


def test_profile_does_not_enable_set_e_or_exit() -> None:
    """Contract C9: a file sourced into every shell must never enable
    `set -e`/`set -u` or call `exit`, which would kill the caller's shell."""
    lines = [ln.split("#", 1)[0].strip() for ln in PROFILE.read_text().splitlines()]
    forbidden = ("set -e", "set -u", "set -eu", "set -ue")
    for ln in lines:
        for bad in forbidden:
            assert bad not in ln, f"forbidden `{bad}` in profile: {ln!r}"
        assert ln != "exit" and not ln.startswith("exit "), (
            f"forbidden `exit` in profile: {ln!r}"
        )


@requires_deb_tools
def test_profile_partial_lib_dir_without_sentinel_reextracts(
    tmp_path: Path, fake_cache: SimpleNamespace,
) -> None:
    """FR-009: a lib dir left from an interrupted publish (NO `.extracted`
    sentinel) is never treated as complete — sourcing re-runs extraction and
    publishes the sentinel, replacing the partial dir with the real .so."""
    fake_cache.libdir.mkdir(parents=True)
    (fake_cache.libdir / "partial.junk").write_bytes(b"stale")
    assert not fake_cache.sentinel.exists()
    profile = _patched_profile(tmp_path, fake_cache.devenv)

    result = _run(
        ["bash", "-c", f". {profile} && echo LLP=$LD_LIBRARY_PATH"],
        env=fake_cache.env,
    )

    assert result.returncode == 0, result.stderr
    assert (fake_cache.libdir / fake_cache.soname).is_file(), result.stderr
    assert fake_cache.sentinel.is_file(), "sentinel not published after re-extract"
    assert not (fake_cache.libdir / "partial.junk").exists(), (
        "stale partial file survived re-extraction"
    )
    assert str(fake_cache.libdir) in result.stdout


@requires_deb_tools
def test_profile_no_sentinel_or_libdir_when_no_so_extracted(
    tmp_path: Path, fake_cache: SimpleNamespace,
) -> None:
    """MEDIUM (adversarial review): a deb carrying NO shared object must not
    be cached as 'complete' — no `.extracted` sentinel is written and no empty
    lib dir is published / prepended. Otherwise an incomplete extraction (zero
    .so found, or silent cp failures) gets permanently sealed by the sentinel
    and the runtime never gets its libraries."""
    for deb in fake_cache.debs.glob("*.deb"):
        deb.unlink()
    _build_synthetic_deb(
        fake_cache.debs / "docsonly_1.0_arm64.deb",
        soname=None,
        extra_file="usr/share/doc/libfake/README",
    )
    profile = _patched_profile(tmp_path, fake_cache.devenv)

    result = _run(
        ["bash", "-c", f". {profile} && echo LLP=$LD_LIBRARY_PATH"],
        env=fake_cache.env,
    )

    assert result.returncode == 0, result.stderr
    assert not fake_cache.sentinel.exists(), (
        "sentinel written despite zero shared objects extracted"
    )
    assert not fake_cache.libdir.exists(), (
        "empty lib dir published despite zero shared objects extracted"
    )
    assert str(fake_cache.libdir) not in result.stdout


@requires_deb_tools
def test_profile_reextracts_when_sentinel_present_but_libdir_missing(
    tmp_path: Path, fake_cache: SimpleNamespace,
) -> None:
    """CRITICAL (adversarial review): if a prior publish lost the lib dir (e.g.
    `rm -rf libdir` succeeded but the subsequent `mv` failed) the sentinel may
    survive with no lib dir. The profile must NOT treat the stale sentinel as
    authoritative — it must re-extract and republish, otherwise every shell is
    deadlocked without its libraries forever."""
    # Sentinel present, but the published lib dir is gone.
    fake_cache.sentinel.parent.mkdir(parents=True, exist_ok=True)
    fake_cache.sentinel.write_text("")
    assert not fake_cache.libdir.exists()
    profile = _patched_profile(tmp_path, fake_cache.devenv)

    result = _run(
        ["bash", "-c", f". {profile} && echo LLP=$LD_LIBRARY_PATH"],
        env=fake_cache.env,
    )

    assert result.returncode == 0, result.stderr
    assert (fake_cache.libdir / fake_cache.soname).is_file(), (
        "lib dir not rebuilt despite stale sentinel; tree:\n"
        + "\n".join(str(p) for p in fake_cache.lib_base.rglob("*"))
    )
    assert fake_cache.sentinel.is_file()
    assert str(fake_cache.libdir) in result.stdout


@requires_deb_tools
def test_profile_extracts_symlinked_shared_object(
    tmp_path: Path, fake_cache: SimpleNamespace,
) -> None:
    """MEDIUM (adversarial review): Debian ships dev/runtime sonames as symlinks
    (``libssl.so -> libssl.so.3``). The extractor must follow symlinked shared
    objects, not skip them with a bare ``-type f`` test, or a runtime that links
    against the unversioned name fails to load."""
    for deb in fake_cache.debs.glob("*.deb"):
        deb.unlink()
    _build_synthetic_deb(
        fake_cache.debs / "libfake_1.0_arm64.deb",
        soname="libfake.so.1",
        symlink="libfake.so",
        triplet=fake_cache.triplet,
    )
    profile = _patched_profile(tmp_path, fake_cache.devenv)

    result = _run(
        ["bash", "-c", f". {profile} && echo LLP=$LD_LIBRARY_PATH"],
        env=fake_cache.env,
    )

    assert result.returncode == 0, result.stderr
    assert (fake_cache.libdir / "libfake.so.1").is_file(), result.stderr
    linked = fake_cache.libdir / "libfake.so"
    assert linked.exists(), (
        "symlinked shared object was skipped (bare -type f);\n"
        + "\n".join(str(p) for p in fake_cache.lib_base.rglob("*"))
    )
    assert str(fake_cache.libdir) in result.stdout


@requires_deb_tools
def test_profile_reclaims_stale_lock_and_extracts(
    tmp_path: Path, fake_cache: SimpleNamespace,
) -> None:
    """CRITICAL (adversarial review, round 2): the per-slug `mkdir` lock has no
    staleness recovery. A shell crashing (OOM/SIGKILL) between acquiring the
    lock (`mkdir .lock`) and releasing it (`rmdir .lock`) leaves a permanent
    `.lock` dir, so every subsequent shell's `mkdir .lock` fails and extraction
    DEADLOCKS FOREVER — the runtime never gets its libraries. An aged lock dir
    must be presumed abandoned and reclaimed so extraction proceeds."""
    slugdir = fake_cache.lib_base / fake_cache.slug
    slugdir.mkdir(parents=True)
    lock = slugdir / ".lock"
    lock.mkdir()
    # Backdate the lock well past any sane extraction window (1 hour).
    old = time.time() - 3600
    os.utime(lock, (old, old))
    assert not fake_cache.libdir.exists()

    profile = _patched_profile(tmp_path, fake_cache.devenv)
    result = _run(
        ["bash", "-c", f". {profile} && echo LLP=$LD_LIBRARY_PATH"],
        env=fake_cache.env,
    )

    assert result.returncode == 0, result.stderr
    assert (fake_cache.libdir / fake_cache.soname).is_file(), (
        "stale lock was not reclaimed; extraction deadlocked. tree:\n"
        + "\n".join(str(p) for p in fake_cache.lib_base.rglob("*"))
    )
    assert str(fake_cache.libdir) in result.stdout


@requires_deb_tools
def test_profile_warns_when_lock_held_and_no_libdir(
    tmp_path: Path, fake_cache: SimpleNamespace,
) -> None:
    """MEDIUM (adversarial review, round 2): when another shell currently holds
    the (fresh) extraction lock and no lib dir is published yet, the profile
    must not silently yield a runtime with no libraries — it emits a stderr
    warning so the missing-library condition is diagnosable, and it does not
    attempt to extract (the lock holder owns that)."""
    slugdir = fake_cache.lib_base / fake_cache.slug
    slugdir.mkdir(parents=True)
    lock = slugdir / ".lock"
    lock.mkdir()  # fresh mtime → presumed actively held by a live shell
    assert not fake_cache.libdir.exists()

    profile = _patched_profile(tmp_path, fake_cache.devenv)
    # 397: a lock loser now WAITS for the holder to publish (see the two tests
    # below). This test is about the warning once waiting is exhausted, so
    # give it no wait at all.
    result = _run(
        ["bash", "-c", f". {profile} && echo done"],
        env={**fake_cache.env, "_DEVENV_LOCK_WAIT_S": "0"},
    )

    assert result.returncode == 0, result.stderr
    assert not fake_cache.libdir.exists(), (
        "extraction ran despite a freshly-held lock owned by another shell"
    )
    stderr_low = result.stderr.lower()
    assert "devenv" in stderr_low and fake_cache.slug in result.stderr, (
        f"expected a stderr warning naming the slug; got: {result.stderr!r}"
    )


# ---------------------------------------------------------------------------
# User Story 4: captured executables are exposed on PATH (deb binaries, not
# just .so files). Symmetric to the LD_LIBRARY_PATH handling: the profile
# extracts the full deb tree to a persistent per-cache prefix root and
# shallow-symlinks captured FHS subtrees (usr/bin, usr/lib, etc) into a
# sysroot (real `/` in production, a tmp dir under test via _DEVENV_SYSROOT),
# only when the target does not already exist (never clobbering image files).
# This is what puts e.g. chromium on PATH so QA screenshots work.
# ---------------------------------------------------------------------------


@requires_deb_tools
def test_profile_symlinks_captured_executable_into_sysroot(
    tmp_path: Path, fake_cache: SimpleNamespace,
) -> None:
    """A deb that ships an executable (``usr/bin/foo``) must have that binary
    exposed under the sysroot at ``<sysroot>/usr/bin/foo`` as a symlink into the
    published per-cache prefix root, so the runtime finds it on PATH (the real
    sysroot is ``/`` whose ``usr/bin`` is already on PATH). Symmetric to the
    existing ``.so`` → LD_LIBRARY_PATH handling."""
    for deb in fake_cache.debs.glob("*.deb"):
        deb.unlink()
    _build_synthetic_deb(
        fake_cache.debs / "footool_1.0_arm64.deb",
        soname="libfake.so.1",
        triplet=fake_cache.triplet,
        binary="usr/bin/footool",
    )
    sysroot = tmp_path / "sysroot"
    (sysroot / "usr" / "bin").mkdir(parents=True)
    profile = _patched_profile(tmp_path, fake_cache.devenv)

    env = {**fake_cache.env, "_DEVENV_SYSROOT": str(sysroot)}
    result = _run(
        ["bash", "-c", f". {profile} && echo OK"],
        env=env,
    )

    assert result.returncode == 0, result.stderr
    exposed = sysroot / "usr" / "bin" / "footool"
    assert exposed.is_symlink(), (
        f"expected {exposed} to be a symlink into the prefix root; tree:\n"
        + "\n".join(str(p) for p in sysroot.rglob("*"))
        + "\n--- lib_base:\n"
        + "\n".join(str(p) for p in fake_cache.lib_base.rglob("*"))
    )
    # The symlink must resolve to a real, executable file (the captured deb
    # binary in the persistent prefix root), so exec'ing it actually works.
    assert exposed.resolve().is_file(), "symlink target is not a real file"
    assert os.access(exposed.resolve(), os.X_OK), "exposed binary is not executable"


@requires_deb_tools
def test_profile_does_not_clobber_existing_sysroot_entry(
    tmp_path: Path, fake_cache: SimpleNamespace,
) -> None:
    """The shallow-symlink step must NEVER overwrite a path that already exists
    in the sysroot (image-provided files win). A pre-existing
    ``<sysroot>/usr/bin/footool`` is left exactly as-is."""
    for deb in fake_cache.debs.glob("*.deb"):
        deb.unlink()
    _build_synthetic_deb(
        fake_cache.debs / "footool_1.0_arm64.deb",
        soname="libfake.so.1",
        triplet=fake_cache.triplet,
        binary="usr/bin/footool",
    )
    sysroot = tmp_path / "sysroot"
    (sysroot / "usr" / "bin").mkdir(parents=True)
    preexisting = sysroot / "usr" / "bin" / "footool"
    preexisting.write_text("#!/bin/sh\necho IMAGE_PROVIDED\n")
    preexisting.chmod(0o755)
    profile = _patched_profile(tmp_path, fake_cache.devenv)

    env = {**fake_cache.env, "_DEVENV_SYSROOT": str(sysroot)}
    result = _run(
        ["bash", "-c", f". {profile} && echo OK"],
        env=env,
    )

    assert result.returncode == 0, result.stderr
    assert not preexisting.is_symlink(), "pre-existing image file was clobbered"
    assert "IMAGE_PROVIDED" in preexisting.read_text(), (
        "pre-existing image file content was overwritten"
    )


@requires_deb_tools
def test_profile_waits_for_a_held_lock_then_uses_the_published_libs(
    tmp_path: Path, fake_cache: SimpleNamespace,
) -> None:
    """397: a shell that loses the extraction lock must WAIT for the holder to
    publish, not proceed without libraries.

    Production sequence that motivated this: ``_activate_env_cache`` held the
    lock while extracting (36-110s); ``_start_env_cache_services`` sourced the
    profile 30s in, found the lock, printed a warning and launched postgres with
    no ``LD_LIBRARY_PATH``. postgres needs libicu from the extracted libs and
    cannot start; the card was held 180s later.

    MUTATION: remove the wait loop in the profile's lock-loser branch. This test
    then observes the warning and an empty LD_LIBRARY_PATH.
    """
    slugdir = fake_cache.lib_base / fake_cache.slug
    slugdir.mkdir(parents=True)
    lock = slugdir / ".lock"
    lock.mkdir()  # fresh: presumed actively held
    profile = _patched_profile(tmp_path, fake_cache.devenv)

    env = {**fake_cache.env, "_DEVENV_LOCK_WAIT_S": "8"}
    proc = subprocess.Popen(
        ["bash", "-c", f". {profile} && echo LLP=$LD_LIBRARY_PATH"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env,
    )
    # Play the lock holder: publish the lib dir + sentinel, then release.
    time.sleep(1.5)
    fake_cache.libdir.mkdir(parents=True)
    (fake_cache.libdir / fake_cache.soname).write_bytes(b"\x7fELF")
    fake_cache.sentinel.touch()
    lock.rmdir()
    out, err = proc.communicate(timeout=15)

    assert proc.returncode == 0, err
    assert f"LLP={fake_cache.libdir}" in out or str(fake_cache.libdir) in out, (
        f"the waiting shell did not pick up the published lib dir; stdout={out!r} stderr={err!r}"
    )
    assert "is locked" not in err, (
        "the shell warned as if it had given up, but the holder published in time"
    )


@requires_deb_tools
def test_profile_gives_up_waiting_at_the_ceiling_and_warns(
    tmp_path: Path, fake_cache: SimpleNamespace,
) -> None:
    """397: the wait is bounded. A holder that never publishes must not hang
    every shell in the container; after the ceiling the existing warning fires
    and the shell continues (the profile must never abort its caller)."""
    slugdir = fake_cache.lib_base / fake_cache.slug
    slugdir.mkdir(parents=True)
    (slugdir / ".lock").mkdir()
    profile = _patched_profile(tmp_path, fake_cache.devenv)

    started = time.monotonic()
    result = _run(
        ["bash", "-c", f". {profile} && echo LLP=$LD_LIBRARY_PATH"],
        env={**fake_cache.env, "_DEVENV_LOCK_WAIT_S": "2"},
    )
    elapsed = time.monotonic() - started

    assert result.returncode == 0, result.stderr
    assert 1.5 <= elapsed < 8, f"expected ~2s of waiting, took {elapsed:.1f}s"
    assert "is locked" in result.stderr and "waited" in result.stderr, (
        f"after the ceiling the warning must fire and say it waited; got {result.stderr!r}"
    )
    assert not fake_cache.libdir.exists()


def test_lock_wait_ceiling_keeps_a_lock_loss_inside_the_services_outer_cap() -> None:
    """397: the default wait ceiling is sized against two other numbers. A shell
    that gives up waiting proceeds to run services-start, whose postgres
    readiness loop is 180s (the services-start templater); the performer kills
    the whole script at ``_SERVICES_START_TIMEOUT_S``. The wait plus the
    readiness window must fit inside that cap, or a genuine lock loss stops
    surfacing as this profile's diagnosable warning and becomes a bare
    "services-start timed out after 300s". 150s was the first draft and did
    not fit (330s)."""
    import re

    from performer.workspace import _SERVICES_START_TIMEOUT_S

    m = re.search(r'_DEVENV_LOCK_WAIT_S:-(\d+)', PROFILE.read_text())
    assert m, "the profile no longer declares a default lock-wait ceiling"
    ceiling = int(m.group(1))
    # 397 (review): the readiness window is a literal in the services-start
    # template, not a constant. Read it from there, the way this test reads the
    # ceiling from the profile, so a change to either side moves this check.
    template = (
        REPO_ROOT
        / "packages" / "service_inference" / "src" / "coordinare_service_inference"
        / "templates" / "services-start.sh.j2"
    )
    waits = re.findall(r'while \[ "\$_pg_wait" -lt (\d+) \]', template.read_text())
    assert waits, f"no postgres readiness loop found in {template}"
    postgres_readiness_s = max(int(w) for w in waits)
    assert ceiling + postgres_readiness_s < _SERVICES_START_TIMEOUT_S, (
        f"wait {ceiling}s + readiness {postgres_readiness_s}s must stay under the "
        f"{_SERVICES_START_TIMEOUT_S:.0f}s outer cap"
    )
    assert ceiling >= 60, "the ceiling must still cover an uncontended extraction (~36s) with headroom"


@requires_deb_tools
def test_lock_wait_ends_when_the_sentinel_appears_even_if_the_lock_is_still_held(
    tmp_path: Path, fake_cache: SimpleNamespace,
) -> None:
    """397 (review): the wait loop has three exit conditions and the first
    waiting test satisfied two of them at once, so removing either check still
    passed. This pins the SENTINEL condition alone: the holder writes the
    sentinel last and only then releases the lock, so there is a real window
    where the libs are published but the lock dir still exists. A waiter must
    proceed in that window, not sit until the lock goes away.

    MUTATION: drop ``[ ! -f "$_devenv_marker" ]`` from the while condition.
    The waiter then holds until the 8s ceiling and this test's 5s bound fails.
    """
    slugdir = fake_cache.lib_base / fake_cache.slug
    slugdir.mkdir(parents=True)
    lock = slugdir / ".lock"
    lock.mkdir()
    profile = _patched_profile(tmp_path, fake_cache.devenv)

    started = time.monotonic()
    proc = subprocess.Popen(
        ["bash", "-c", f". {profile} && echo LLP=$LD_LIBRARY_PATH"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env={**fake_cache.env, "_DEVENV_LOCK_WAIT_S": "8"},
    )
    time.sleep(1.5)
    fake_cache.libdir.mkdir(parents=True)
    (fake_cache.libdir / fake_cache.soname).write_bytes(b"\x7fELF")
    fake_cache.sentinel.touch()          # published ...
    # ... but the lock is deliberately NOT released.
    out, err = proc.communicate(timeout=15)
    elapsed = time.monotonic() - started

    assert proc.returncode == 0, err
    assert elapsed < 5, f"waiter ignored the sentinel and sat on the held lock ({elapsed:.1f}s)"
    assert str(fake_cache.libdir) in out, out
    assert "is locked" not in err
    lock.rmdir()


@requires_deb_tools
def test_lock_wait_ends_when_the_lock_is_released_without_a_sentinel(
    tmp_path: Path, fake_cache: SimpleNamespace,
) -> None:
    """397 (review): the LOCK condition alone. A holder that releases the lock
    without publishing (extraction produced nothing, or it was killed and the
    lock reclaimed) has nothing more coming; the waiter must stop promptly and
    fall through to the warning, not wait out the ceiling for a sentinel that
    will never appear.

    MUTATION: drop ``[ -d "$_devenv_lock" ]`` from the while condition. The
    waiter then holds until the 8s ceiling and this test's 5s bound fails.
    """
    slugdir = fake_cache.lib_base / fake_cache.slug
    slugdir.mkdir(parents=True)
    lock = slugdir / ".lock"
    lock.mkdir()
    profile = _patched_profile(tmp_path, fake_cache.devenv)

    started = time.monotonic()
    proc = subprocess.Popen(
        ["bash", "-c", f". {profile} && echo LLP=$LD_LIBRARY_PATH"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        env={**fake_cache.env, "_DEVENV_LOCK_WAIT_S": "8"},
    )
    time.sleep(1.5)
    lock.rmdir()                          # released, nothing published
    _out, err = proc.communicate(timeout=15)
    elapsed = time.monotonic() - started

    assert proc.returncode == 0, err
    assert elapsed < 5, f"waiter ignored the released lock and waited out the ceiling ({elapsed:.1f}s)"
    assert not fake_cache.libdir.exists()
    assert "is locked" in err and "waited" in err, err
