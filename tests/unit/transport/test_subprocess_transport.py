from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from coordinare.protocol import ProtocolMessage, ProtocolResponse
from coordinare.transport.base import TransportError, TransportTimeoutError
from coordinare.transport.subprocess_transport import (
    SubprocessTransport,
    _discover_backend_ui_url,
    _fetch_session_stats,
)

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
async def test_pushed_branch_survives_subprocess_round_trip() -> None:
    """511: the coordinare-side ProtocolResponse must carry pushed_branch
    (kept in sync with performer.protocol.PerformerResponse) so subprocess
    deployments record the working branch instead of pushed_branch=None."""
    transport = SubprocessTransport("/usr/bin/agent", timeout=30)
    wire = (
        b'{"status":"pr_opened","session_id":"s1",'
        b'"pr_url":"https://github.com/org/repo/pull/9",'
        b'"pr_node_id":"PR_9",'
        b'"head_after":"abc123",'
        b'"pushed_branch":"coordinare/PVTI_X/feat"}\n'
    )
    proc = _make_persistent_proc(wire)
    with patch("coordinare.transport.subprocess_transport.asyncio") as mock_asyncio:
        mock_asyncio.create_subprocess_exec = AsyncMock(return_value=proc)
        mock_asyncio.create_task = _mock_create_task
        mock_asyncio.subprocess = asyncio.subprocess
        mock_asyncio.wait_for = _await_coro
        resp = await transport.send(_make_msg("status"))
    assert resp.pushed_branch == "coordinare/PVTI_X/feat"
    assert resp.head_after == "abc123"


@pytest.mark.parametrize("status", ["nothing_to_review", "nothing_to_scan", "not_applicable", "blocked", "session_expired"])
@pytest.mark.asyncio
async def test_proc_cleared_after_advance_with_note_status(status) -> None:
    """412: the advance-with-note verdicts exit the performer's run loop and a
    review-cycle block caps the session -- the transport must not hand the
    next dispatch a process that is exiting or already at its cycle limit."""
    transport = SubprocessTransport("/usr/bin/agent", timeout=30)
    payload = f'{{"status":"{status}","session_id":"s1"}}\n'
    proc1 = _make_persistent_proc(payload.encode())
    proc2 = _make_persistent_proc(b'{"status":"accepted","session_id":"s2"}\n')

    with patch("coordinare.transport.subprocess_transport.asyncio") as mock_asyncio:
        mock_asyncio.create_subprocess_exec = AsyncMock(side_effect=[proc1, proc2])
        mock_asyncio.create_task = _mock_create_task
        mock_asyncio.subprocess = asyncio.subprocess
        mock_asyncio.wait_for = _await_coro

        r1 = await transport.send(_make_msg("status"))
        r2 = await transport.send(_make_msg("dispatch"))

    assert r1.status == status
    assert r2.status == "accepted"
    assert mock_asyncio.create_subprocess_exec.await_count == 2, "a fresh process replaces the one that exited"


@pytest.mark.asyncio
async def test_proc_kept_when_session_expired_while_another_session_active() -> None:
    """412 round 46: session_expired answered while another session is still
    being served (active_session=true) is an error for the stale request only
    -- the live process must not be cleared or reaped."""
    transport = SubprocessTransport("/usr/bin/agent", timeout=30)
    proc = _make_persistent_proc()
    proc.stdout.readline = AsyncMock(side_effect=[
        b'{"status":"session_expired","session_id":"stale","active_session":true}\n',
        b'{"status":"accepted","session_id":"s2"}\n',
    ])

    with patch("coordinare.transport.subprocess_transport.asyncio") as mock_asyncio:
        mock_asyncio.create_subprocess_exec = AsyncMock(return_value=proc)
        mock_asyncio.create_task = _mock_create_task
        mock_asyncio.subprocess = asyncio.subprocess
        mock_asyncio.wait_for = _await_coro

        r1 = await transport.send(_make_msg("status"))
        r2 = await transport.send(_make_msg("dispatch"))

    assert r1.status == "session_expired"
    assert r1.active_session is True
    assert r2.status == "accepted"
    assert mock_asyncio.create_subprocess_exec.await_count == 1, "the live process is kept for the active session"


@pytest.mark.asyncio
async def test_discarded_process_exits_cleanly_within_the_grace_window() -> None:
    """412 round 12: the reap lets the performer's cleanup path (backend stop,
    proxy shutdown, workspace cleanup) run to completion -- no SIGTERM when the
    process exits on its own."""
    transport = SubprocessTransport("/usr/bin/agent", timeout=30)
    proc = MagicMock(returncode=None)
    proc.wait = AsyncMock(return_value=0)

    transport._schedule_reap(proc)
    await asyncio.sleep(0.01)

    proc.terminate.assert_not_called()


@pytest.mark.asyncio
async def test_discarded_process_is_terminated_after_the_grace_window(monkeypatch) -> None:
    """412 round 12: a process that never exits on its own -- session_expired
    answered by a handler while a performance is still active -- is
    terminated once the bounded grace window passes."""
    from coordinare.transport import subprocess_transport

    monkeypatch.setattr(subprocess_transport, "_REAP_GRACE_SECONDS", 0.01)
    transport = SubprocessTransport("/usr/bin/agent", timeout=30)
    proc = MagicMock(returncode=None)
    stopped = asyncio.Event()

    async def _wait() -> None:
        await stopped.wait()

    proc.wait = _wait
    proc.terminate.side_effect = lambda: stopped.set()

    transport._schedule_reap(proc)
    await asyncio.sleep(0.05)

    proc.terminate.assert_called_once()


