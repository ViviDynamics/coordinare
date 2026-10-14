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
import os
import subprocess
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from coordinare.models.env_cache import EnvCacheState
from coordinare.models.env_manifest import EnvManifest, ManifestItem
from coordinare.services.env_cache import verify_env_cache_clean
from coordinare.services.env_manifest import render_activate_sh, render_verify_sh

pytestmark = pytest.mark.docker_image

IMAGE = os.environ.get("COORDINARE_E2E_PERFORMER_IMAGE", "coordinare-performer:full")
CACHE_PATH = "/devenv/probe218"
MANIFEST = EnvManifest(
    symphony_name="probe218",
    items=[ManifestItem(name="ruby", kind="runtime", version="3.4.2", source=".ruby-version")],
)
BAD_ACTIVATE = "ruby() { printf 3.4.2; }\n"
# What the real verify prints when the toolchain is absent. A passing verify is never this.
NO_TOOLCHAIN = "FAIL: ruby version (want 3.4.2, got none)"
# The daemon refused before docker ran, because the cache copy no longer matches.
MISMATCH = "activate.sh in the cache does not match the coordinare-rendered content"


@pytest.fixture(scope="module", autouse=True)
def _image_is_present() -> None:
    """Fail, do not pass, when the image is missing: the opt-in lane must actually run."""
    probe = subprocess.run(
        ["docker", "image", "inspect", IMAGE], capture_output=True, text=True, check=False, timeout=60,
    )
    if probe.returncode != 0:
        pytest.fail(f"performer image {IMAGE!r} is not built; build it, or set COORDINARE_E2E_PERFORMER_IMAGE")


def _state(cache: Path) -> dict[str, Any]:
    activate = render_activate_sh(MANIFEST, cache_mount_path=CACHE_PATH)
    verify = render_verify_sh(MANIFEST, cache_mount_path=CACHE_PATH, activate_text=activate)
    (cache / "activate.sh").write_text(activate)
    state = EnvCacheState(
        symphony_name="probe218", sanitised_name="probe218", cache_dir=cache,
        rendered_verify_sh=verify, rendered_activate_sh=activate,
    )
    return {"env_cache": {"probe218": state}}


def _svc() -> Any:
    return SimpleNamespace(devenv_root="/devenv", _config=SimpleNamespace(image=IMAGE))


def _run(coro: Any) -> Any:
    """Run on a worker thread with its own loop, so an already-running loop cannot break the call."""
    with ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro).result()


def _plant_fake_ruby_deb(debs: Path) -> None:
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
        ["docker", "run", "--rm", "-v", f"{debs}:/out", "--entrypoint", "bash", IMAGE, "-c", script],
        check=True,
        timeout=300,
    )


def test_planted_deb_with_fake_ruby_does_not_pass_the_verify(tmp_path: Path) -> None:
    """Block 1: the image profile (BASH_ENV) would extract debs/*.deb to /usr/bin before the daemon's
    script runs. The verify must run and find no toolchain, not the fake one the performer planted."""
    cache = tmp_path / "probe218"
    cache.mkdir()
    state = _state(cache)
    _plant_fake_ruby_deb(cache / "debs")

    passed, detail = _run(verify_env_cache_clean(state, "probe218", _svc(), performer_mounts=[tmp_path]))

    assert passed is False
    assert NO_TOOLCHAIN in detail, detail


def test_activate_swap_during_the_verify_never_changes_the_verdict(tmp_path: Path) -> None:
    """Block 2: a writer that flips activate.sh while docker runs must not change the verdict. With no
    toolchain installed, every run is the verify's own FAIL. The swapped-in activate.sh is never read."""
    cache = tmp_path / "probe218"
    cache.mkdir()
    state = _state(cache)
    good = (cache / "activate.sh").read_text()
    stop = threading.Event()

    def toggle() -> None:
        while not stop.is_set():
            (cache / "activate.sh").write_text(BAD_ACTIVATE)
            (cache / "activate.sh").write_text(good)

    async def five() -> list[tuple[bool | None, str]]:
        results: list[tuple[bool | None, str]] = []
        for _ in range(5):
            results.append(await verify_env_cache_clean(state, "probe218", _svc(), performer_mounts=[tmp_path]))
        return results

    writer = threading.Thread(target=toggle, daemon=True)
    writer.start()
    try:
        results = _run(five())
    finally:
        stop.set()
        writer.join(timeout=10)

    # A toggle that lands inside the match check is a legitimate refusal, and the verify never
    # runs in that case. Any other outcome is a failure.
    assert [passed for passed, _ in results] == [False] * 5
    assert all(detail == MISMATCH or NO_TOOLCHAIN in detail for _, detail in results), results
