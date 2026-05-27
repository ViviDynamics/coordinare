"""Shared pytest fixtures for the performer test suite."""
from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
PERFORMER_DIR = REPO_ROOT / "agent" / "performer"
BASE_IMAGE_TAG = "coordinare-performer:base"


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return result.returncode == 0


@pytest.fixture(scope="session")
def docker_available() -> bool:
    return _docker_available()


@pytest.fixture
def require_docker(docker_available: bool) -> None:
    if not docker_available:
        pytest.skip("Docker daemon not reachable; skipping containerized test")


@pytest.fixture(scope="session")
def performer_base_image_built(docker_available: bool) -> str:
    """Build coordinare-performer:base once per session.

    Slim and full Dockerfiles inherit ``FROM coordinare-performer:base``, so the
    base image must exist locally before any derived build can succeed.
    """
    if not docker_available:
        pytest.skip("Docker daemon not reachable; skipping containerized test")
    dockerfile = PERFORMER_DIR / "Dockerfile.base"
    result = subprocess.run(
        [
            "docker",
            "build",
            "-f",
            str(dockerfile),
            "-t",
            BASE_IMAGE_TAG,
            str(REPO_ROOT),
        ],
        capture_output=True,
        timeout=900,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(
            f"failed to build {BASE_IMAGE_TAG}: {result.stderr.decode()[-500:]}"
        )
    return BASE_IMAGE_TAG


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


def resolve_published_port(
    container_id: str, internal_port: int, timeout: float = 10.0
) -> str:
    """Poll ``docker port`` until the ephemeral mapping is published.

    ``docker run -d -p 0:<port>`` returns the container ID before the port
    binding is actually wired through, so a naive ``docker port`` call races
    and returns empty stdout — which then yields a malformed URL downstream
    (curl hits ``http://127.0.0.1:/status`` and the request fails opaquely).
    Poll briefly until docker reports the mapping.
    """
    deadline = time.monotonic() + timeout
    last_stderr = ""
    while time.monotonic() < deadline:
        port_result = subprocess.run(
            ["docker", "port", container_id, f"{internal_port}/tcp"],
            capture_output=True,
            timeout=10,
            text=True,
        )
        if port_result.returncode == 0 and port_result.stdout.strip():
            return port_result.stdout.strip().split("\n")[0].split(":")[-1]
        last_stderr = port_result.stderr
        time.sleep(0.2)
    pytest.fail(
        f"docker port never reported a mapping for {internal_port}/tcp: {last_stderr}"
    )


async def wait_for_status(
    port: str, timeout: float = 90.0, container_id: str | None = None
) -> dict:
    """Poll GET /status until the server responds or *timeout* seconds elapse.

    Raises pytest.fail (not skip) on timeout so the test counts as a failure.
    Uses a 2-second interval; the first attempt fires immediately.

    If *container_id* is provided, the container's Running state is checked on
    every poll cycle: a container that has already exited fails fast (no point
    waiting the full timeout), and on any failure the container's stderr/stdout
    logs are included in the pytest.fail message so the failure is diagnosable
    instead of opaque.
    """
    deadline = time.monotonic() + timeout
    last_stderr = ""
    while True:
        result = subprocess.run(
            ["curl", "-sS", "-f", f"http://127.0.0.1:{port}/status"],
            capture_output=True,
            timeout=5,
            text=True,
        )
        if result.returncode == 0:
            return json.loads(result.stdout)
        last_stderr = result.stderr

        if container_id:
            inspect = subprocess.run(
                ["docker", "inspect", "-f", "{{.State.Running}}", container_id],
                capture_output=True,
                timeout=5,
                text=True,
            )
            if inspect.returncode == 0 and inspect.stdout.strip() == "false":
                pytest.fail(
                    f"Container {container_id[:12]} exited before /status responded.\n"
                    f"curl stderr: {last_stderr}\n"
                    f"docker logs:\n{_docker_logs(container_id)}"
                )

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            logs_tail = (
                f"\ndocker logs:\n{_docker_logs(container_id)}" if container_id else ""
            )
            pytest.fail(
                f"Server on port {port} did not respond within {timeout:.0f}s.\n"
                f"curl stderr: {last_stderr}{logs_tail}"
            )
        await asyncio.sleep(min(2.0, remaining))


def _docker_logs(container_id: str) -> str:
    """Return the tail of a container's combined stdout/stderr for diagnostics."""
    result = subprocess.run(
        ["docker", "logs", "--tail", "100", container_id],
        capture_output=True,
        timeout=10,
        text=True,
    )
    combined = (result.stdout or "") + (result.stderr or "")
    return combined.strip() or "(no logs)"
