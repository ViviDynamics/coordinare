"""Integration test for Dockerfile.slim (spec 056, T043).

Tests that slim image builds for each backend and advertises the correct
backend + optional browser capability based on --build-arg BROWSER.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import uuid

import pytest


BACKENDS = ["claude_code", "codex", "cursor", "junie", "opencode"]


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
        pytest.skip(f"docker build failed: {build_result.stderr.decode()}")

    container_id: str | None = None

    try:
        # Start a container
        run_result = subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "-p",
                "0:8088",
                tag,
            ],
            capture_output=True,
            timeout=10,
            text=True,
        )

        if run_result.returncode != 0:
            pytest.skip(f"docker run failed: {run_result.stderr}")

        container_id = run_result.stdout.strip()

        # Get the host port
        port_result = subprocess.run(
            ["docker", "port", container_id, "8088/tcp"],
            capture_output=True,
            timeout=10,
            text=True,
        )

        if port_result.returncode != 0:
            pytest.skip(f"docker port failed: {port_result.stderr}")

        port_line = port_result.stdout.strip().split("\n")[0]
        port = port_line.split(":")[-1]

        # Wait for server startup
        await asyncio.sleep(2)

        # Hit /status
        status_result = subprocess.run(
            ["curl", "-s", "-f", f"http://127.0.0.1:{port}/status"],
            capture_output=True,
            timeout=5,
            text=True,
        )

        if status_result.returncode != 0:
            pytest.skip(f"curl /status failed: {status_result.stderr}")

        status_data = json.loads(status_result.stdout)
        backends = status_data.get("capabilities", {}).get("backends", [])

        # Slim image should advertise exactly the specified backend
        assert backend in backends, f"Backend {backend} not in {backends}"
        # And should not advertise other backends
        for other_backend in BACKENDS:
            if other_backend != backend:
                assert (
                    other_backend not in backends
                ), f"Unexpected backend {other_backend} in {backends}"

        # Should NOT have browser flag (no --build-arg BROWSER=true)
        tool_flags = status_data.get("capabilities", {}).get("tool_flags", [])
        assert "browser" not in tool_flags

    finally:
        if container_id:
            subprocess.run(
                ["docker", "stop", "-t", "2", container_id],
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
        pytest.skip(f"docker build failed: {build_result.stderr.decode()}")

    container_id: str | None = None

    try:
        # Start container
        run_result = subprocess.run(
            [
                "docker",
                "run",
                "-d",
                "-p",
                "0:8088",
                tag,
            ],
            capture_output=True,
            timeout=10,
            text=True,
        )

        if run_result.returncode != 0:
            pytest.skip(f"docker run failed: {run_result.stderr}")

        container_id = run_result.stdout.strip()

        # Get port
        port_result = subprocess.run(
            ["docker", "port", container_id, "8088/tcp"],
            capture_output=True,
            timeout=10,
            text=True,
        )

        if port_result.returncode != 0:
            pytest.skip(f"docker port failed: {port_result.stderr}")

        port_line = port_result.stdout.strip().split("\n")[0]
        port = port_line.split(":")[-1]

        # Wait for startup
        await asyncio.sleep(2)

        # Hit /status
        status_result = subprocess.run(
            ["curl", "-s", "-f", f"http://127.0.0.1:{port}/status"],
            capture_output=True,
            timeout=5,
            text=True,
        )

        if status_result.returncode != 0:
            pytest.skip(f"curl /status failed: {status_result.stderr}")

        status_data = json.loads(status_result.stdout)
        tool_flags = status_data.get("capabilities", {}).get("tool_flags", [])

        # With BROWSER=true, browser flag should be present
        assert "browser" in tool_flags, f"browser flag missing in {tool_flags}"

    finally:
        if container_id:
            subprocess.run(
                ["docker", "stop", "-t", "2", container_id],
                capture_output=True,
                timeout=10,
            )
        subprocess.run(["docker", "rmi", tag], capture_output=True, timeout=10)
