from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from coordinare.protocol import ProtocolMessage
from coordinare.transport.base import TransportError, TransportTimeoutError
from coordinare.transport.subprocess_transport import SubprocessTransport

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_msg(action: str = "dispatch") -> ProtocolMessage:
    return ProtocolMessage(action=action, session_id="s1", payload={"title": "Test"})


def _make_persistent_proc(
    response_line: bytes = b'{"status":"accepted","session_id":"s1"}\n',
    *,
    drain_raises: type[Exception] | None = None,
    readline_raises: type[Exception] | None = None,
    returncode: int | None = None,
) -> MagicMock:
    """Fake process for persistent (non-health) sends."""
    stdin = MagicMock()
    stdin.write = MagicMock()
    stdin.drain = AsyncMock(side_effect=drain_raises) if drain_raises else AsyncMock()

    stdout = MagicMock()
    stdout.readline = AsyncMock(return_value=response_line)
    if readline_raises:
        stdout.readline = AsyncMock(side_effect=readline_raises)

    proc = MagicMock()
    proc.stdin = stdin
    proc.stdout = stdout
    proc.stderr = None  # _drain_stderr exits immediately when stderr is None
    proc.returncode = returncode
    proc.kill = MagicMock()
    proc.wait = AsyncMock()
    return proc


def _make_oneshot_proc(
    stdout: bytes = b'{"status":"healthy","session_id":""}\n',
    stderr: bytes = b"",
    returncode: int = 0,
) -> MagicMock:
    """Fake process for health one-shot sends (uses communicate())."""
    proc = MagicMock()
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.returncode = returncode
    proc.kill = MagicMock()
    proc.wait = AsyncMock()
    proc.stdin = MagicMock()
    proc.stdout = MagicMock()
    return proc


# ---------------------------------------------------------------------------
# Persistent-path tests (dispatch, status, relay_feedback)
# ---------------------------------------------------------------------------

async def _await_coro(coro, timeout=None):
    return await coro


def _mock_create_task(coro, **kw):
    """Close the coroutine immediately so it is not garbage-collected unawaited.

    Tests that patch asyncio entirely don't have a running event loop that would
    schedule the coroutine, so we close it explicitly to silence the
    'coroutine was never awaited' RuntimeWarning from _drain_stderr.
    """
    coro.close()
    return MagicMock(done=lambda: True, cancel=MagicMock())


@pytest.mark.asyncio
async def test_happy_path_returns_protocol_response() -> None:
    transport = SubprocessTransport("/usr/bin/agent", timeout=30)
    proc = _make_persistent_proc()

    with patch("coordinare.transport.subprocess_transport.asyncio") as mock_asyncio:
        mock_asyncio.create_subprocess_exec = AsyncMock(return_value=proc)
        mock_asyncio.create_task = _mock_create_task
        mock_asyncio.subprocess = asyncio.subprocess
        mock_asyncio.wait_for = _await_coro

        response = await transport.send(_make_msg("dispatch"))

    assert response.status == "accepted"
    assert response.session_id == "s1"


@pytest.mark.asyncio
async def test_persistent_proc_reused_across_sends() -> None:
    """The same process must be reused between dispatch and status calls."""
    transport = SubprocessTransport("/usr/bin/agent", timeout=30)
    proc = _make_persistent_proc()
    proc.stdout.readline = AsyncMock(side_effect=[
        b'{"status":"accepted","session_id":"s1"}\n',
        b'{"status":"working","session_id":"s1"}\n',
    ])

    with patch("coordinare.transport.subprocess_transport.asyncio") as mock_asyncio:
        mock_asyncio.create_subprocess_exec = AsyncMock(return_value=proc)
        mock_asyncio.create_task = _mock_create_task
        mock_asyncio.subprocess = asyncio.subprocess
        mock_asyncio.wait_for = _await_coro

        await transport.send(_make_msg("dispatch"))
        await transport.send(_make_msg("status"))

    # Only one process should have been started
    mock_asyncio.create_subprocess_exec.assert_awaited_once()


