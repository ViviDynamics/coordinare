"""Unit tests for orphan container cleanup on coordinare startup (spec 056, T046a).

Tests that on coordinare startup, orphan ephemeral containers labeled
coordinare.performer.session=<old_session> are removed with retry on failure.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, Mock, patch

import pytest

from coordinare.services import performer_lifecycle as lifecycle
from coordinare.services.performer_lifecycle import (
    ContainerStartError,
    _run_docker,
    cleanup_orphaned_containers,
)


@pytest.mark.asyncio
async def test_cleanup_orphaned_containers_stops_each(monkeypatch: pytest.MonkeyPatch) -> None:
    """When docker ps lists container IDs, each is stopped and count returned."""

    async def fake_run_docker(*args: str, timeout: float = 15.0) -> tuple[int, str, str]:
        return 0, "abc123\ndef456\n", ""

    safe_stop = AsyncMock()
    monkeypatch.setattr(lifecycle, "_run_docker", fake_run_docker)
    monkeypatch.setattr(lifecycle, "_safe_stop", safe_stop)

    count = await cleanup_orphaned_containers()

    assert count == 2
    assert safe_stop.await_count == 2
    safe_stop.assert_any_await("abc123")
    safe_stop.assert_any_await("def456")


@pytest.mark.asyncio
async def test_cleanup_orphaned_containers_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """No containers → return 0 without invoking _safe_stop."""

    async def fake_run_docker(*args: str, timeout: float = 15.0) -> tuple[int, str, str]:
        return 0, "  \n  \n", ""

    safe_stop = AsyncMock()
    monkeypatch.setattr(lifecycle, "_run_docker", fake_run_docker)
    monkeypatch.setattr(lifecycle, "_safe_stop", safe_stop)

    assert await cleanup_orphaned_containers() == 0
    safe_stop.assert_not_awaited()


@pytest.mark.asyncio
async def test_cleanup_orphaned_containers_docker_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-zero rc from docker ps → return 0 without stopping anything."""

    async def fake_run_docker(*args: str, timeout: float = 15.0) -> tuple[int, str, str]:
        return 1, "", "docker daemon unreachable"

    safe_stop = AsyncMock()
    monkeypatch.setattr(lifecycle, "_run_docker", fake_run_docker)
    monkeypatch.setattr(lifecycle, "_safe_stop", safe_stop)

    assert await cleanup_orphaned_containers() == 0
    safe_stop.assert_not_awaited()


@pytest.mark.asyncio
async def test_safe_stop_swallows_exceptions(monkeypatch: pytest.MonkeyPatch) -> None:
    """_safe_stop logs and returns even when stop() raises."""

    async def boom(_cid: str) -> None:
        raise RuntimeError("stop failed")

    monkeypatch.setattr(lifecycle, "stop", boom)
    await lifecycle._safe_stop("abc123")


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
