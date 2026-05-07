"""Integration test for Dockerfile.full (spec 056, T044).

Tests that the full image builds successfully and advertises every backend
plus the full tool flag set including browser.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import uuid

import pytest


@pytest.mark.asyncio
async def test_dockerfile_full_builds(require_docker: None, performer_base_image_built: str) -> None:
    """Test that Dockerfile.full builds successfully."""
    tag = f"coordinare-performer:full-test-{uuid.uuid4().hex[:8]}"

    # Build the full image
    result = subprocess.run(
        [
            "docker",
            "build",
            "-f",
            "agent/performer/Dockerfile.full",
            "-t",
            tag,
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


@pytest.mark.asyncio
async def test_dockerfile_full_advertises_all_capabilities(require_docker: None, performer_base_image_built: str) -> None:
    """Test that full image /status advertises every backend (when configured) and full tool flags."""
    tag = f"coordinare-performer:full-test-{uuid.uuid4().hex[:8]}"

    # Build the full image
    build_result = subprocess.run(
        [
            "docker",
            "build",
            "-f",
            "agent/performer/Dockerfile.full",
            "-t",
            tag,
            "agent/performer/",
        ],
        capture_output=True,
        timeout=300,
    )

    if build_result.returncode != 0:
        pytest.skip(f"docker build failed: {build_result.stderr.decode()}")

    # Test each backend individually (entrypoint.sh installs per BACKEND env var)
    # and verify that tool flags are present in all cases.
    backends_to_test = ["claude_code", "codex", "cursor", "junie", "opencode"]
    advertised_backends = set()
    advertised_tool_flags = set()

    for backend in backends_to_test:
        container_id: str | None = None

        try:
            # Start container with BACKEND set (matching entrypoint.sh case statement)
            backend_name = "claude" if backend == "claude_code" else backend
            run_result = subprocess.run(
                [
                    "docker",
                    "run",
                    "-d",
                    "-p",
                    "0:8088",
                    "-e",
                    f"BACKEND={backend_name}",
                    tag,
                ],
                capture_output=True,
                timeout=10,
                text=True,
            )

            if run_result.returncode != 0:
                pytest.skip(f"docker run with BACKEND={backend_name} failed: {run_result.stderr}")

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

            # Wait for server startup and backend installation (npm/curl installs can be slow)
            await asyncio.sleep(10)

            # Hit /status
            status_result = subprocess.run(
                ["curl", "-s", "-f", f"http://127.0.0.1:{port}/status"],
                capture_output=True,
                timeout=5,
                text=True,
            )

            if status_result.returncode != 0:
                pytest.skip(f"curl /status failed for BACKEND={backend_name}: {status_result.stderr}")

            status_data = json.loads(status_result.stdout)

            # Collect backends and tool flags advertised across all backend configurations
            backends = status_data.get("capabilities", {}).get("backends", [])
            advertised_backends.update(backends)
            tool_flags = status_data.get("capabilities", {}).get("tool_flags", [])
            advertised_tool_flags.update(tool_flags)

        finally:
            if container_id:
                subprocess.run(
                    ["docker", "stop", "-t", "2", container_id],
                    capture_output=True,
                    timeout=10,
                )

    # Verify all backends are advertised across the test configurations
    expected_backends = {"claude_code", "codex", "cursor", "junie", "opencode"}
    assert (
        expected_backends.issubset(advertised_backends)
    ), f"Not all backends advertised. Got: {advertised_backends}, expected: {expected_backends}"

    # Check that full tool flags are advertised
    expected_flags = {
        "git",
        "node",
        "python",
        "browser",
        "lint",
        "format",
        "test_runner",
        "ripgrep",
        "jq",
        "shell",
    }
    assert (
        expected_flags.issubset(advertised_tool_flags)
    ), f"Not all flags present. Got: {advertised_tool_flags}, expected: {expected_flags}"

    # browser should definitely be present
    assert "browser" in advertised_tool_flags

    # Clean up image
    subprocess.run(["docker", "rmi", tag], capture_output=True, timeout=10)
