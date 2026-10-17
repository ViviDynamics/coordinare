"""Spec 218 (review round 2): the verify container runs only the daemon's text.

Block 1: the performer image sets BASH_ENV to the devenv profile, which unpacks the cache's debs,
symlinks /usr/bin entries and sources the cache's activate.sh before ``bash -s`` reads stdin. The
verify docker run must disable that, and the rendered verify must not source the cache copy.

Block 2: activate.sh was checked and then sourced by docker a moment later, so a writer could swap
it in between. The rendered verify inlines the daemon's activation text, so the cache copy of
activate.sh is never read by the verify.

These assert on the exact docker argv, env flags and stdin, through a fake runner. The real-image
proof is in tests/e2e/test_218_verify_real_image.py.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from coordinare.models.env_cache import EnvCacheState
from coordinare.models.env_manifest import EnvManifest, ManifestItem
from coordinare.services.env_cache import verify_env_cache_clean
from coordinare.services.env_manifest import render_activate_sh, render_verify_sh
from tests.unit.env_manifest_fixture import reviewed_cache, verification_output


@pytest.fixture(autouse=True)
def _accepted_source_fixture(monkeypatch):
    from unittest.mock import AsyncMock

    monkeypatch.setattr("coordinare.services.environment_verification.verification_sources",
                        AsyncMock(return_value={"tools.plan": "baseline"}))
    monkeypatch.setattr("coordinare.services.environment_capabilities._docker_identity",
        AsyncMock(return_value=("trusted/global@sha256:" + "a" * 64, "linux/x86_64")))
    # These tests exercise trusted verifier execution, not source archive materialisation.
    monkeypatch.setattr("coordinare.services.environment_verification.source_setup", lambda files, **kwargs: "")


SVC = SimpleNamespace(devenv_root="/devenv", _config=SimpleNamespace(image="img@sha256:" + "1" * 64))
CACHE_PATH = "/devenv/sym-abc"
ACTIVATE_TEXT = "DEVENV_MARKER_ACTIVATE=1\n"
VERIFY_TEXT = "#!/bin/sh\necho 'FAIL: nothing installed' >&2\nexit 1\n"

_REAL_EXEC = asyncio.create_subprocess_exec


class _Docker:
    """Records argv and stdin. ``on_run`` runs while the fake container is 'running'."""

    def __init__(self, *, run_locally: bool, rc: int = 0, on_run: Any = None) -> None:
        self.run_locally = run_locally
        self.rc = rc
        self.on_run = on_run
        self.argv: list[tuple[str, ...]] = []
        self.stdin: list[bytes | None] = []
        self.manifest = _state(Path("/fixture-unused")).environment_manifest

    async def create_subprocess_exec(self, *argv: Any, **_kwargs: Any) -> _Proc:
        self.argv.append(tuple(str(a) for a in argv))
        return _Proc(self)


class _Proc:
    def __init__(self, owner: _Docker) -> None:
        self._owner = owner
        self.returncode: int | None = None

    async def wait(self) -> int:
        if self.returncode is None:
            self.returncode = 0
        return self.returncode

    async def communicate(self, **kwargs: Any) -> tuple[bytes, None]:
        data = kwargs.get("input")
        self._owner.stdin.append(data)
        if self._owner.on_run is not None:
            self._owner.on_run()
        if not self._owner.run_locally:
            self.returncode = self._owner.rc
            return verification_output(self._owner.manifest) if self._owner.rc == 0 else b"", None
        proc = await _REAL_EXEC(
            "sh", "-s",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        out, _ = await proc.communicate(input=data)
        self.returncode = proc.returncode
        return out, None


def _manifest() -> EnvManifest:
    return EnvManifest(
        symphony_name="sym",
        items=[ManifestItem(name="ruby", kind="runtime", version="3.4.2", source=".ruby-version")],
    )


def _cache(tmp_path: Path, activate: str = ACTIVATE_TEXT) -> Path:
    cache = tmp_path / "sym-abc"
    cache.mkdir()
    (cache / "activate.sh").write_text(activate)
    return cache


def _state(cache: Path, verify: str = VERIFY_TEXT, activate: str = ACTIVATE_TEXT) -> EnvCacheState:
    return reviewed_cache(EnvCacheState(
        symphony_name="sym", sanitised_name="sym-abc", cache_dir=cache,
        rendered_verify_sh=verify, rendered_activate_sh=activate,
        bootstrap_commit_sha="a" * 40,
    ))


def _ctx(state: EnvCacheState) -> dict[str, Any]:
    return {"env_cache": {"sym": state}, "config": SimpleNamespace(performer_image="trusted/global@sha256:" + "a" * 64, performer_endpoints=[])}


def _docker_argv(fake: _Docker) -> tuple[str, ...]:
    runs = [c for c in fake.argv if c[1:2] == ("run",)]
    assert len(runs) == 1, fake.argv
    return runs[0]


def _env_pairs(argv: tuple[str, ...]) -> set[str]:
    return {argv[i + 1] for i, arg in enumerate(argv[:-1]) if arg == "-e"}


@pytest.mark.asyncio
async def test_verify_container_disables_the_performer_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BASH_ENV (the profile that unpacks debs and sources the cache) and ENV are emptied, the
    profile guard is set, and the service start is skipped. This is the exact argv."""
    cache = _cache(tmp_path)
    fake = _Docker(run_locally=False, rc=0)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake.create_subprocess_exec)

    passed, _ = await verify_env_cache_clean(_ctx(_state(cache)), "sym", SVC, performer_mounts=())

    assert passed is True
    argv = _docker_argv(fake)
    env = _env_pairs(argv)
    assert "BASH_ENV=" in env
    assert "ENV=" in env
    assert "_DEVENV_SOURCED=1" in env
    assert "_DEVENV_SKIP_SERVICES=1" in env
    # Still the read-only cache mount, the bash entrypoint and stdin.
    assert f"{cache}:{CACHE_PATH}:ro" in argv
    assert argv[argv.index("--entrypoint") + 1] == "bash"
    assert "-i" in argv
    assert argv[-1] == "-s"


