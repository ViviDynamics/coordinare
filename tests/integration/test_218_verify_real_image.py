"""Spec 218 (review round 2 and 3): the verify decision against a REAL performer image.

The unit tests drive docker through a fake runner, so they cannot see the profile the image runs before
``bash -s`` reads stdin (BASH_ENV). These tests run the real ``verify_env_cache_clean`` through docker.

Opt-in lane: they run only with ``COORDINARE_DOCKER_IMAGE_TESTS=1`` and a built performer image named by
``COORDINARE_E2E_PERFORMER_IMAGE`` (default ``coordinare-performer:full``). Without the opt-in the
``docker_image`` marker deselects them (tests/conftest.py). With the opt-in, a missing docker or image FAILS
the module fixture. It never passes quietly.

Every assertion is on the specific outcome: the verify ran, the toolchain reported ``got none``, and the
verdict is the verify's own. An image that cannot start yields exit 125, which the verify maps to
infrastructure (None), so none of these can pass on a missing image.
"""

from __future__ import annotations

import asyncio
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from coordinare.services.env_cache import verify_env_cache_clean
from tests.integration.reviewed_environment import (
    accepted_state,
    manifest_for,
)
from tests.integration.reviewed_environment import (
    immutable_performer_image as _image_fixture,
)

real_image = _image_fixture
pytestmark = pytest.mark.docker_image

CACHE_PATH = "/devenv/probe218"
FILES = {".ruby-version": "3.4.2\n"}
RUBY_CHECK = (
    "from pathlib import Path; import os,shutil,subprocess,sys; "
    "assert 'COORDINARE_SWAPPED_ACTIVATION' not in os.environ, 'FAIL: swapped activation executed'; "
    "want=Path('.ruby-version').read_text().strip(); binary=shutil.which('ruby'); "
    "got=subprocess.run([binary,'-e','print RUBY_VERSION'],capture_output=True,text=True).stdout.strip() if binary else 'none'; "
    "passed=got==want; print(('PASS' if passed else 'FAIL')+': ruby version (want '+want+', got '+got+')'); "
    "sys.exit(0 if passed else 1)"
)
MANIFEST = manifest_for("probe218", files=FILES, checks=[{
    "id": "ruby-version", "argv": ["/usr/local/bin/python", "-I", "-c", RUBY_CHECK]}])
BAD_ACTIVATE = "export COORDINARE_SWAPPED_ACTIVATION=1\nruby() { printf 3.4.2; }\n"
# What the real verify prints when the toolchain is absent. A passing verify is never this.
NO_TOOLCHAIN = "FAIL: ruby version (want 3.4.2, got none)"


def _state(cache: Path) -> dict[str, Any]:
    return accepted_state(cache, MANIFEST, FILES)


def _svc(image: str) -> Any:
    return SimpleNamespace(devenv_root="/devenv", _config=SimpleNamespace(image=image))


def _run(coro: Any) -> Any:
    """Run on a worker thread with its own loop, so an already-running loop cannot break the call."""
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def _plant_fake_ruby_deb(debs: Path, image: str) -> None:
    """A performer-written .deb whose data is a fake /usr/bin/ruby that reports 3.4.2."""
    debs.mkdir(parents=True, exist_ok=True)
    script = (
        "set -e; rm -rf /tmp/p; mkdir -p /tmp/p/DEBIAN /tmp/p/usr/bin /tmp/p/usr/lib; "
        "printf x > /tmp/p/usr/lib/libfake.so.1; "
        "printf '#!/bin/sh\\nprintf 3.4.2\\n' > /tmp/p/usr/bin/ruby; chmod 755 /tmp/p/usr/bin/ruby; "
        "printf 'Package: fake\\nVersion: 1\\nArchitecture: all\\nMaintainer: x\\nDescription: x\\n' "
        "> /tmp/p/DEBIAN/control; "
        "dpkg-deb --build /tmp/p /out/fake.deb >/dev/null"
    )
    subprocess.run(
        ["docker", "run", "--rm", "-v", f"{debs}:/out", "--entrypoint", "bash", image, "-c", script],
        check=True,
        timeout=300,
    )


def test_planted_deb_with_fake_ruby_does_not_pass_the_verify(tmp_path: Path, real_image: str) -> None:
    """Block 1: the image profile (BASH_ENV) would extract debs/*.deb to /usr/bin before the daemon's
    script runs. The verify must run and find no toolchain, not the fake one the performer planted."""
    cache = tmp_path / "probe218"
    cache.mkdir()
    state = _state(cache)
    _plant_fake_ruby_deb(cache / "debs", real_image)
    # Positive control: the actual image startup profile can turn this planted
    # archive into a fake passing Ruby when it is allowed to read a writer cache.
    attack = subprocess.run(
        ["docker", "run", "--rm", "-v", f"{cache}:{CACHE_PATH}:ro", "-e", "_DEVENV_BOOTSTRAP=1",
         "--entrypoint", "bash", real_image, "-c", "ruby -e 'print RUBY_VERSION'"],
        capture_output=True, text=True, check=False, timeout=300,
    )
    assert attack.returncode == 0, attack.stderr
    assert attack.stdout == "3.4.2", attack.stderr

    passed, detail = _run(verify_env_cache_clean(state, "probe218", _svc(real_image), performer_mounts=[tmp_path]))

    assert passed is False
    assert NO_TOOLCHAIN in detail, detail
    evidence = state["env_cache"]["probe218"].environment_verification
    assert evidence["manifest_digest"] == MANIFEST.digest()
    assert evidence["results"][0]["id"] == "ruby-version"
    assert evidence["results"][0]["status"] == "fail"
    assert evidence["results"][0]["exit_code"] == 1


def test_activate_swap_during_the_verify_never_changes_the_verdict(
    tmp_path: Path, real_image: str, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Swap the real cache after its match gate and before the real container starts.

    Scheduling this boundary deliberately makes every attempt reach the checker;
    it cannot pass by merely refusing the cache before Docker runs.
    """
    cache = tmp_path / "probe218"
    cache.mkdir()
    state = _state(cache)
    good = (cache / "activate.sh").read_text()
    actual_exec = asyncio.create_subprocess_exec
    swaps = []

    async def swap_before_start(*argv, **kwargs):
        if argv[:2] == ("docker", "run") and "--name" in argv:
            (cache / "activate.sh").write_text(BAD_ACTIVATE)
            swaps.append(True)
        return await actual_exec(*argv, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", swap_before_start)

    async def five() -> list[tuple[bool | None, str]]:
        results = []
        for _ in range(5):
            (cache / "activate.sh").write_text(good)
            results.append(await verify_env_cache_clean(
                state, "probe218", _svc(real_image), performer_mounts=[tmp_path],
            ))
        return results

    results = _run(five())
    assert len(swaps) == 5
    assert (cache / "activate.sh").read_text() == BAD_ACTIVATE
    assert [passed for passed, _ in results] == [False] * 5
    assert all(NO_TOOLCHAIN in detail for _, detail in results), results
    evidence = state["env_cache"]["probe218"].environment_verification
    assert evidence["results"][0]["status"] == "fail"
    assert evidence["results"][0]["exit_code"] == 1
