from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from coordinare.protocol import ProtocolMessage, ProtocolResponse


class TransportError(RuntimeError):
    pass


class TransportTimeoutError(TransportError):
    def __init__(self, timeout: int) -> None:
        self.timeout = timeout
        super().__init__(f"Transport timed out after {timeout}s")


class AgentTransport(Protocol):
    async def send(
        self,
        message: ProtocolMessage,
        *,
        timeout_override: int | None = None,
    ) -> ProtocolResponse: ...
