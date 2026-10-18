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
from coordinare_service_inference.environment.execution_source import SourceEntry, source_identity
from coordinare_service_inference.environment.models import (
    CoverageAssessment,
    EnvironmentManifest,
    ExecutionCapabilities,
)
from coordinare_service_inference.environment.render import render_activate, render_verify

from coordinare.models.env_cache import EnvCacheState
from coordinare.services.environment_source_provider import write_execution_bundle


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


def _image_capabilities(name: str) -> ExecutionCapabilities | None:
    image = os.environ.get("COORDINARE_E2E_PERFORMER_IMAGE", "")
    if not re.fullmatch(r"[^\s@]+@sha256:[0-9a-f]{64}", image):
        return None  # Collection without the opt-in image launcher must do no Docker work.
    info = json.loads(_docker("image", "inspect", image))[0]
    architecture = info["Architecture"]
    machine = {"amd64": "x86_64", "arm64": "aarch64"}.get(architecture, architecture)
    return ExecutionCapabilities(schema_version=1, image_ref=image, platform=f"{info['Os']}/{machine}",
        cache_mount_path=f"/devenv/{name}", install_network="enabled", verify_network="disabled")


def manifest_for(name: str, *, files: dict[str, str], checks: list[dict], activation: dict | None = None) -> EnvironmentManifest:
    capabilities = _image_capabilities(name)
    inventory_digest = hashlib.sha256(json.dumps(sorted(files), ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
    return EnvironmentManifest.model_validate({
        "capabilities_digest": capabilities.digest() if capabilities else None, "inventory_digest": inventory_digest,
        "schema_version": 1, "symphony_name": name, "source_commit": "a" * 40,
        "inputs": {path: hashlib.sha256(text.encode()).hexdigest() for path, text in files.items()},
        "requirements": [{"id": "environment", "description": "Pinned source environment constraint",
                          "source": next(iter(files)), "checks": [check["id"] for check in checks]}],
        "install": [], "checks": checks, "activation": activation or {}, "services": [],
        "empty_reason": None, "policy_version": "351-v1",
    })


def accepted_state(cache: Path, manifest: EnvironmentManifest, files: dict[str, str]) -> dict:
    """Use real frozen snapshot reads and trusted rendering, with no verifier seam patched."""
    capabilities = _image_capabilities(manifest.symphony_name)
    if capabilities is None or manifest.capabilities_digest != capabilities.digest():
        pytest.fail("real-image fixtures require pinning before collection; run python -m tests.integration.reviewed_environment")
    activation = render_activate(manifest, cache_mount_path=f"/devenv/{manifest.symphony_name}")
    verification = render_verify(manifest, cache_mount_path=f"/devenv/{manifest.symphony_name}")
    (cache / "activate.sh").write_text(activation)
    (cache / "verify.sh").write_text(verification)
    from agent.performer.tests.source_contract_fixture import current_review_fixture

    assessment = CoverageAssessment(schema_version=1, manifest_digest=manifest.digest(),
        capabilities_digest=manifest.capabilities_digest, inventory_digest=manifest.inventory_digest,
        source_commit=manifest.source_commit, symphony_name=manifest.symphony_name,
        verdict="accept", rationale="Independently inspected the exact fixture source snapshot",
        sources=list(files), inputs=dict(manifest.inputs), policy_version="351-v1")
    source_files = {path: text.encode() for path, text in files.items()}
    entries = [SourceEntry(path=path, mode="100644", size=len(body),
        object_id=hashlib.sha1(b"blob " + str(len(body)).encode() + b"\0" + body).hexdigest())
        for path, body in source_files.items()]
    identity = source_identity(manifest.source_commit, entries)
    state = EnvCacheState(symphony_name=manifest.symphony_name, sanitised_name=manifest.symphony_name,
        cache_dir=cache, bootstrap_commit_sha=manifest.source_commit,
        environment_manifest=manifest.model_dump(), environment_assessment=current_review_fixture(manifest, assessment.model_dump()),
        environment_execution_source=identity.model_dump(),
        rendered_activate_sh=activation, rendered_verify_sh=verification)

    async def fetch(org, repo, path, *, ref):
        assert ref == manifest.source_commit
        content = files.get(path)
        if content is not None:
            assert hashlib.sha256(content.encode()).hexdigest() == manifest.inputs[path]
        return content

    async def prepare_source(org, repo, *, commit, destination, deadline):
        assert org == "fixture" and repo == "repository" and commit == manifest.source_commit
        destination.mkdir(parents=True)
        objects = destination / "objects"
        objects.mkdir()
        for entry in entries:
            (objects / entry.object_id).write_bytes(source_files[entry.path])
        return write_execution_bundle(commit, entries, objects, destination / "source.tgz", deadline=deadline)

    return {"env_cache": {manifest.symphony_name: state},
            "config": SimpleNamespace(github_org="fixture", project_name="repository", agent_transport="docker",
                performer_image=capabilities.image_ref, performer_endpoints=[]),
            "github_service": SimpleNamespace(get_file_content=fetch, prepare_execution_source=prepare_source)}


if __name__ == "__main__":
    raise SystemExit(run_image_tests(sys.argv[1:]))
