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

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from coordinare_service_inference.environment.render import render_activate
from performer.env_provenance import materialise
from performer.server.models import EnvProvenance

from coordinare.services.env_cache import verify_env_cache_clean
from coordinare.services.env_provenance import (
    ProvenanceScope,
    build_payload,
    freeze_cache,
    open_manifest,
    verify_content,
)
from tests.integration.reviewed_environment import (
    accepted_state,
    manifest_for,
)
from tests.integration.reviewed_environment import (
    immutable_performer_image as _image_fixture,
)

real_image = _image_fixture
pytestmark = pytest.mark.docker_image

SLUG = "sym-234"
CACHE_PATH = f"/devenv/{SLUG}"
FILES = {"environment.plan": "Activation must set COORDINARE_FROZEN_234=yes\n"}
MANIFEST = manifest_for(SLUG, files=FILES, activation={"env": {"COORDINARE_FROZEN_234": "yes"}}, checks=[{
    "id": "activation", "argv": ["/usr/local/bin/python", "-I", "-c",
        "import os; assert os.environ.get('COORDINARE_FROZEN_234') == 'yes'"]}])
FROZEN_ACTIVATE = render_activate(MANIFEST, cache_mount_path=CACHE_PATH)
SWAPPED_ACTIVATE = "touch /tmp/swapped-234\n"
PLANTED_SERVICES = "touch /tmp/planted-services-234\n"


async def _frozen_cache(tmp_path: Path, image: str) -> tuple[Path, Path, object]:
    root = tmp_path / "caches"
    cache = root / SLUG
    (cache / "bin").mkdir(parents=True)
    (cache / "bin" / "ruby").write_text("#!/bin/sh\necho ruby 3.4.2\n")
    (cache / "bin" / "ruby").chmod(0o755)
    state = accepted_state(cache, MANIFEST, FILES)
    svc = SimpleNamespace(devenv_root="/devenv", _config=SimpleNamespace(image=image))
    passed, detail = await verify_env_cache_clean(state, SLUG, svc, performer_mounts=(root,))
    assert passed is True, detail
    evidence = state["env_cache"][SLUG].environment_verification
    assert evidence["manifest_digest"] == MANIFEST.digest()
    assert evidence["results"][0]["id"] == "activation"
    assert evidence["results"][0]["status"] == "pass"
    assert evidence["results"][0]["exit_code"] == 0
    out = tmp_path / "state" / "sym-234.json"
    result = freeze_cache(
        cache, identity=("sym-234", "k"), activate_sh=FROZEN_ACTIVATE,
        scope=ProvenanceScope((root,)), out_path=out,
    )
    manifest = open_manifest(out, digest=result.manifest.digest, performer_mounts=(root,), max_bytes=10**9)
    assert manifest is not None
    return cache, root, manifest


async def test_a_post_verification_swap_is_not_run_by_a_consumer_shell(tmp_path: Path, real_image: str) -> None:
    cache, root, manifest = await _frozen_cache(tmp_path, real_image)

    # The bootstrap writer changes the cache after the verification.
    (cache / "activate.sh").write_text(SWAPPED_ACTIVATE)
    (cache / "services").mkdir()
    (cache / "services" / "services-start.sh").write_text(PLANTED_SERVICES)

    # The daemon refuses the cache a consumer would mount.
    check = verify_content(cache, manifest, scope=ProvenanceScope((root,)))
    assert check.ok is False

    # Even so, the frozen steps the daemon hands over are the verified ones. Materialise them the way the
    # performer does, then run a consumer shell in the real image with the cache mounted read-only.
    payload = build_payload(manifest.meta, cache_path=CACHE_PATH, digest=manifest.digest)
    provenance = tmp_path / "provenance"
    materialise(EnvProvenance.model_validate({**payload, "cache_path": str(cache)}), root=provenance)

    argv = [
        "docker", "run", "--rm",
        "-v", f"{cache}:{CACHE_PATH}:ro",
        "-v", f"{provenance}:/var/lib/devenv-provenance:ro",
        "-e", "_DEVENV_PROVENANCE_DIR=/var/lib/devenv-provenance",
        "--entrypoint", "bash", real_image, "-c",
        "test -e /tmp/swapped-234 && echo SWAPPED; "
        "test -e /tmp/planted-services-234 && echo PLANTED; "
        'test "${COORDINARE_FROZEN_234-}" = yes && echo FROZEN; true',
    ]
    result = subprocess.run(argv, capture_output=True, text=True, check=False, timeout=600)
    assert result.returncode == 0, result.stderr
    assert "SWAPPED" not in result.stdout, "the swapped activate.sh was sourced by a consumer"
    assert "PLANTED" not in result.stdout, "a services-start.sh added to the cache was run"
    assert "FROZEN" in result.stdout, "the frozen activate.sh is the one that ran"
