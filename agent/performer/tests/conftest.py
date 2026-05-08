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
            str(PERFORMER_DIR),
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


async def wait_for_status(port: str, timeout: float = 90.0) -> dict:
    """Poll GET /status until the server responds or *timeout* seconds elapse.

    Raises pytest.fail (not skip) on timeout so the test counts as a failure.
    Uses a 2-second interval; the first attempt fires immediately.
    """
    deadline = time.monotonic() + timeout
    last_stderr = ""
    while True:
        result = subprocess.run(
            ["curl", "-s", "-f", f"http://127.0.0.1:{port}/status"],
            capture_output=True,
            timeout=5,
            text=True,
        )
        if result.returncode == 0:
            return json.loads(result.stdout)
        last_stderr = result.stderr
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            pytest.fail(
                f"Server on port {port} did not respond within {timeout:.0f}s: {last_stderr}"
            )
        await asyncio.sleep(min(2.0, remaining))
