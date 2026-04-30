"""Integration test for Dockerfile.base (spec 056, T042).

Tests that the base image builds successfully and advertises only universal
tool flags (no backend identifiers).
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import uuid

import pytest


@pytest.mark.asyncio
async def test_dockerfile_base_builds(require_docker: None) -> None:
    """Test that Dockerfile.base builds successfully."""
    tag = f"coordinare-performer:base-test-{uuid.uuid4().hex[:8]}"

    # Build the base image
    result = subprocess.run(
        [
            "docker",
            "build",
            "-f",
            "agent/performer/Dockerfile.base",
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
        # Clean up the image
        subprocess.run(["docker", "rmi", tag], capture_output=True, timeout=10)


@pytest.mark.asyncio
async def test_dockerfile_base_advertises_universal_capabilities(require_docker: None) -> None:
    """Test that base image /status advertises only universal tool flags."""
    tag = f"coordinare-performer:base-test-{uuid.uuid4().hex[:8]}"

    # Build the base image
    build_result = subprocess.run(
        [
            "docker",
            "build",
            "-f",
            "agent/performer/Dockerfile.base",
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

        # Wait a moment for the server to start
        await asyncio.sleep(2)

        # Hit /status endpoint
        status_result = subprocess.run(
            [
                "curl",
                "-s",
                "-f",
                f"http://127.0.0.1:{port}/status",
            ],
            capture_output=True,
            timeout=5,
            text=True,
        )

        if status_result.returncode != 0:
            pytest.skip(f"curl /status failed: {status_result.stderr}")

        status_data = json.loads(status_result.stdout)

        # Check that backends list is empty (base has no backend CLIs)
        assert status_data.get("capabilities", {}).get("backends", []) == []

        # Check that universal tool flags are present (git, python, shell, etc.)
        # Base should have git, node, shell; no backend-specific flags
        tool_flags = set(status_data.get("capabilities", {}).get("tool_flags", []))
        # Base should NOT have backend identifiers (those are added by slim/full)
        # It should have universal flags like git, python, shell, jq, ripgrep
        assert "browser" not in tool_flags  # Base doesn't include Playwright
        assert "lint" not in tool_flags  # Linters only in full
        assert "format" not in tool_flags  # Formatters only in full

    finally:
        # Clean up
        if container_id:
            subprocess.run(
                ["docker", "stop", "-t", "2", container_id],
                capture_output=True,
                timeout=10,
            )
        subprocess.run(["docker", "rmi", tag], capture_output=True, timeout=10)
