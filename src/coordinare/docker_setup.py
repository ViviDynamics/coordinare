from __future__ import annotations

import asyncio
import errno
import json
import os
import re
import shutil
import subprocess
import sys
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from coordinare.init_diagnostics import DOCKER_DISK_FIX, init_stage, safe_cause
from coordinare.init_files import SetupError, fail
from coordinare.lib.container_env import docker_environment
from coordinare.metrics import _coordinare_version

if TYPE_CHECKING:
    from coordinare.config import ProjectConfiguration
    from coordinare.models.performer_endpoint import PerformerEndpointConfig

IMAGE_REPOSITORY = "ghcr.io/vividynamics/coordinare-performer"
PROBE_TIMEOUT_SECONDS = 10.0
PULL_TIMEOUT_SECONDS = 600.0
GIB = 1024**3
DISK_BUDGET_GIB = {"base": 4, "full": 10}
DockerRunner = Callable[[list[str], float], subprocess.CompletedProcess[str]]


def run_docker(command: list[str], timeout: float) -> subprocess.CompletedProcess[str]:
    """The only Docker subprocess boundary used by init provisioning."""
    return asyncio.run(_docker_process(command, timeout))


async def _docker_process(command: list[str], duration_seconds: float) -> subprocess.CompletedProcess[str]:
    process = await asyncio.create_subprocess_exec(
        "docker", *command[1:], stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        async with asyncio.timeout(duration_seconds):
            stdout, stderr = await process.communicate()
    except TimeoutError as exc:
        process.kill()
        await process.wait()
        raise subprocess.TimeoutExpired(command, duration_seconds) from exc
    return subprocess.CompletedProcess(command, process.returncode or 0, stdout.decode(errors="replace"), stderr.decode(errors="replace"))


def image_tag(explicit: str | None) -> str:
    tag = _coordinare_version() if explicit is None else explicit
    if explicit is None and re.fullmatch(r"[0-9]{4}\.[0-9]{1,2}\.[0-9]+(?:\.[0-9]+)?", tag) is None:
        fail("This development or prerelease version has no published image. Pick a published release with --image-tag VERSION.")
    if tag.lower() == "latest" or re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", tag) is None:
        fail("Choose a fixed image tag with --image-tag VERSION; latest and invalid tags are refused.")
    return tag


def _known_docker_failure(stderr: str | bytes | None, exc: OSError | None = None) -> None:
    """Recognize causes, but emit only fixed text, never captured Docker output."""
    error = (stderr.decode(errors="replace") if isinstance(stderr, bytes) else stderr or "").lower()
    if (exc is not None and exc.errno == errno.ENOSPC) or "no space left" in error:
        fail("Docker ran out of disk space.", cause="ENOSPC: " + os.strerror(errno.ENOSPC), fix=DOCKER_DISK_FIX)
    if ("permission denied" in error and any(part in error for part in ("socket", "connect", "daemon"))) or "access is denied" in error:
        fail("Docker socket permission denied.", cause="Docker socket permission denied", fix="Grant your user Docker access or select a usable Docker context, then rerun init.")
    if any(part in error for part in ("manifest unknown", "manifest_unknown", "no matching manifest")):
        fail("No published image for this tag or platform.", cause="manifest unknown", fix="Choose a published release with --image-tag VERSION and check Docker's architecture.")
    if any(part in error for part in ("pull access denied", "denied: requested access", "requested access to the resource is denied", "unauthorized: authentication required")):
        fail("Registry access denied.", cause="registry access denied", fix="Run docker login ghcr.io with a token granting read:packages and package access, then rerun init. Public packages require no login.")
    if (exc is not None and exc.errno == errno.ENETUNREACH) or any(part in error for part in ("network unreachable", "network is unreachable")):
        fail("Docker network unreachable.", cause="network unreachable", fix="Restore the Docker daemon's network access and check Docker proxy settings, then rerun init.")


def _invoke(runner: DockerRunner, arguments: list[str], timeout: float = PROBE_TIMEOUT_SECONDS, *, retry_timeout: bool = False) -> subprocess.CompletedProcess[str]:
    try:
        result = runner(["docker", *arguments], timeout)
    except subprocess.CalledProcessError as exc:
        _known_docker_failure(exc.stderr)
        fail("Docker command failed.", cause=safe_cause(exc), fix="Check Docker service status, registry access, network settings and disk space, then rerun init.")
    except subprocess.TimeoutExpired as exc:
        _known_docker_failure(exc.stderr)
        if retry_timeout:
            raise
        fail("Docker " + arguments[0] + " timed out. Check Docker, network access and disk space, then rerun init.", cause=safe_cause(exc))
    except OSError as exc:
        _known_docker_failure(str(exc), exc)
        if isinstance(exc, FileNotFoundError):
            fail("Docker is missing. Install Docker Desktop or Docker Engine and ensure docker is on PATH, then rerun init.", cause=safe_cause(exc))
        if isinstance(exc, PermissionError):
            fail("Cannot execute Docker: check executable permissions on docker and its parent directories, then rerun init.", cause=safe_cause(exc))
        fail("Cannot execute Docker. Check installation, executable permissions and your Docker context, then rerun init.", cause=safe_cause(exc))
    except ValueError as exc:
        _known_docker_failure(str(exc))
        raise
    if result.returncode:
        _known_docker_failure(result.stderr)
    return result


def _check_runtime(runner: DockerRunner, directory: Path, variant: str) -> None:
    result = _invoke(runner, ["info", "--format", "{{json .}}"])
    if result.returncode:
        if "permission denied" in result.stderr.lower() or "access is denied" in result.stderr.lower():
            fail("Docker socket permission denied. Grant your user Docker access or select a usable Docker context, then rerun init.")
        fail("Docker is not running or its context is unreachable. Start Docker Desktop or the Docker Engine service; check docker context ls and docker info, then rerun init.")
    try:
        info = json.loads(result.stdout)
    except ValueError:
        fail("Cannot read Docker info. Check docker info and update Docker before rerunning init.")
    root = info.get("DockerRootDir") if isinstance(info, dict) else None
    init_stage.set("docker_disk")
    disk_path = Path(root) if isinstance(root, str) and Path(root).is_dir() else directory
    while not disk_path.exists():
        disk_path = disk_path.parent
    required = DISK_BUDGET_GIB[variant]
    if shutil.disk_usage(disk_path).free < required * GIB:
        fail(f"Insufficient disk space for the {variant} image. Free at least {required} GiB on the Docker/data filesystem and rerun init. Review unused images with docker system df.")
    sys.stdout.write(f"Docker is usable. Disk budget: {required} GiB. For Docker Desktop or a remote context, also check the daemon's disk allocation.\n")


def _pull(runner: DockerRunner, image: str) -> None:
    sys.stdout.write(f"Pulling {image}; this can take several minutes.\n")
    sys.stdout.flush()
    result = _invoke(runner, ["pull", image], PULL_TIMEOUT_SECONDS)
    if not result.returncode:
        return
    error = result.stderr.lower()
    if any(word in error for word in ("tls", "x509", "certificate")):
        fail("Docker pull failed TLS certificate verification. Check registry certificates and your TLS proxy configuration, then rerun init.")
    if "proxy" in error:
        fail("Docker pull failed through the proxy. Check Docker's proxy settings and network access, then rerun init.")
    if any(word in error for word in ("denied", "unauthorized", "forbidden")):
        fail("Registry access denied. Run docker login ghcr.io with a token granting read:packages and package access, then rerun init. Public packages require no login.")
    fail("Docker pull failed. Check network access to ghcr.io, proxy settings and disk space, then rerun init. No local build is needed.")


def _inspect(runner: DockerRunner, image: str, repository: str) -> str:
    result = _invoke(runner, ["image", "inspect", image])
    if result.returncode:
        fail("Docker image inspect failed. Check docker image inspect for the selected tag, then rerun init.")
    try:
        records: Any = json.loads(result.stdout)
        record = records[0]
        entrypoint = record["Config"]["Entrypoint"]
        digests = record.get("RepoDigests") or []
    except (ValueError, KeyError, IndexError, TypeError):
        fail("Cannot verify Docker image inspect metadata. Pull the selected release again and rerun init.")
    if not isinstance(digests, list):
        fail("Cannot verify Docker image inspect digest metadata. Pull the selected release again and rerun init.")
    if not isinstance(entrypoint, list) or not any(str(part).endswith("/entrypoint.sh") for part in entrypoint):
        fail("This image lacks the harness setup entrypoint. Use --image-variant full or choose a newer published release with --image-tag VERSION.")
    pinned = image
    for digest in digests:
        if isinstance(digest, str) and re.fullmatch(re.escape(repository) + r"@sha256:[0-9a-f]{64}", digest):
            pinned = image + "@" + digest.split("@", 1)[1]
            break
    sys.stdout.write("Image inspected: harness setup entrypoint present. " + ("Registry digest pinned.\n" if pinned != image else "No repository digest reported; version tag retained.\n"))
    return pinned


def prepare_image(variant: str, tag: str | None, directory: Path, *, runner: DockerRunner | None = None) -> str:
    """Preflight, pull and inspect without ever falling back to a moving tag."""
    init_stage.set("image_pull")
    if variant not in DISK_BUDGET_GIB:
        fail("image_variant must be base or full. Use --image-variant full for browser and security tooling.")
    selected = image_tag(tag)
    repository = IMAGE_REPOSITORY + "-" + variant
    image = repository + ":" + selected
    command_runner = runner or run_docker
    init_stage.set("docker_runtime")
    _check_runtime(command_runner, directory, variant)
    init_stage.set("image_pull")
    _pull(command_runner, image)
    init_stage.set("image_inspect")
    return _inspect(command_runner, image, repository)


START_TIMEOUT_SECONDS = 300.0
POLL_INTERVAL_SECONDS = 1.0
# Executed inside the container. Tokens are read from its environment, never argv.
SMOKE_PROBE = """
import json
import os
import urllib.request

def get(url, headers):
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=3) as response:
        return json.load(response)

token = os.environ.get('PERFORMER_AUTH_TOKEN', '')
headers = {'Authorization': 'Bearer ' + token} if token else {}
get('http://127.0.0.1:8088/healthz', headers)
status = get('http://127.0.0.1:8088/status', headers)
assert status['availability'] == 'idle', 'Performer is not idle'
assert os.environ['BACKEND'] in status['capabilities']['backends'], 'Harness is not ready'
if token and not status.get('auth_enabled'):
    raise RuntimeError('Performer authentication is disabled')
if os.environ.get('COORDINARE_SMOKE_GITHUB_TOKEN'):
    get(os.environ.get('COORDINARE_SMOKE_GITHUB_URL', 'https://api.github.com/user'), {'Authorization': 'Bearer ' + os.environ['COORDINARE_SMOKE_GITHUB_TOKEN']})
if os.environ.get('COORDINARE_SMOKE_MODEL_URL'):
    key = os.environ.get('COORDINARE_SMOKE_MODEL_TOKEN', '')
    model_headers = {'Authorization': 'Bearer ' + key} if key else {}
    if os.environ.get('COORDINARE_SMOKE_MODEL_KIND') == 'anthropic':
        model_headers = {'x-api-key': key, 'anthropic-version': '2023-06-01'}
    get(os.environ['COORDINARE_SMOKE_MODEL_URL'], model_headers)
"""


def smoke_environment(config: ProjectConfiguration) -> dict[str, str]:
    """Pass only the starter model and GitHub credentials to the setup probe."""
    token = config.github_token
    if token is None:
        fail("A GitHub token is required for the container connectivity probe.")
    values = {
        "COORDINARE_SMOKE_GITHUB_TOKEN": token.get_secret_value(),
        "COORDINARE_SMOKE_GITHUB_URL": config.github_api_url.rstrip("/") + "/user",
    }
    for endpoint in config.endpoints:
        if endpoint.name != "starter":
            continue
        if endpoint.kind == "anthropic":
            url = "https://api.anthropic.com/v1/models"
        elif endpoint.kind == "openai":
            url = "https://api.openai.com/v1/models"
        else:
            url = str(endpoint.base_url).rstrip("/") + "/models"
        values.update({
            "COORDINARE_SMOKE_MODEL_URL": url,
            "COORDINARE_SMOKE_MODEL_KIND": endpoint.kind,
            "COORDINARE_SMOKE_MODEL_TOKEN": os.environ.get(endpoint.auth_env or "", ""),
        })
    return values


def smoke_container(
    endpoint: PerformerEndpointConfig,
    *,
    connectivity: dict[str, str] | None = None,
    runner: DockerRunner | None = None,
) -> None:
    """Exercise the real entrypoint and harness, with bounded probes and cleanup."""
    init_stage.set("container_smoke")
    command_runner = runner or run_docker
    name = "coordinare-setup-" + uuid.uuid4().hex
    arguments = ["run", "-d", "--rm", "--name", name, "--label", "coordinare.setup_smoke=true"]
    values = {**endpoint.env, **(connectivity or {})}
    if endpoint.auth_token is not None:
        values["PERFORMER_AUTH_TOKEN"] = endpoint.auth_token.get_secret_value()
    for entry in endpoint.extra_hosts:
        arguments += ["--add-host", entry]
    for mount in endpoint.volumes:
        arguments += ["-v", f"{mount.host_path}:{mount.container_path}:{mount.mode}"]
    deadline = time.monotonic() + START_TIMEOUT_SECONDS
    try:
        with docker_environment(values) as env_path:
            if env_path is not None:
                arguments += ["--env-file", str(env_path)]
            arguments += [str(endpoint.image)]
            result = _invoke(command_runner, arguments)
        if result.returncode or not result.stdout.strip():
            fail("Performer smoke container did not start. Check Docker and the selected image; rerun init or use --skip-smoke.")
        while (remaining := deadline - time.monotonic()) > 0:
            try:
                probe = _invoke(command_runner, ["exec", name, "python", "-c", SMOKE_PROBE], min(PROBE_TIMEOUT_SECONDS, remaining), retry_timeout=True)
            except subprocess.TimeoutExpired:
                probe = None
            if probe is not None and probe.returncode == 0:
                sys.stdout.write("Performer smoke passed: /healthz and /status answered, harness ready.\n")
                return
            time.sleep(min(POLL_INTERVAL_SECONDS, max(0.0, deadline - time.monotonic())))
        fail("Performer smoke timed out waiting for harness readiness and container GitHub/model access. Check network, credentials and harness installation; rerun init or use --skip-smoke.")
    finally:
        # Clean up by name even when `docker run` timed out before returning its id.
        primary_error = sys.exc_info()[0] is not None
        try:
            cleanup = _invoke(command_runner, ["rm", "-f", name])
            if cleanup.returncode and "no such container" not in cleanup.stderr.lower():
                fail("Performer smoke cleanup failed. Remove the container named " + name + " with docker rm -f before retrying.")
        except SetupError:
            if not primary_error:
                raise
            sys.stderr.write("Performer smoke cleanup failed; run docker rm -f " + name + ".\n")