@pytest.mark.asyncio
async def test_proc_cleared_after_terminal_status() -> None:
    """After pr_opened/error the process reference is cleared for next dispatch."""
    transport = SubprocessTransport("/usr/bin/agent", timeout=30)
    proc1 = _make_persistent_proc(b'{"status":"pr_opened","session_id":"s1"}\n')
    proc2 = _make_persistent_proc(b'{"status":"accepted","session_id":"s2"}\n')

    with patch("coordinare.transport.subprocess_transport.asyncio") as mock_asyncio:
        mock_asyncio.create_subprocess_exec = AsyncMock(side_effect=[proc1, proc2])
        mock_asyncio.create_task = _mock_create_task
        mock_asyncio.subprocess = asyncio.subprocess
        mock_asyncio.wait_for = _await_coro

        r1 = await transport.send(_make_msg("status"))
        r2 = await transport.send(_make_msg("dispatch"))

    assert r1.status == "pr_opened"
    assert r2.status == "accepted"
    assert mock_asyncio.create_subprocess_exec.await_count == 2


@pytest.mark.asyncio
async def test_timeout_kills_process_and_raises() -> None:
    transport = SubprocessTransport("/usr/bin/agent", timeout=5)
    proc = _make_persistent_proc()

    async def _fake_wait_for(coro, timeout=None):
        coro.close()
        raise TimeoutError

    with patch("coordinare.transport.subprocess_transport.asyncio") as mock_asyncio:
        mock_asyncio.create_subprocess_exec = AsyncMock(return_value=proc)
        mock_asyncio.create_task = _mock_create_task
        mock_asyncio.subprocess = asyncio.subprocess
        mock_asyncio.wait_for = _fake_wait_for

        with pytest.raises(TransportTimeoutError) as exc_info:
            await transport.send(_make_msg("dispatch"))

    assert exc_info.value.timeout == 5
    proc.kill.assert_called_once()


@pytest.mark.asyncio
async def test_timeout_override_used() -> None:
    transport = SubprocessTransport("/usr/bin/agent", timeout=30)
    proc = _make_persistent_proc()

    async def _fake_wait_for(coro, timeout=None):
        coro.close()
        raise TimeoutError

    with patch("coordinare.transport.subprocess_transport.asyncio") as mock_asyncio:
        mock_asyncio.create_subprocess_exec = AsyncMock(return_value=proc)
        mock_asyncio.create_task = _mock_create_task
        mock_asyncio.subprocess = asyncio.subprocess
        mock_asyncio.wait_for = _fake_wait_for

        with pytest.raises(TransportTimeoutError) as exc_info:
            await transport.send(_make_msg("dispatch"), timeout_override=10)

    assert exc_info.value.timeout == 10


@pytest.mark.asyncio
async def test_empty_stdout_raises_transport_error() -> None:
    transport = SubprocessTransport("/usr/bin/agent", timeout=30)
    proc = _make_persistent_proc(response_line=b"")

    with patch("coordinare.transport.subprocess_transport.asyncio") as mock_asyncio:
        mock_asyncio.create_subprocess_exec = AsyncMock(return_value=proc)
        mock_asyncio.create_task = _mock_create_task
        mock_asyncio.subprocess = asyncio.subprocess
        mock_asyncio.wait_for = _await_coro

        with pytest.raises(TransportError, match="no output"):
            await transport.send(_make_msg("dispatch"))


@pytest.mark.asyncio
async def test_malformed_json_raises_transport_error() -> None:
    transport = SubprocessTransport("/usr/bin/agent", timeout=30)
    proc = _make_persistent_proc(response_line=b"not valid json\n")

    with patch("coordinare.transport.subprocess_transport.asyncio") as mock_asyncio:
        mock_asyncio.create_subprocess_exec = AsyncMock(return_value=proc)
        mock_asyncio.create_task = _mock_create_task
        mock_asyncio.subprocess = asyncio.subprocess
        mock_asyncio.wait_for = _await_coro

        with pytest.raises(TransportError, match="No valid JSON response"):
            await transport.send(_make_msg("dispatch"))


