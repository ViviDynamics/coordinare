"""Spec 234: a post-verification swap through the REAL performer image is ignored or refused.

The unit tests pin the daemon's decisions and the profile's logic. This lane runs a consumer shell in the
built image, the way a consumer runs, and checks what it actually executed:

1. The daemon freezes a cache (a toolchain stub, an activate.sh, a services-start.sh).
2. After the freeze the cache is changed: activate.sh is swapped, and a services-start.sh that drops a marker
   is planted. The daemon's own check refuses it.
3. The performer materialises the frozen steps (the daemon's payload) into a provenance directory, which is
   mounted into the image at the path the profile reads. The cache is mounted read-only, as a consumer gets it.
4. A consumer shell (``bash -c``, the image's BASH_ENV profile) runs. The frozen activate.sh ran, the swapped one
   and the planted services script did not.

Opt-in lane: ``COORDINARE_DOCKER_IMAGE_TESTS=1`` and a built image named by ``COORDINARE_E2E_PERFORMER_IMAGE``
(default ``coordinare-performer:full``). Without the opt-in the ``docker_image`` marker deselects it. With the
opt-in, a missing docker or image FAILS the module fixture, so the lane cannot pass quietly.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from performer.env_provenance import materialise
from performer.server.models import EnvProvenance

from coordinare.services.env_provenance import build_payload, check_cache, freeze_cache

pytestmark = pytest.mark.docker_image

IMAGE = os.environ.get("COORDINARE_E2E_PERFORMER_IMAGE", "coordinare-performer:full")
SLUG = "sym-234"
CACHE_PATH = f"/devenv/{SLUG}"
FROZEN_ACTIVATE = "touch /tmp/frozen-234\n"
SWAPPED_ACTIVATE = "touch /tmp/swapped-234\n"
PLANTED_SERVICES = "touch /tmp/planted-services-234\n"


@pytest.fixture(scope="module", autouse=True)
def _image_is_present() -> None:
    """Fail, do not pass, when the image is missing: the opt-in lane must actually run."""
    probe = subprocess.run(
        ["docker", "image", "inspect", IMAGE], capture_output=True, text=True, check=False, timeout=60,
    )
    if probe.returncode != 0:
        pytest.fail(f"performer image {IMAGE!r} is not built; build it, or set COORDINARE_E2E_PERFORMER_IMAGE")


def _frozen_cache(tmp_path: Path) -> tuple[Path, Path, object]:
    root = tmp_path / "caches"
    cache = root / SLUG
    (cache / "bin").mkdir(parents=True)
    (cache / "bin" / "ruby").write_text("#!/bin/sh\necho ruby 3.4.2\n")
    (cache / "bin" / "ruby").chmod(0o755)
    (cache / "activate.sh").write_text(FROZEN_ACTIVATE)
    manifest = freeze_cache(
        cache, symphony="sym-234", cache_key="k", activate_sh=FROZEN_ACTIVATE, performer_mounts=(root,),
    )
    return cache, root, manifest


def test_a_post_verification_swap_is_not_run_by_a_consumer_shell(tmp_path: Path) -> None:
    cache, root, manifest = _frozen_cache(tmp_path)

    # The bootstrap writer changes the cache after the verification.
    (cache / "activate.sh").write_text(SWAPPED_ACTIVATE)
    (cache / "services").mkdir()
    (cache / "services" / "services-start.sh").write_text(PLANTED_SERVICES)

    # The daemon refuses the cache a consumer would mount.
    check = check_cache(cache, manifest, full=True, performer_mounts=(root,))
    assert check.ok is False

    # Even so, the frozen steps the daemon hands over are the verified ones. Materialise them the way the
    # performer does, then run a consumer shell in the real image with the cache mounted read-only.
    payload = build_payload(manifest, cache_path=CACHE_PATH)
    provenance = tmp_path / "provenance"
    materialise(EnvProvenance.model_validate({**payload, "cache_path": str(cache)}), root=provenance)

    argv = [
        "docker", "run", "--rm",
        "-v", f"{cache}:{CACHE_PATH}:ro",
        "-v", f"{provenance}:/var/lib/devenv-provenance:ro",
        "-e", "_DEVENV_PROVENANCE_DIR=/var/lib/devenv-provenance",
        "--entrypoint", "bash", IMAGE, "-c",
        "test -e /tmp/swapped-234 && echo SWAPPED; "
        "test -e /tmp/planted-services-234 && echo PLANTED; "
        "test -e /tmp/frozen-234 && echo FROZEN; true",
    ]
    result = subprocess.run(argv, capture_output=True, text=True, check=False, timeout=600)
    assert result.returncode == 0, result.stderr
    assert "SWAPPED" not in result.stdout, "the swapped activate.sh was sourced by a consumer"
    assert "PLANTED" not in result.stdout, "a services-start.sh added to the cache was run"
    assert "FROZEN" in result.stdout, "the frozen activate.sh is the one that ran"
