"""Integration test for Dockerfile.slim (spec 056, T043).

Tests that slim image builds for each backend and advertises the correct
backend + optional browser capability based on --build-arg BROWSER.
"""

from __future__ import annotations

import subprocess
import uuid

import pytest

from tests.conftest import resolve_published_port, wait_for_status


BACKENDS = ["claude_code", "codex", "junie", "opencode"]


@pytest.mark.timeout(600)
@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.asyncio
async def test_dockerfile_slim_builds_all_backends(
    backend: str, require_docker: None, performer_base_image_built: str
) -> None:
    """Test that Dockerfile.slim builds successfully for each backend."""
    tag = f"coordinare-performer:slim-{backend}-test-{uuid.uuid4().hex[:8]}"

    # Build slim image for this backend
    result = subprocess.run(
        [
            "docker",
            "build",
            "-f",
            "agent/performer/Dockerfile.slim",
            "-t",
            tag,
            "--build-arg",
            f"BACKEND={backend}",
            "agent/performer/",
        ],
        capture_output=True,
        timeout=300,
        text=True,
    )

    try:
        assert result.returncode == 0, f"docker build failed: {result.stderr}"
    finally:
        subprocess.run(["docker", "rmi", tag], capture_output=True, timeout=10)


@pytest.mark.timeout(600)
@pytest.mark.parametrize("backend", BACKENDS)
@pytest.mark.asyncio
async def test_dockerfile_slim_advertises_correct_backend(
    backend: str, require_docker: None, performer_base_image_built: str
) -> None:
    """Test that slim image /status advertises exactly the specified backend."""
    tag = f"coordinare-performer:slim-{backend}-test-{uuid.uuid4().hex[:8]}"

    # Build the slim image
    build_result = subprocess.run(
        [
            "docker",
            "build",
            "-f",
            "agent/performer/Dockerfile.slim",
            "-t",
            tag,
            "--build-arg",
            f"BACKEND={backend}",
            "agent/performer/",
        ],
        capture_output=True,
        timeout=300,
    )

    if build_result.returncode != 0:
        pytest.fail(f"docker build failed: {build_result.stderr.decode()}")

    container_id: str | None = None

    try:
        run_result = subprocess.run(
            ["docker", "run", "-d", "--rm", "-p", "0:8088", tag],
            capture_output=True,
            timeout=10,
            text=True,
        )

        if run_result.returncode != 0:
            pytest.fail(f"docker run failed: {run_result.stderr}")

        container_id = run_result.stdout.strip()

        port = resolve_published_port(container_id, 8088)

        status_data = await wait_for_status(port, container_id=container_id)
        backends = status_data.get("capabilities", {}).get("backends", [])

        assert backend in backends, f"Backend {backend} not in {backends}"
        for other_backend in BACKENDS:
            if other_backend != backend:
                assert other_backend not in backends, (
                    f"Unexpected backend {other_backend} in {backends}"
                )

        tool_flags = status_data.get("capabilities", {}).get("tool_flags", [])
        assert "browser" not in tool_flags

    finally:
        if container_id:
            subprocess.run(
                ["docker", "rm", "-f", container_id],
                capture_output=True,
                timeout=10,
            )
        subprocess.run(["docker", "rmi", tag], capture_output=True, timeout=10)


@pytest.mark.timeout(900)
@pytest.mark.parametrize("backend", ["claude_code"])
@pytest.mark.asyncio
async def test_dockerfile_slim_browser_toggle(
    backend: str, require_docker: None, performer_base_image_built: str
) -> None:
    """Test that --build-arg BROWSER=true adds the browser capability (one backend — behavior is backend-agnostic)."""
    tag = f"coordinare-performer:slim-{backend}-browser-test-{uuid.uuid4().hex[:8]}"

    # Build slim image with BROWSER=true (Playwright install can take > 5 min)
    build_result = subprocess.run(
        [
            "docker",
            "build",
            "-f",
            "agent/performer/Dockerfile.slim",
            "-t",
            tag,
            "--build-arg",
            f"BACKEND={backend}",
            "--build-arg",
            "BROWSER=true",
            "agent/performer/",
        ],
        capture_output=True,
        timeout=900,
    )

    if build_result.returncode != 0:
        pytest.fail(f"docker build failed: {build_result.stderr.decode()}")

    container_id: str | None = None

    try:
        run_result = subprocess.run(
            ["docker", "run", "-d", "--rm", "-p", "0:8088", tag],
            capture_output=True,
            timeout=10,
            text=True,
        )

        if run_result.returncode != 0:
            pytest.fail(f"docker run failed: {run_result.stderr}")

        container_id = run_result.stdout.strip()

        port = resolve_published_port(container_id, 8088)

        # Slim+browser is heavier than plain slim (Playwright + chromium), so
        # first-boot can exceed the 90s default. Observed flake during the 065
        # Fix 13 bin/build run; 180s gives margin without slowing the suite.
        status_data = await wait_for_status(
            port, timeout=180.0, container_id=container_id
        )
        tool_flags = status_data.get("capabilities", {}).get("tool_flags", [])

        assert "browser" in tool_flags, f"browser flag missing in {tool_flags}"

    finally:
        if container_id:
            subprocess.run(
                ["docker", "rm", "-f", container_id],
                capture_output=True,
                timeout=10,
            )
        subprocess.run(["docker", "rmi", tag], capture_output=True, timeout=10)