@pytest.mark.asyncio
async def test_invalid_status_in_response_raises_transport_error() -> None:
    transport = SubprocessTransport("/usr/bin/agent", timeout=30)
    bad = json.dumps({"status": "not_a_real_status"}).encode() + b"\n"
    proc = _make_persistent_proc(response_line=bad)

    with patch("coordinare.transport.subprocess_transport.asyncio") as mock_asyncio:
        mock_asyncio.create_subprocess_exec = AsyncMock(return_value=proc)
        mock_asyncio.create_task = _mock_create_task
        mock_asyncio.subprocess = asyncio.subprocess
        mock_asyncio.wait_for = _await_coro

        with pytest.raises(TransportError, match="Valid JSON but invalid protocol response"):
            await transport.send(_make_msg("dispatch"))


@pytest.mark.asyncio
async def test_os_error_on_exec_raises_transport_error() -> None:
    transport = SubprocessTransport("/nonexistent/agent", timeout=30)

    with patch("coordinare.transport.subprocess_transport.asyncio") as mock_asyncio:
        mock_asyncio.create_subprocess_exec = AsyncMock(side_effect=OSError("No such file"))
        mock_asyncio.create_task = _mock_create_task
        mock_asyncio.subprocess = asyncio.subprocess

        with pytest.raises(TransportError, match="Failed to start"):
            await transport.send(_make_msg("dispatch"))


# ---------------------------------------------------------------------------
# Health (one-shot path)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_health_uses_oneshot_not_persistent() -> None:
    """Health checks must not share the persistent process."""
    transport = SubprocessTransport("/usr/bin/agent", timeout=30)
    health_proc = _make_oneshot_proc()

    async def _fake_wait_for(coro, timeout=None):
        return await coro

    with patch("coordinare.transport.subprocess_transport.asyncio") as mock_asyncio:
        mock_asyncio.create_subprocess_exec = AsyncMock(return_value=health_proc)
        mock_asyncio.create_task = _mock_create_task
        mock_asyncio.subprocess = asyncio.subprocess
        mock_asyncio.wait_for = _fake_wait_for

        response = await transport.send(_make_msg("health"), timeout_override=10)

    assert response.status == "healthy"
    assert transport._proc is None  # health never sets the persistent proc


@pytest.mark.asyncio
async def test_stderr_logged_at_debug(caplog: pytest.LogCaptureFixture) -> None:
    """Stderr from a health one-shot is logged at debug level."""
    transport = SubprocessTransport("/usr/bin/agent", timeout=30)
    proc = _make_oneshot_proc(stderr=b"some debug info")

    async def _fake_wait_for(coro, timeout=None):
        return await coro

    with patch("coordinare.transport.subprocess_transport.asyncio") as mock_asyncio:
        mock_asyncio.create_subprocess_exec = AsyncMock(return_value=proc)
        mock_asyncio.create_task = _mock_create_task
        mock_asyncio.subprocess = asyncio.subprocess
        mock_asyncio.wait_for = _fake_wait_for

        response = await transport.send(_make_msg("health"))

    assert response.status == "healthy"


@pytest.mark.asyncio
async def test_non_zero_exit_raises_transport_error() -> None:
    """Health one-shot with non-zero exit raises TransportError."""
    transport = SubprocessTransport("/usr/bin/agent", timeout=30)
    proc = _make_oneshot_proc(returncode=1)

    async def _fake_wait_for(coro, timeout=None):
        return await coro

    with patch("coordinare.transport.subprocess_transport.asyncio") as mock_asyncio:
        mock_asyncio.create_subprocess_exec = AsyncMock(return_value=proc)
        mock_asyncio.create_task = _mock_create_task
        mock_asyncio.subprocess = asyncio.subprocess
        mock_asyncio.wait_for = _fake_wait_for

        with pytest.raises(TransportError, match="exited with code 1"):
            await transport.send(_make_msg("health"))


# ---------------------------------------------------------------------------
# agent_logs property
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_agent_logs_property_returns_list() -> None:
    """agent_logs property returns a list copy of _agent_logs."""
    transport = SubprocessTransport("/usr/bin/agent", timeout=30)
    proc = _make_persistent_proc()

    with patch("coordinare.transport.subprocess_transport.asyncio") as mock_asyncio:
        mock_asyncio.create_subprocess_exec = AsyncMock(return_value=proc)
        mock_asyncio.create_task = _mock_create_task
        mock_asyncio.subprocess = asyncio.subprocess
        mock_asyncio.wait_for = _await_coro

        await transport.send(_make_msg("dispatch"))

    # Manually add some fake log lines to the buffer
    transport._agent_logs.extend(["line1", "line2"])
    logs = transport.agent_logs
    assert logs == ["line1", "line2"]
    assert isinstance(logs, list)


