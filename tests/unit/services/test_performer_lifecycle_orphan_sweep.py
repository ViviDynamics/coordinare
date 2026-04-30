"""Unit tests for orphan container cleanup on coordinare startup (spec 056, T046a).

Tests that on coordinare startup, orphan ephemeral containers labeled
coordinare.performer.session=<old_session> are removed with retry on failure.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock, patch

import pytest

from coordinare.services.performer_lifecycle import (
    ContainerStartError,
    _run_docker,
)


@pytest.mark.asyncio
@pytest.mark.skip(reason="T046a orphan sweep not yet implemented")
async def test_docker_list_orphan_containers() -> None:
    """Test that we can list containers with a given label."""
    pass


@pytest.mark.asyncio
async def test_run_docker_timeout() -> None:
    """Test that _run_docker raises ContainerStartError on timeout."""
    # Mock subprocess to timeout
    with patch("coordinare.services.performer_lifecycle.asyncio.create_subprocess_exec") as mock_create:
        mock_proc = AsyncMock()
        mock_proc.communicate.side_effect = TimeoutError("timeout")
        mock_proc.kill = Mock()
        mock_proc.wait = AsyncMock()
        mock_create.return_value = mock_proc

        with pytest.raises(ContainerStartError):
            await _run_docker("ps", timeout=0.1)


@pytest.mark.asyncio
async def test_run_docker_success() -> None:
    """Test that _run_docker returns (rc, stdout, stderr) on success."""
    with patch("coordinare.services.performer_lifecycle.asyncio.create_subprocess_exec") as mock_create:
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"output\n", b"")
        mock_proc.returncode = 0
        mock_create.return_value = mock_proc

        rc, stdout, stderr = await _run_docker("ps")
        assert rc == 0
        assert stdout == "output"
        assert stderr == ""


@pytest.mark.asyncio
async def test_run_docker_error_return_code() -> None:
    """Test that _run_docker returns error code on command failure."""
    with patch("coordinare.services.performer_lifecycle.asyncio.create_subprocess_exec") as mock_create:
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"", b"error message\n")
        mock_proc.returncode = 1
        mock_create.return_value = mock_proc

        rc, _, stderr = await _run_docker("invalid-command")
        assert rc == 1
        assert stderr == "error message"


@pytest.mark.asyncio
async def test_run_docker_strips_whitespace() -> None:
    """Test that _run_docker strips trailing whitespace from output."""
    with patch("coordinare.services.performer_lifecycle.asyncio.create_subprocess_exec") as mock_create:
        mock_proc = AsyncMock()
        mock_proc.communicate.return_value = (b"  container-id  \n\n", b"  warning  \n")
        mock_proc.returncode = 0
        mock_create.return_value = mock_proc

        _, stdout, stderr = await _run_docker("ps")
        assert stdout == "container-id"
        assert stderr == "warning"


@pytest.mark.skip(reason="T046a orphan sweep not yet implemented")
def test_orphan_sweep_behavior_documented() -> None:
    """Placeholder for sweep_orphan_containers() once T046a is implemented."""
    pass
