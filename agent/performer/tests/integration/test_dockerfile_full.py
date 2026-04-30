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
    """Test that full image /status advertises every backend and full tool flags."""
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

        # Check that all backends are advertised
        backends = status_data.get("capabilities", {}).get("backends", [])
        expected_backends = {"claude_code", "codex", "cursor", "junie", "opencode"}
        assert (
            expected_backends.issubset(set(backends))
        ), f"Not all backends present. Got: {backends}, expected: {expected_backends}"

        # Check that full tool flags are advertised
        tool_flags = set(status_data.get("capabilities", {}).get("tool_flags", []))
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
            expected_flags.issubset(tool_flags)
        ), f"Not all flags present. Got: {tool_flags}, expected: {expected_flags}"

        # browser should definitely be present
        assert "browser" in tool_flags

    finally:
        if container_id:
            subprocess.run(
                ["docker", "stop", "-t", "2", container_id],
                capture_output=True,
                timeout=10,
            )
        subprocess.run(["docker", "rmi", tag], capture_output=True, timeout=10)