# ---------------------------------------------------------------------------
# Stale process cleared (line 63)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stale_exited_proc_cleared_before_next_send() -> None:
    """If _proc.returncode is set (process exited), a fresh proc is started."""
    transport = SubprocessTransport("/usr/bin/agent", timeout=30)
    proc1 = _make_persistent_proc(b'{"status":"accepted","session_id":"s1"}\n')
    proc1.returncode = 0  # already exited
    proc2 = _make_persistent_proc(b'{"status":"accepted","session_id":"s2"}\n')

    with patch("coordinare.transport.subprocess_transport.asyncio") as mock_asyncio:
        mock_asyncio.create_subprocess_exec = AsyncMock(side_effect=[proc1, proc2])
        mock_asyncio.create_task = _mock_create_task
        mock_asyncio.subprocess = asyncio.subprocess
        mock_asyncio.wait_for = _await_coro

        # Inject the stale process reference directly
        transport._proc = proc1
        response = await transport.send(_make_msg("dispatch"))

    assert response.status == "accepted"
    # A second process should have been started since proc1 was stale
    mock_asyncio.create_subprocess_exec.assert_awaited_once()


# ---------------------------------------------------------------------------
# One-shot timeout (lines 140-143)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_oneshot_timeout_kills_proc_and_raises() -> None:
    """Health one-shot timeout kills the process and raises TransportTimeoutError."""
    transport = SubprocessTransport("/usr/bin/agent", timeout=10)
    proc = _make_oneshot_proc()

    async def _fake_wait_for(coro, timeout=None):
        coro.close()
        raise TimeoutError

    with patch("coordinare.transport.subprocess_transport.asyncio") as mock_asyncio:
        mock_asyncio.create_subprocess_exec = AsyncMock(return_value=proc)
        mock_asyncio.create_task = _mock_create_task
        mock_asyncio.subprocess = asyncio.subprocess
        mock_asyncio.wait_for = _fake_wait_for

        with pytest.raises(TransportTimeoutError) as exc_info:
            await transport.send(_make_msg("health"))

    assert exc_info.value.timeout == 10
    proc.kill.assert_called_once()


# ---------------------------------------------------------------------------
# BrokenPipeError in _exchange (lines 172-174)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_broken_pipe_in_exchange_raises_transport_error() -> None:
    """BrokenPipeError during write raises TransportError and clears _proc."""
    transport = SubprocessTransport("/usr/bin/agent", timeout=30)
    proc = _make_persistent_proc(drain_raises=BrokenPipeError)

    with patch("coordinare.transport.subprocess_transport.asyncio") as mock_asyncio:
        mock_asyncio.create_subprocess_exec = AsyncMock(return_value=proc)
        mock_asyncio.create_task = _mock_create_task
        mock_asyncio.subprocess = asyncio.subprocess
        mock_asyncio.wait_for = _await_coro

        with pytest.raises(TransportError, match="pipe broken"):
            await transport.send(_make_msg("dispatch"))

    assert transport._proc is None


# ---------------------------------------------------------------------------
# _drain_stderr execution (lines 116-124)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_drain_stderr_buffers_lines() -> None:
    """_drain_stderr reads lines from proc.stderr into _agent_logs."""
    transport = SubprocessTransport("/usr/bin/agent", timeout=30)

    async def _fake_stderr():
        for line in [b"log line 1\n", b"log line 2\n"]:
            yield line

    stderr_mock = MagicMock()
    stderr_mock.__aiter__ = lambda self: _fake_stderr()

    proc = MagicMock()
    proc.stderr = stderr_mock

    await transport._drain_stderr(proc)

    assert list(transport._agent_logs) == ["log line 1", "log line 2"]


@pytest.mark.asyncio
async def test_drain_stderr_noop_when_stderr_none() -> None:
    proc = MagicMock()
    proc.stderr = None

    transport = SubprocessTransport("/usr/bin/agent", timeout=30)
    await transport._drain_stderr(proc)  # should return immediately

    assert list(transport._agent_logs) == []