@pytest.mark.asyncio
async def test_next_start_serializes_behind_the_grace_window_reap(monkeypatch) -> None:
    """412 round 13: the next performer must not start beside the discarded
    predecessor's cleanup -- the start waits for the grace-window reap."""
    from coordinare.transport import subprocess_transport

    monkeypatch.setattr(subprocess_transport, "_REAP_GRACE_SECONDS", 0.01)
    transport = SubprocessTransport("/usr/bin/agent", timeout=30)
    proc = MagicMock(returncode=None)
    reaped = asyncio.Event()

    async def _wait() -> None:
        await reaped.wait()

    proc.wait = _wait
    proc.terminate.side_effect = lambda: reaped.set()
    transport._schedule_reap(proc)

    start_order: list[str] = []

    async def _fake_start(drain_stderr: bool):
        start_order.append("reaped" if reaped.is_set() else "overlapped")
        return _make_persistent_proc()

    async def _fake_exchange(p, message, timeout):
        return ProtocolResponse(status="accepted", session_id="s1")

    monkeypatch.setattr(transport, "_start", _fake_start)
    monkeypatch.setattr(transport, "_exchange", _fake_exchange)

    await transport.send(_make_msg("dispatch"))
    assert start_order == ["reaped"], "the start must wait for the reap, not overlap it"


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


def test_parse_response_aggregates_non_json_warnings() -> None:
    """045 + Copilot round 2: emit ONE transport.skipping_non_json_line
    warning per ``_parse_response`` call with a skipped_count and the
    first preview, rather than one warning per noise line.  Prevents the
    log storm that used to fire on multi-line backend stack traces.
    """
    from structlog.testing import capture_logs

    data = (
        b"ruby 3.2.0\n"
        b"\x1b[2mlog line one\x1b[0m\n"
        b"Fetching gem metadata...\n"
        b'{"status":"accepted","session_id":"s1"}\n'
    )
    with capture_logs() as cap:
        resp = SubprocessTransport._parse_response(data, one_shot=False, returncode=None)
    assert resp.status == "accepted"

    skips = [e for e in cap if e.get("event") == "transport.skipping_non_json_line"]
    assert len(skips) == 1, f"expected one aggregated warning, got {skips}"
    assert skips[0]["skipped_count"] == 3


def test_parse_response_redacts_tokens_in_preview() -> None:
    """045 + Copilot round 1: secret-looking substrings in the first
    skipped line (preview) are redacted before the warning goes out so
    tokens that leak onto performer stdout can't land in log files."""
    from structlog.testing import capture_logs

    fake_pat = "ghp_" + "A" * 36
    data = (
        f"performer startup: using token {fake_pat}\n".encode()
        + b'{"status":"accepted","session_id":"s1"}\n'
    )
    with capture_logs() as cap:
        SubprocessTransport._parse_response(data, one_shot=False, returncode=None)

    skips = [e for e in cap if e.get("event") == "transport.skipping_non_json_line"]
    assert len(skips) == 1
    preview = skips[0]["line_preview"]
    assert fake_pat not in preview
    assert "[REDACTED]" in preview


# ---------------------------------------------------------------------------
# T009 — _discover_backend_ui_url and _fetch_session_stats (052)
# ---------------------------------------------------------------------------


def test_discover_backend_ui_url_returns_url_from_matching_line() -> None:
    """T009a: _discover_backend_ui_url returns URL when opencode log line matches."""
    logs = [
        "starting server...",
        '{"level":"info","server":"http://127.0.0.1:34567","message":"ready"}',
    ]
    url = _discover_backend_ui_url(logs)
    assert url == "http://127.0.0.1:34567"


def test_discover_backend_ui_url_returns_none_when_no_match() -> None:
    """T009b: _discover_backend_ui_url returns None when no matching log line."""
    logs = ["startup complete", "loading config", "no port announcement here"]
    assert _discover_backend_ui_url(logs) is None


def test_discover_backend_ui_url_returns_none_for_empty_logs() -> None:
    assert _discover_backend_ui_url([]) is None


@pytest.mark.asyncio
async def test_fetch_session_stats_returns_stats_on_success() -> None:
    """T009c: _fetch_session_stats returns SessionStats on successful HTTP response."""
    mock_resp = MagicMock()
    mock_resp.is_success = True
    mock_resp.json.return_value = {
        "title": "Add auth flow",
        "filesChanged": 3,
        "linesAdded": 42,
        "linesRemoved": 7,
    }
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.get = AsyncMock(return_value=mock_resp)
    with patch("httpx.AsyncClient", return_value=mock_client):
        stats = await _fetch_session_stats("http://127.0.0.1:34567")
    assert stats is not None
    assert stats.title == "Add auth flow"
    assert stats.files_changed == 3
    assert stats.lines_added == 42
    assert stats.lines_removed == 7


@pytest.mark.asyncio
async def test_fetch_session_stats_returns_none_on_http_error() -> None:
    """T009d: _fetch_session_stats returns None on HTTP error without raising."""
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.get = AsyncMock(side_effect=Exception("connection refused"))
    with patch("httpx.AsyncClient", return_value=mock_client):
        stats = await _fetch_session_stats("http://127.0.0.1:34567")
    assert stats is None


@pytest.mark.asyncio
async def test_fetch_session_stats_returns_none_on_non_200() -> None:
    mock_resp = MagicMock()
    mock_resp.is_success = False
    mock_resp.status_code = 503
    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=None)
    mock_client.get = AsyncMock(return_value=mock_resp)
    with patch("httpx.AsyncClient", return_value=mock_client):
        stats = await _fetch_session_stats("http://127.0.0.1:34567")
    assert stats is None
