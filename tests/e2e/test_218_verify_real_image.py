"""Spec 218 (review round 2): the verify decision against a REAL performer image.

The unit tests use a fake docker runner, so they cannot see the profile that the image runs before
``bash -s`` reads stdin (BASH_ENV). These tests run the real ``verify_env_cache_clean`` through
docker, with the image named by ``COORDINARE_E2E_PERFORMER_IMAGE`` (default ``coordinare-performer:full``).

Run with: ``pytest -m e2e tests/e2e/test_218_verify_real_image.py``
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

pytestmark = pytest.mark.e2e

IMAGE = os.environ.get("COORDINARE_E2E_PERFORMER_IMAGE", "coordinare-performer:full")
CACHE_PATH = "/devenv/probe218"
MANIFEST = EnvManifest(
    symphony_name="probe218",
    items=[ManifestItem(name="ruby", kind="runtime", version="3.4.2", source=".ruby-version")],
)
BAD_ACTIVATE = "ruby() { printf 3.4.2; }\n"


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
    """Run a coroutine on its own loop in a worker thread. The e2e session may leave a loop running
    on this thread, so asyncio.run here would fail with 'cannot be called from a running event loop'."""
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
    """Block 1: the image profile (BASH_ENV) would extract debs/*.deb to /usr/bin before the
    daemon's script runs. The verify must not pass on a fake ruby the performer planted."""
    cache = tmp_path / "probe218"
    cache.mkdir()
    state = _state(cache)
    _plant_fake_ruby_deb(cache / "debs")

    passed, detail = _run(verify_env_cache_clean(state, "probe218", _svc(), performer_mounts=[tmp_path]))

    assert passed is False, detail


def test_activate_swap_during_the_verify_never_changes_the_verdict(tmp_path: Path) -> None:
    """Block 2: a writer that flips activate.sh while docker runs must not change the verdict.
    With no toolchain installed, every run must fail; the swapped-in activate.sh is never read."""
    cache = tmp_path / "probe218"
    cache.mkdir()
    state = _state(cache)
    good = (cache / "activate.sh").read_text()
    stop = threading.Event()

    def toggle() -> None:
        while not stop.is_set():
            (cache / "activate.sh").write_text(BAD_ACTIVATE)
            (cache / "activate.sh").write_text(good)

    async def five() -> list[bool | None]:
        verdicts: list[bool | None] = []
        for _ in range(5):
            verdict, _detail = await verify_env_cache_clean(
                state, "probe218", _svc(), performer_mounts=[tmp_path],
            )
            verdicts.append(verdict)
        return verdicts

    writer = threading.Thread(target=toggle, daemon=True)
    writer.start()
    try:
        verdicts = _run(five())
    finally:
        stop.set()
        writer.join(timeout=10)

    assert verdicts == [False] * 5
