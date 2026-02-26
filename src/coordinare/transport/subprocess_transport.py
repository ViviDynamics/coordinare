from __future__ import annotations

import asyncio
import time

import structlog

from coordinare.protocol import ProtocolMessage, ProtocolResponse
from coordinare.transport.base import TransportError, TransportTimeoutError

logger = structlog.get_logger(__name__)


class SubprocessTransport:
    def __init__(self, executable: str, timeout: int) -> None:
        self._executable = executable
        self._timeout = timeout

    async def send(
        self,
        message: ProtocolMessage,
        *,
        timeout_override: int | None = None,
    ) -> ProtocolResponse:
        effective_timeout = timeout_override if timeout_override is not None else self._timeout
        msg_bytes = message.model_dump_json().encode()

        start = time.monotonic()
        try:
            proc = await asyncio.create_subprocess_exec(
                self._executable,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            msg = f"Failed to start agent process: {exc}"
            raise TransportError(msg) from exc

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=msg_bytes),
                timeout=effective_timeout,
            )
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise TransportTimeoutError(timeout=effective_timeout) from None

        duration_ms = round((time.monotonic() - start) * 1000, 1)

        if stderr:
            logger.debug(
                "agent_stderr",
                stderr=stderr.decode(errors="replace").strip(),
            )

        logger.debug(
            "transport_send_complete",
            action=message.action,
            duration_ms=duration_ms,
        )

        if proc.returncode != 0:
            msg = f"Agent process exited with code {proc.returncode}"
            raise TransportError(msg)

        if not stdout:
            msg = "Agent process produced no output"
            raise TransportError(msg)

        try:
            return ProtocolResponse.model_validate_json(stdout)
        except Exception as exc:
            msg = f"Invalid agent response: {exc}"
            raise TransportError(msg) from exc
