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


async def wait_for_status(
    container_id: str, *, timeout: float = 90.0, internal_port: int = 8088
) -> dict:
    """Poll GET /status *inside the container* until it responds or *timeout* elapses.

    The probe runs ``docker exec <container_id> curl … http://127.0.0.1:<port>/status``
    so it hits the container's own loopback, deliberately bypassing Docker's
    host-port publishing. Host-port forwarding on a shared Docker Desktop host is
    unreliable under container churn: an ephemeral host port assigned to this
    container (``-p 0:8088``) can be misrouted to — or reused from — an unrelated
    container that happens to publish the same internal port, and those foreign
    servers routinely return HTTP 404 on ``/status``. That surfaces as a hang
    until the full timeout wall with ``curl: (22) 404`` even though *this*
    container's uvicorn is already serving ("Application startup complete").
    The failure moved from backend to backend run-to-run because it tracked
    Docker's ephemeral-port allocation, not any real defect. Probing inside the
    container removes that entire class of race and makes the test deterministic.

    Raises pytest.fail (not skip) on timeout so the test counts as a failure.
    Uses a 2-second interval; the first attempt fires immediately.

    The container's Running state is checked on every poll cycle: a container
    that has already exited fails fast (no point waiting the full timeout), and
    on any failure the container's stderr/stdout logs are included in the
    pytest.fail message so the failure is diagnosable instead of opaque.
    """
    url = f"http://127.0.0.1:{internal_port}/status"
    deadline = time.monotonic() + timeout
    last_stderr = ""
    while True:
        result = subprocess.run(
            ["docker", "exec", container_id, "curl", "-sS", "-f", "--max-time", "5", url],
            capture_output=True,
            timeout=15,
            text=True,
        )
        if result.returncode == 0:
            return json.loads(result.stdout)
        last_stderr = result.stderr

        inspect = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Running}}", container_id],
            capture_output=True,
            timeout=5,
            text=True,
        )
        if inspect.returncode == 0 and inspect.stdout.strip() == "false":
            pytest.fail(
                f"Container {container_id[:12]} exited before /status responded.\n"
                f"exec curl stderr: {last_stderr}\n"
                f"docker logs:\n{_docker_logs(container_id)}"
            )

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            pytest.fail(
                f"/status inside container {container_id[:12]} did not respond "
                f"within {timeout:.0f}s.\n"
                f"exec curl stderr: {last_stderr}\n"
                f"docker logs:\n{_docker_logs(container_id)}"
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