@pytest.mark.asyncio
async def test_verify_stdin_is_the_daemon_script_and_never_the_cache_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cache = _cache(tmp_path)
    (cache / "verify.sh").write_text("#!/bin/sh\nexit 0\n")
    fake = _Docker(run_locally=False, rc=0)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake.create_subprocess_exec)

    await verify_env_cache_clean(_ctx(_state(cache)), "sym", SVC, performer_mounts=())

    assert [data for data in fake.stdin if data is not None] == [VERIFY_TEXT.encode()]
    assert all("verify.sh" not in arg for arg in _docker_argv(fake))


@pytest.mark.asyncio
async def test_rendered_verify_inlines_activation_and_never_sources_the_cache_copy() -> None:
    """The daemon's verify carries its activation text, so it does not source the cache file."""
    text = render_verify_sh(
        _manifest(), cache_mount_path=CACHE_PATH, activate_text=render_activate_sh(
            _manifest(), cache_mount_path=CACHE_PATH,
        ),
    )

    assert f". \"{CACHE_PATH}/activate.sh\"" not in text
    assert f"{CACHE_PATH}/activate.sh" not in text
    # The activation body is present, inline.
    assert "DEVENV" in text
    assert 'if [ -f "/devenv/sym-abc/activate.sh" ]' not in text


@pytest.mark.asyncio
async def test_a_cache_activate_rewrite_during_the_run_does_not_change_the_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The TOCTOU: the match check runs first, then docker runs. A writer swapping activate.sh
    in that window must not change what the verify executes. The verify no longer reads it."""
    cache = _cache(tmp_path)
    rendered = render_verify_sh(
        _manifest(), cache_mount_path=CACHE_PATH, activate_text=ACTIVATE_TEXT,
    )
    state = _state(cache, verify=rendered, activate=ACTIVATE_TEXT)
    _seed = (cache / "activate.sh").read_text()

    def swap() -> None:
        (cache / "activate.sh").write_text("echo 'SOURCED ATTACKER' >&2\nexit 1\n")

    fake = _Docker(run_locally=False, rc=0, on_run=swap)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake.create_subprocess_exec)

    passed, _detail = await verify_env_cache_clean(_ctx(state), "sym", SVC, performer_mounts=())

    assert _seed != (cache / "activate.sh").read_text()  # the writer really did swap it mid-run
    assert passed is True
    assert [data for data in fake.stdin if data is not None] == [rendered.encode()]
