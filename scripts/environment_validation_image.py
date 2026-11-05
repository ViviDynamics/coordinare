"""Private, temporary images for opt-in real-repository environment validation."""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import TYPE_CHECKING, Any

import coordinare_service_inference.environment as environment_package

if TYPE_CHECKING:
    from collections.abc import Iterator

DOCKER = shutil.which("docker") or "/usr/local/bin/docker"

_IMAGE = re.compile(r"[^\s@]+@sha256:[0-9a-f]{64}")


def _docker(*args: str, timeout: int = 300) -> str:
    result = subprocess.run(
        [DOCKER, *args], capture_output=True, text=True, check=False, timeout=timeout,
    )
    if result.returncode:
        raise RuntimeError("validation image operation failed: " + " ".join(args[:2]))
    return result.stdout.strip()


def package_hashes() -> dict[str, str]:
    root = Path(environment_package.__file__).parent
    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*.py"))
    }


def performer_source() -> Path:
    return Path(__file__).resolve().parents[1]/'agent'/'performer'/'src'/'performer'


def performer_hashes() -> dict[str, str]:
    root = performer_source()
    return {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(root.rglob('*.py'))}


def verify_image_code(image: str) -> dict[str, Any]:
    """Confirm current shared and performer code in the immutable execution image."""
    if not _IMAGE.fullmatch(image):
        raise ValueError("validation image must use an immutable repository digest")
    program = (
        "import hashlib,json,pathlib,platform;import coordinare_service_inference.environment as package;"
        "import performer;root=pathlib.Path(package.__file__).parent;performer_root=pathlib.Path(performer.__file__).parent;"
        "print(json.dumps({'platform':platform.system().lower()+'/'+platform.machine().lower(),"
        "'files':{str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(root.rglob('*.py'))},"
        "'performer_files':{str(p.relative_to(performer_root)):hashlib.sha256(p.read_bytes()).hexdigest() "
        "for p in sorted(performer_root.rglob('*.py'))}}))"
    )
    info = json.loads(
        _docker(
            "run",
            "--rm",
            "--network",
            "none",
            "--entrypoint",
            "/usr/local/bin/python",
            image,
            "-I",
            "-c",
            program,
        ),
    )
    if info.get("files") != package_hashes():
        raise ValueError("validation image shared environment code differs from the checkout")
    if info.get('performer_files') != performer_hashes():
        raise ValueError('validation image performer code differs from the checkout')
    return {"image_ref": image, "platform": info["platform"], "files": info["files"],
            "performer_files": info['performer_files']}


@contextlib.contextmanager
def candidate_image(base_image: str, output: Path, *, allow_local_image_id: bool = False) -> Iterator[str]:
    """Overlay trusted code, pin via a loopback registry, remove only owned refs."""
    local = allow_local_image_id and re.fullmatch(r"sha256:[0-9a-f]{64}", base_image)
    if local:
        actual = _docker("image", "inspect", base_image, "--format", "{{.Id}}")
        if actual != base_image:
            raise ValueError("local base image identity differs")
    elif not _IMAGE.fullmatch(base_image):
        raise ValueError("base image must use an immutable repository digest")
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    source = Path(environment_package.__file__).parent
    shutil.copytree(source, output / "environment", ignore=shutil.ignore_patterns("__pycache__"))
    shutil.copytree(performer_source(), output / 'performer', ignore=shutil.ignore_patterns('__pycache__'))
    program = (
        "import pathlib,shutil\nimport coordinare_service_inference as package\nimport performer\n"
        "for source,destination in [('/tmp/coordinare-validation-environment',pathlib.Path(package.__file__).parent/'environment'),"
        "('/tmp/coordinare-validation-performer',pathlib.Path(performer.__file__).parent)]:\n"
        " shutil.rmtree(destination,ignore_errors=True)\n shutil.copytree(source,destination)\n"
    )
    (output / "Dockerfile").write_text(
        "FROM " + base_image + "\n"
        'COPY ["environment/", "/tmp/coordinare-validation-environment/"]\n'
        'COPY ["performer/", "/tmp/coordinare-validation-performer/"]\n'
        + "RUN "
        + json.dumps(["/usr/local/bin/python", "-I", "-c", program])
        + "\n",
    )
    nonce = uuid.uuid4().hex[:12]
    tag = "coordinare-env-validation-" + nonce + ":candidate"
    registry = "coordinare-env-validation-registry-" + nonce
    owned_refs = {tag}
    registry_attempted = False
    try:
        built = _docker(
            "build", "-t", tag, "-f", str(output / "Dockerfile"), str(output), timeout=600,
        )
        (output / "build.log").write_text(built)
        registry_attempted = True
        _docker(
            "run",
            "--detach",
            "--rm",
            "--name",
            registry,
            "--network",
            "host",
            "-e",
            "REGISTRY_HTTP_ADDR=127.0.0.1:0",
            "registry:2",
        )
        listener = None
        for _ in range(50):
            result = subprocess.run(
                [DOCKER, "logs", registry],
                capture_output=True,
                text=True,
                check=False,
                timeout=10,
            )
            listener = re.search(r"listening on 127\.0\.0\.1:(\d+)", result.stdout + result.stderr)
            if listener:
                break
            time.sleep(0.1)
        if listener is None:
            raise RuntimeError("validation loopback image registry did not become ready")
        repository = f"127.0.0.1:{listener[1]}/coordinare-env-validation-{nonce}"
        published_tag = repository + ":candidate"
        owned_refs.add(published_tag)
        _docker("tag", tag, published_tag)
        pushed = _docker("push", published_tag, timeout=600)
        digest = re.search(r"digest: (sha256:[0-9a-f]{64})", pushed)
        if digest is None:
            raise RuntimeError("validation registry did not report an immutable manifest")
        image = repository + "@" + digest[1]
        owned_refs.add(image)
        _docker("pull", image, timeout=600)
        aliases = json.loads(_docker("image", "inspect", published_tag, "--format", "{{json .RepoDigests}}"))
        owned_refs.update(ref for ref in (aliases or []) if ref.startswith(repository + "@sha256:"))
        identity = verify_image_code(image)
        identity["base_image"] = base_image
        (output / "identity.json").write_text(json.dumps(identity, indent=2))
        yield image
    finally:
        cleanup = []
        for reference in sorted(owned_refs):
            result = subprocess.run(
                [DOCKER, "image", "rm", reference],
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
            cleanup.append({"kind": "image_ref", "name": reference, "exit_code": result.returncode})
        if registry_attempted:
            result = subprocess.run(
                [DOCKER, "rm", "-f", "-v", registry],
                capture_output=True,
                text=True,
                check=False,
                timeout=60,
            )
            cleanup.append({"kind": "registry", "name": registry, "exit_code": result.returncode})
        (output / "cleanup.json").write_text(json.dumps(cleanup, indent=2))
