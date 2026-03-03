from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from coordinare.protocol import ProtocolMessage
from coordinare.transport.base import TransportError, TransportTimeoutError
from coordinare.transport.subprocess_transport import SubprocessTransport


def _make_msg(action: str = "dispatch") -> ProtocolMessage:
    return ProtocolMessage(action=action, session_id="s1", payload={"title": "Test"})


def _make_process(
    stdout: bytes = b'{"status":"accepted","session_id":"s1"}',
    stderr: bytes = b"",
    returncode: int = 0,
) -> MagicMock:
    proc = MagicMock()
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.returncode = returncode
    proc.kill = MagicMock()
    proc.wait = AsyncMock()
    return proc


@asynccontextmanager
async def _patch_asyncio(
    proc: MagicMock,
    *,
    raise_timeout: bool = False,
) -> AsyncGenerator[MagicMock, None]:
    """Patch the asyncio module inside SubprocessTransport.

    Uses a real async wait_for stub that actually awaits (or closes) the
    coroutine argument so Python's GC never sees un-awaited coroutines.
    """

    async def _fake_wait_for(coro, timeout=None):  # type: ignore[no-untyped-def]
        if raise_timeout:
            coro.close()  # prevent "coroutine was never awaited" warning
            raise TimeoutError
        return await coro

    with patch("coordinare.transport.subprocess_transport.asyncio") as mock_asyncio:
        mock_asyncio.create_subprocess_exec = AsyncMock(return_value=proc)
        mock_asyncio.wait_for = _fake_wait_for
        mock_asyncio.subprocess = asyncio.subprocess
        yield mock_asyncio


@pytest.mark.asyncio
async def test_happy_path_returns_protocol_response() -> None:
    transport = SubprocessTransport("/usr/bin/agent", timeout=30)
    proc = _make_process()

    async with _patch_asyncio(proc):
        response = await transport.send(_make_msg())

    assert response.status == "accepted"
    assert response.session_id == "s1"


@pytest.mark.asyncio
async def test_timeout_kills_process_and_raises() -> None:
    transport = SubprocessTransport("/usr/bin/agent", timeout=5)
    proc = _make_process()

    async with _patch_asyncio(proc, raise_timeout=True):
        with pytest.raises(TransportTimeoutError) as exc_info:
            await transport.send(_make_msg())

    assert exc_info.value.timeout == 5
    proc.kill.assert_called_once()
    proc.wait.assert_awaited_once()


@pytest.mark.asyncio
async def test_timeout_override_used() -> None:
    transport = SubprocessTransport("/usr/bin/agent", timeout=30)
    proc = _make_process()

    async with _patch_asyncio(proc, raise_timeout=True):
        with pytest.raises(TransportTimeoutError) as exc_info:
            await transport.send(_make_msg(), timeout_override=10)

    assert exc_info.value.timeout == 10


@pytest.mark.asyncio
async def test_non_zero_exit_raises_transport_error() -> None:
    transport = SubprocessTransport("/usr/bin/agent", timeout=30)
    proc = _make_process(returncode=1)

    async with _patch_asyncio(proc):
        with pytest.raises(TransportError, match="exited with code 1"):
            await transport.send(_make_msg())


@pytest.mark.asyncio
async def test_empty_stdout_raises_transport_error() -> None:
    transport = SubprocessTransport("/usr/bin/agent", timeout=30)
    proc = _make_process(stdout=b"")

    async with _patch_asyncio(proc):
        with pytest.raises(TransportError, match="no output"):
            await transport.send(_make_msg())


@pytest.mark.asyncio
async def test_malformed_json_raises_transport_error() -> None:
    transport = SubprocessTransport("/usr/bin/agent", timeout=30)
    proc = _make_process(stdout=b"not valid json")

    async with _patch_asyncio(proc):
        with pytest.raises(TransportError, match="Invalid agent response"):
            await transport.send(_make_msg())


@pytest.mark.asyncio
async def test_invalid_status_in_response_raises_transport_error() -> None:
    transport = SubprocessTransport("/usr/bin/agent", timeout=30)
    bad_json = json.dumps({"status": "not_a_real_status"}).encode()
    proc = _make_process(stdout=bad_json)

    async with _patch_asyncio(proc):
        with pytest.raises(TransportError, match="Invalid agent response"):
            await transport.send(_make_msg())


@pytest.mark.asyncio
async def test_os_error_on_exec_raises_transport_error() -> None:
    transport = SubprocessTransport("/nonexistent/agent", timeout=30)

    with patch("coordinare.transport.subprocess_transport.asyncio") as mock_asyncio:
        mock_asyncio.create_subprocess_exec = AsyncMock(
            side_effect=OSError("No such file")
        )
        mock_asyncio.subprocess = asyncio.subprocess

        with pytest.raises(TransportError, match="Failed to start"):
            await transport.send(_make_msg())


@pytest.mark.asyncio
async def test_stderr_logged_at_debug(caplog: pytest.LogCaptureFixture) -> None:
    transport = SubprocessTransport("/usr/bin/agent", timeout=30)
    proc = _make_process(stderr=b"some debug info")

    async with _patch_asyncio(proc):
        response = await transport.send(_make_msg())

    assert response.status == "accepted"