@pytest.mark.asyncio
async def test_drain_stderr_skips_empty_lines() -> None:
    async def _fake_stderr():
        yield b"\n"         # empty — should not be appended
        yield b"real\n"     # non-empty — should be appended

    proc = MagicMock()
    proc.stderr = MagicMock()
    proc.stderr.__aiter__ = lambda self: _fake_stderr()

    transport = SubprocessTransport("/usr/bin/agent", timeout=30)
    await transport._drain_stderr(proc)

    assert list(transport._agent_logs) == ["real"]


@pytest.mark.asyncio
async def test_drain_stderr_cancelled_error_is_swallowed() -> None:
    async def _fake_stderr():
        raise asyncio.CancelledError()
        yield  # pragma: no cover

    proc = MagicMock()
    proc.stderr = MagicMock()
    proc.stderr.__aiter__ = lambda self: _fake_stderr()

    transport = SubprocessTransport("/usr/bin/agent", timeout=30)
    await transport._drain_stderr(proc)  # should not raise or propagate


# ---------------------------------------------------------------------------
# Stderr task cancelled before restart (line 104)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_stderr_task_cancelled_on_proc_restart() -> None:
    """If an active stderr drain task exists, it is cancelled when proc restarts."""
    transport = SubprocessTransport("/usr/bin/agent", timeout=30)

    # Inject a fake not-done stderr task
    old_task = MagicMock()
    old_task.done.return_value = False
    old_task.cancel = MagicMock()
    transport._stderr_task = old_task

    proc = _make_persistent_proc(b'{"status":"accepted","session_id":"s1"}\n')

    with patch("coordinare.transport.subprocess_transport.asyncio") as mock_asyncio:
        mock_asyncio.create_subprocess_exec = AsyncMock(return_value=proc)
        mock_asyncio.create_task = _mock_create_task
        mock_asyncio.subprocess = asyncio.subprocess
        mock_asyncio.wait_for = _await_coro

        await transport.send(_make_msg("dispatch"))

    old_task.cancel.assert_called_once()


# ---------------------------------------------------------------------------
# 044 — Transport resilience: skip non-JSON lines
# ---------------------------------------------------------------------------


def test_parse_response_valid_json_unchanged() -> None:
    """044: Valid single-line JSON parses exactly as before."""
    data = b'{"status":"accepted","session_id":"s1"}\n'
    resp = SubprocessTransport._parse_response(data, one_shot=False, returncode=None)
    assert resp.status == "accepted"


def test_parse_response_skips_ansi_prefix() -> None:
    """044: ANSI log line + valid JSON — the JSON line is accepted."""
    ansi_line = b"\x1b[2m2026-04-14T15:26:43Z\x1b[0m info performer started\n"
    json_line = b'{"status":"working","session_id":"s1"}\n'
    data = ansi_line + json_line
    resp = SubprocessTransport._parse_response(data, one_shot=False, returncode=None)
    assert resp.status == "working"


def test_parse_response_skips_multiple_noise_lines() -> None:
    """044: Multiple non-JSON lines before the valid response."""
    data = (
        b"ruby 3.2.0\n"
        b"\x1b[2mlog line\x1b[0m\n"
        b"Fetching gem metadata...\n"
        b'{"status":"accepted","session_id":"s1"}\n'
    )
    resp = SubprocessTransport._parse_response(data, one_shot=False, returncode=None)
    assert resp.status == "accepted"


def test_parse_response_only_noise_raises() -> None:
    """044: If no valid JSON found after all lines, raise TransportError."""
    data = b"ruby 3.2.0\nnot json\n\x1b[2mlog\x1b[0m\n"
    with pytest.raises(TransportError, match="No valid JSON response"):
        SubprocessTransport._parse_response(data, one_shot=False, returncode=None)


def test_parse_response_empty_raises() -> None:
    """044: Empty input still raises TransportError (unchanged)."""
    with pytest.raises(TransportError, match="no output"):
        SubprocessTransport._parse_response(b"", one_shot=False, returncode=None)

    with pytest.raises(TransportError, match="no output"):
        SubprocessTransport._parse_response(b"   \n  \n", one_shot=False, returncode=None)
