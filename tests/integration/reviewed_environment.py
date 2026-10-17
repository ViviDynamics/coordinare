"""Real-image fixtures with accepted contracts and actual immutable OCI identities."""
from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest
from coordinare_service_inference.environment.models import CoverageAssessment, EnvironmentManifest
from coordinare_service_inference.environment.render import render_activate, render_verify

from coordinare.models.env_cache import EnvCacheState


def _docker(*args: str, timeout: int = 300) -> str:
    result = subprocess.run(["docker", *args], capture_output=True, text=True, check=False, timeout=timeout)
    if result.returncode:
        pytest.fail(f"docker {' '.join(args[:3])} failed: {result.stderr[-2000:]}")
    return result.stdout.strip()


@contextlib.contextmanager
def prepared_performer_image():
    """Keep real pin validation enabled, including for CI's locally built tag."""
    image = os.environ.get("COORDINARE_E2E_PERFORMER_IMAGE", "coordinare-performer:full")
    info = json.loads(_docker("image", "inspect", image))[0]
    if re.fullmatch(r"[^\s@]+@sha256:[0-9a-f]{64}", image):
        yield image
        return
    digests = info.get("RepoDigests") or []
    if digests:
        yield digests[0]
        return
    name = "coordinare-image-tests-" + uuid.uuid4().hex[:12]
    tag = None
    owned_refs: set[str] = set()
    _docker("run", "--detach", "--rm", "--name", name, "--network", "host",
            "-e", "REGISTRY_HTTP_ADDR=127.0.0.1:0", "registry:2")
    try:
        # Share the daemon's loopback namespace: published host ports can belong
        # to the desktop host rather than to the Linux VM running Docker.
        for _attempt in range(30):
            logs = subprocess.run(["docker", "logs", name], capture_output=True, text=True, check=False, timeout=10)
            listener = re.search(r"listening on 127\.0\.0\.1:(\d+)", logs.stdout + logs.stderr)
            if listener:
                break
            time.sleep(0.1)
        else:
            pytest.fail("temporary image registry never became ready")
        repository = f"127.0.0.1:{listener[1]}/coordinare-image-tests"
        tag = repository + ":" + uuid.uuid4().hex
        _docker("tag", image, tag)
        pushed = _docker("push", tag, timeout=600)
        published = json.loads(_docker("image", "inspect", tag))[0]["RepoDigests"]
        owned_refs.update(ref for ref in published if ref.startswith(repository + "@sha256:"))
        # Containerd may retain the source index identity after pushing only its
        # available platform. Pull the exact manifest the registry accepted.
        identity = re.search(r"digest: (sha256:[0-9a-f]{64})", pushed)
        if identity is None:
            pytest.fail("temporary registry push did not report a manifest digest")
        digest = repository + "@" + identity[1]
        owned_refs.add(digest)
        _docker("pull", digest, timeout=600)
        yield digest
    finally:
        for reference in ([tag] if tag else []) + sorted(owned_refs):
            subprocess.run(["docker", "image", "rm", reference], capture_output=True, check=False, timeout=60)
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, check=False, timeout=60)


@pytest.fixture(scope="session")
def immutable_performer_image():
    with prepared_performer_image() as image:
        yield image


def run_image_tests(arguments: list[str]) -> int:
    """Prepare the immutable image before pytest starts its test timeout clocks."""
    with prepared_performer_image() as image:
        env = dict(os.environ, COORDINARE_E2E_PERFORMER_IMAGE=image)
        result = subprocess.run(
            [sys.executable, "-m", "pytest", *arguments], env=env, check=False, timeout=300,
        )
        return result.returncode


def manifest_for(name: str, *, files: dict[str, str], checks: list[dict], activation: dict | None = None) -> EnvironmentManifest:
    return EnvironmentManifest.model_validate({
        "schema_version": 1, "symphony_name": name, "source_commit": "a" * 40,
        "inputs": {path: hashlib.sha256(text.encode()).hexdigest() for path, text in files.items()},
        "requirements": [{"id": "environment", "description": "Pinned source environment constraint",
                          "source": next(iter(files)), "checks": [check["id"] for check in checks]}],
        "install": [], "checks": checks, "activation": activation or {}, "services": [],
        "empty_reason": None, "policy_version": "351-v1",
    })


def accepted_state(cache: Path, manifest: EnvironmentManifest, files: dict[str, str]) -> dict:
    """Use real frozen snapshot reads and trusted rendering, with no verifier seam patched."""
    activation = render_activate(manifest, cache_mount_path=f"/devenv/{manifest.symphony_name}")
    verification = render_verify(manifest, cache_mount_path=f"/devenv/{manifest.symphony_name}")
    (cache / "activate.sh").write_text(activation)
    (cache / "verify.sh").write_text(verification)
    assessment = CoverageAssessment(schema_version=1, manifest_digest=manifest.digest(),
        source_commit=manifest.source_commit, symphony_name=manifest.symphony_name,
        verdict="accept", rationale="Independently inspected the exact fixture source snapshot",
        sources=list(files), inputs=dict(manifest.inputs), policy_version="351-v1")
    state = EnvCacheState(symphony_name=manifest.symphony_name, sanitised_name=manifest.symphony_name,
        cache_dir=cache, bootstrap_commit_sha=manifest.source_commit,
        environment_manifest=manifest.model_dump(), environment_assessment=assessment.model_dump(),
        rendered_activate_sh=activation, rendered_verify_sh=verification)

    async def fetch(org, repo, path, *, ref):
        assert ref == manifest.source_commit
        content = files.get(path)
        if content is not None:
            assert hashlib.sha256(content.encode()).hexdigest() == manifest.inputs[path]
        return content

    return {"env_cache": {manifest.symphony_name: state},
            "config": SimpleNamespace(github_org="fixture", project_name="repository"),
            "github_service": SimpleNamespace(get_file_content=fetch)}


if __name__ == "__main__":
    raise SystemExit(run_image_tests(sys.argv[1:]))
