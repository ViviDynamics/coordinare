from __future__ import annotations

import asyncio
import os
import re
import time
from collections import deque
from typing import Any

import httpx
import structlog

from coordinare.lib.redaction import redact_secrets
from coordinare.lib.subprocess_env import inherited_host_env
from coordinare.protocol import PROCESS_EXITING_STATUSES, ProtocolMessage, ProtocolResponse
from coordinare.session import SessionStats
from coordinare.transport.base import TransportError, TransportTimeoutError

logger = structlog.get_logger(__name__)

# Pattern opencode emits to stderr when its HTTP server starts:
# e.g. {"server":"http://127.0.0.1:34567"}
_OPENCODE_SERVER_RE = re.compile(r'"server"\s*:\s*"(https?://[^"]+)"')


def _discover_backend_ui_url(agent_logs: list[str]) -> str | None:
    """Scan buffered stderr lines for opencode's port announcement.

    Returns the full URL when found, or None. Trailing slash stripped intentionally.
    """
    for line in reversed(agent_logs):
        m = _OPENCODE_SERVER_RE.search(line)
        if m:
            url = m.group(1).rstrip("/")
            logger.debug("transport.backend_ui_discovered", url=url)
            return url
    return None


def _is_local_url(url: str) -> bool:
    """Return True only for http://127.0.0.1:PORT or http://localhost:PORT."""
    from urllib.parse import urlparse

    try:
        p = urlparse(url)
        if p.scheme != "http" or p.hostname not in ("127.0.0.1", "localhost"):
            return False
        port = p.port
        return port is not None and 1 <= port <= 65535
    except ValueError:
        return False


async def _fetch_session_stats(ui_url: str) -> SessionStats | None:
    """Poll the backend's /api/session endpoint and map to SessionStats.

    Returns None on any error without raising.
    """
    if not _is_local_url(ui_url):
        logger.warning("transport.session_stats_ssrf_blocked", url=ui_url)
        return None
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{ui_url}/api/session")
        if not resp.is_success:
            logger.debug("transport.session_stats_error", status=resp.status_code, url=ui_url)
            return None
        data = resp.json()
        return SessionStats(
            title=data.get("title") or None,
            files_changed=int(data.get("filesChanged") or data.get("files_changed") or 0),
            lines_added=int(data.get("linesAdded") or data.get("lines_added") or 0),
            lines_removed=int(data.get("linesRemoved") or data.get("lines_removed") or 0),
        )
    except Exception as exc:
        logger.debug("transport.session_stats_error", error=str(exc), url=ui_url)
        return None

