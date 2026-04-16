from __future__ import annotations

import asyncio
import time
from collections import deque

import structlog

from coordinare.lib.redaction import redact_secrets
from coordinare.protocol import ProtocolMessage, ProtocolResponse
from coordinare.transport.base import TransportError, TransportTimeoutError

logger = structlog.get_logger(__name__)

# Statuses after which the performer process has exited and must not be reused.
_TERMINAL_STATUSES = frozenset({"pr_opened", "error", "blocked"})

# Max stderr lines buffered per session — older lines are dropped automatically.
_STDERR_MAXLEN = 200


class SubprocessTransport:
    """Persistent-subprocess transport.

    Keeps the performer process alive across multiple send() calls so that
    session state (dispatch → status polls → pr_opened) is preserved within a
    single process.  The process is started lazily on the first non-health
    send and replaced automatically after a terminal response or crash.

    Health checks always use a fresh one-shot process so they never interfere
    with an active dispatch session.

    A background stderr-drain task reads the performer's stderr continuously.
    This prevents the 64 KB pipe-buffer from filling and blocking the process,
    and buffers the last _STDERR_MAXLEN lines for dashboard visibility.
    """

    def __init__(self, executable: str, timeout: int) -> None:
        self._executable = executable
        self._timeout = timeout
        self._proc: asyncio.subprocess.Process | None = None
        self._agent_logs: deque[str] = deque(maxlen=_STDERR_MAXLEN)
        self._stderr_task: asyncio.Task[None] | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def send(
        self,
        message: ProtocolMessage,
        *,
        timeout_override: int | None = None,
    ) -> ProtocolResponse:
        effective_timeout = timeout_override if timeout_override is not None else self._timeout

        if message.action == "health":
            # Health is stateless — always one-shot so it doesn't interfere
            # with an active dispatch session.
            return await self._one_shot(message, effective_timeout)

        # Clear stale process reference.
        if self._proc is not None and self._proc.returncode is not None:
            self._proc = None

        if self._proc is None:
            self._proc = await self._start(drain_stderr=True)

        response = await self._exchange(self._proc, message, effective_timeout)

        if response.status in _TERMINAL_STATUSES:
            # Process will exit on its own after emitting a terminal status;
            # clear our reference so the next dispatch gets a fresh one.
            self._proc = None

        return response

    @property
    def agent_logs(self) -> list[str]:
        """Return the buffered stderr lines from the active performer process."""
        return list(self._agent_logs)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _start(self, *, drain_stderr: bool = False) -> asyncio.subprocess.Process:
        try:
            proc = await asyncio.create_subprocess_exec(
                self._executable,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            raise TransportError(f"Failed to start agent process: {exc}") from exc
        logger.debug("subprocess_transport.started", pid=proc.pid, executable=self._executable)
        if drain_stderr:
            # Only for the persistent process — one-shot health checks use
            # proc.communicate() which reads stderr itself; starting a drain
            # task there would cause "read() called while another coroutine
            # is already waiting" on the same pipe.
            self._agent_logs.clear()
            if self._stderr_task and not self._stderr_task.done():
                self._stderr_task.cancel()
            self._stderr_task = asyncio.create_task(
                self._drain_stderr(proc), name="performer-stderr-drain"
            )
        return proc

    async def _drain_stderr(self, proc: asyncio.subprocess.Process) -> None:
        """Read performer stderr continuously, buffering lines for the dashboard.

        Running as a background task prevents the 64 KB pipe-buffer from filling
        up and blocking the performer process on its own log writes.
        """
        if proc.stderr is None:
            return
        try:
            async for raw in proc.stderr:
                line = raw.decode(errors="replace").rstrip()
                if line:
                    self._agent_logs.append(line)
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # pragma: no cover
            logger.debug("stderr_drain_error", error=str(exc))

    async def _one_shot(
        self, message: ProtocolMessage, timeout: int
    ) -> ProtocolResponse:
        """Start a fresh process, send one message, wait for it to exit."""
        start = time.monotonic()
        proc = await self._start()
        msg_bytes = message.model_dump_json().encode() + b"\n"
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=msg_bytes),
                timeout=timeout,
            )
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise TransportTimeoutError(timeout=timeout) from None

        self._log_stderr(stderr)
        logger.debug(
            "transport_send_complete",
            action=message.action,
            duration_ms=round((time.monotonic() - start) * 1000, 1),
        )
        return self._parse_response(stdout, one_shot=True, returncode=proc.returncode)

    async def _exchange(
        self,
        proc: asyncio.subprocess.Process,
        message: ProtocolMessage,
        timeout: int,
    ) -> ProtocolResponse:
        """Write one message and read one response line from a persistent process."""
        assert proc.stdin is not None and proc.stdout is not None
        start = time.monotonic()
        msg_bytes = message.model_dump_json().encode() + b"\n"
        try:
            proc.stdin.write(msg_bytes)
            await asyncio.wait_for(proc.stdin.drain(), timeout=timeout)
            line = await asyncio.wait_for(proc.stdout.readline(), timeout=timeout)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            self._proc = None
            raise TransportTimeoutError(timeout=timeout) from None
        except (BrokenPipeError, ConnectionResetError) as exc:
            self._proc = None
            raise TransportError(f"Performer pipe broken: {exc}") from exc

        logger.debug(
            "transport_send_complete",
            action=message.action,
            duration_ms=round((time.monotonic() - start) * 1000, 1),
        )
        logger.debug(
            "transport_response_raw",
            action=message.action,
            preview=line[:200].decode(errors="replace").strip() if line else "",
        )
        return self._parse_response(line, one_shot=False, returncode=None)

    @staticmethod
    def _log_stderr(stderr: bytes) -> None:
        if stderr:
            logger.debug("agent_stderr", stderr=stderr.decode(errors="replace").strip())

    @staticmethod
    def _parse_response(
        data: bytes, *, one_shot: bool, returncode: int | None
    ) -> ProtocolResponse:
        if one_shot and returncode is not None and returncode != 0:
            raise TransportError(f"Agent process exited with code {returncode}")
        if not data or not data.strip():
            raise TransportError("Agent process produced no output")

        # 044: Resilient line-by-line parsing.  Performer subprocesses can
        # emit non-JSON lines (ANSI log output, bundler diagnostics, bare
        # strings) that share stdout with the protocol channel.  Skip non-
        # JSON lines and accept the first valid JSON response.  Valid JSON
        # with wrong schema (Pydantic validation error) raises immediately.
        # If no valid JSON found after all lines, raise TransportError.
        import json as _json

        lines = data.strip().split(b"\n")
        skipped_count = 0
        first_skipped_preview: str | None = None

        def _log_skipped() -> None:
            """Emit a single aggregated warning for all non-JSON lines seen.

            045 + Copilot round 2: logging every skipped line at warning
            inside the loop created a log storm on backend crashes that
            dumped multi-line stack traces to stdout.  Collect the count
            and the first redacted preview and emit once per response.
            """
            if skipped_count > 0 and first_skipped_preview is not None:
                logger.warning(
                    "transport.skipping_non_json_line",
                    skipped_count=skipped_count,
                    line_preview=first_skipped_preview,
                )

        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            # First check: is this even valid JSON?  If not, skip it
            # (diagnostic noise from subprocesses).  If it IS valid JSON
            # but fails Pydantic validation (wrong schema), that's a real
            # protocol error — raise immediately rather than skipping.
            try:
                _json.loads(stripped)
            except (ValueError, TypeError, UnicodeDecodeError):
                # 045: ``UnicodeDecodeError`` covers binary/garbled bytes
                # that ``json.loads`` would otherwise raise on implicit
                # UTF-8 decoding — treat them as non-JSON noise.  The
                # preview goes through ``redact_secrets`` so tokens that
                # leak onto the performer's stdout don't land in log files.
                skipped_count += 1
                if first_skipped_preview is None:
                    first_skipped_preview = redact_secrets(
                        stripped[:200].decode("utf-8", errors="replace"),
                    )
                continue
            # Valid JSON — try Pydantic parse.  If this fails, it's a
            # schema error (real problem), not noise.
            _log_skipped()
            try:
                return ProtocolResponse.model_validate_json(stripped)
            except Exception as exc:
                raise TransportError(f"Valid JSON but invalid protocol response: {exc}") from exc
        _log_skipped()
        raise TransportError(
            f"No valid JSON response in output ({len(lines)} lines checked)",
        )
