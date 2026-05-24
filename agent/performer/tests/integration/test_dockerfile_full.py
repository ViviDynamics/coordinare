"""Integration test for Dockerfile.full (spec 056, T044).

Tests that the full image builds successfully and advertises every backend
plus the full tool flag set including browser.
"""

from __future__ import annotations

import subprocess
import time
import uuid

import pytest

from tests.conftest import wait_for_status


def _resolve_published_port(container_id: str, internal_port: int, timeout: float = 10.0) -> str:
    """Poll ``docker port`` until the ephemeral mapping is published.

    ``docker run -d -p 0:<port>`` returns the container ID before the port
    binding is actually wired through, so a naive ``docker port`` call races
    and returns empty.  Poll briefly until docker reports the mapping.
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
    pytest.fail(f"docker port never reported a mapping for {internal_port}/tcp: {last_stderr}")


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
        pytest.fail(f"docker build failed: {build_result.stderr.decode()}")

    # Test each backend individually (entrypoint.sh installs per BACKEND env var)
    # and verify that tool flags are present in all cases.
    backends_to_test = ["claude_code", "codex", "cursor", "junie", "opencode"]
    advertised_backends = set()
    advertised_tool_flags = set()

    for backend in backends_to_test:
        container_id: str | None = None

        try:
            backend_name = "claude" if backend == "claude_code" else backend
            run_result = subprocess.run(
                [
                    "docker", "run", "-d", "--rm", "-p", "0:8088",
                    "-e", f"BACKEND={backend_name}",
                    tag,
                ],
                capture_output=True,
                timeout=10,
                text=True,
            )

            if run_result.returncode != 0:
                pytest.fail(f"docker run with BACKEND={backend_name} failed: {run_result.stderr}")

            container_id = run_result.stdout.strip()

            port = _resolve_published_port(container_id, 8088)

            # Poll until ready — npm/curl installs can take >10s
            status_data = await wait_for_status(port, timeout=120.0)

            backends = status_data.get("capabilities", {}).get("backends", [])
            advertised_backends.update(backends)
            tool_flags = status_data.get("capabilities", {}).get("tool_flags", [])
            advertised_tool_flags.update(tool_flags)

        finally:
            if container_id:
                subprocess.run(
                    ["docker", "rm", "-f", container_id],
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