# Max stderr lines buffered per session — older lines are dropped automatically.
_STDERR_MAXLEN = 200
#: 412 round 12: how long a discarded performer process may keep running its
#: cleanup path before the transport terminates it.
_REAP_GRACE_SECONDS = 10.0


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

    def __init__(
        self,
        executable: str,
        timeout: int,
        config: Any = None,
        github_token: str | None = None,
    ) -> None:
        self._executable = executable
        self._timeout = timeout
        self._config = config
        self._github_token = github_token
        self._proc: asyncio.subprocess.Process | None = None
        self._agent_logs: deque[str] = deque(maxlen=_STDERR_MAXLEN)
        self._stderr_task: asyncio.Task[None] | None = None
        self._reap_tasks: set[asyncio.Task[None]] = set()

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
            # 412 round 13: a discarded predecessor may still be inside its
            # grace-window cleanup (backend stop / proxy shutdown / workspace
            # teardown) in the same workspace -- the next performer must not
            # start beside it. Serializing the start behind the reap bounds
            # the wait to the grace window.
            pending_reaps = [t for t in self._reap_tasks if not t.done()]
            if pending_reaps:
                await asyncio.gather(*pending_reaps, return_exceptions=True)
            self._proc = await self._start(drain_stderr=True)

        response = await self._exchange(self._proc, message, effective_timeout)

        # 412 round 46: session_expired answers both a no-session exit (the
        # run loop breaks; the process is on its way out) and a stale or
        # mismatched session id while another session is still being served
        # (the loop keeps running). Only the former may clear the process --
        # reaping a live backend loses the active session. Every other
        # process-exiting status genuinely exits the loop.
        exiting = response.status in PROCESS_EXITING_STATUSES and (
            response.status != "session_expired" or not response.active_session
        )
        if exiting:
            # 412: the run loop exits for every terminal status, not just the
            # failures -- clear our reference so the next dispatch gets a
            # fresh process instead of reusing one that is on its way out.
            # 412 round 12: stop what we discard, but let the performer's
            # cleanup path run first: the run loop writes the response before
            # it breaks into backend stop / proxy shutdown / workspace
            # cleanup, and an immediate SIGTERM would leave that behind.
            proc, self._proc = self._proc, None
            if proc is not None and proc.returncode is None:
                self._schedule_reap(proc)

        return response

    def _schedule_reap(self, proc: asyncio.subprocess.Process) -> None:
        """Exit the discarded process gracefully, then terminate (412 round 12)."""

        async def _reap() -> None:
            try:
                await asyncio.wait_for(proc.wait(), timeout=_REAP_GRACE_SECONDS)
            except TimeoutError:
                proc.terminate()
                try:
                    await asyncio.wait_for(proc.wait(), timeout=_REAP_GRACE_SECONDS)
                except TimeoutError:
                    proc.kill()
                    await proc.wait()

        task = asyncio.create_task(_reap())
        self._reap_tasks.add(task)
        task.add_done_callback(self._reap_tasks.discard)

    @property
    def agent_logs(self) -> list[str]:
        """Return the buffered stderr lines from the active performer process."""
        return list(self._agent_logs)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _build_subprocess_env(self) -> dict[str, str]:
        """Build a minimal, isolated env dict for the performer subprocess (051).

        Only an allowlisted set of host vars is inherited. The GitHub token
        and git identity are injected explicitly so the performer never picks
        up the host user's credentials or git config.
        """
        # Shared with workspace's git env rather than copied: this list had two
        # literal copies, and the Windows fix that added SystemRoot to the other
        # one left this one -- the performer's own env, which does the cloning --
        # unable to resolve a hostname.
        env: dict[str, str] = inherited_host_env()
        env["GIT_TERMINAL_PROMPT"] = "0"

        # self._github_token is the static PAT captured at construction time (None
        # in App mode).  In App mode the performer receives a fresh installation
        # token via the dispatch_card protocol message, so no GITHUB_TOKEN env
        # var is needed here.  Never fall back to the host's GITHUB_TOKEN — that
        # would reintroduce the credential leakage this feature is designed to
        # prevent (spec 051, acceptance scenario 2).
        if self._github_token:
            env["GITHUB_TOKEN"] = self._github_token

        identity = getattr(self._config, "bot_identity", None) if self._config is not None else None
        name = (identity.name if identity is not None else None) or "Coordinare Bot"
        email = (identity.email if identity is not None else None) or "coordinare@localhost"
        env["GIT_AUTHOR_NAME"] = name
        env["GIT_AUTHOR_EMAIL"] = email
        env["GIT_COMMITTER_NAME"] = name
        env["GIT_COMMITTER_EMAIL"] = email

        for var in list(getattr(self._config, "env_passthrough", None) or []):
            if isinstance(var, str) and var in os.environ:
                env[var] = os.environ[var]

        logger.debug("subprocess_transport.env_constructed", var_names=sorted(env.keys()))
        return env

    async def _start(self, *, drain_stderr: bool = False) -> asyncio.subprocess.Process:
        # Only apply isolated env when config is provided (production path).
        # Without config (test fixtures, health checks) inherit the parent env.
        env = self._build_subprocess_env() if self._config is not None else None
        try:
            proc = await asyncio.create_subprocess_exec(
                self._executable,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
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
                self._drain_stderr(proc), name="performer-stderr-drain",
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
        self, message: ProtocolMessage, timeout: int,
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
        data: bytes, *, one_shot: bool, returncode: int | None,
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
