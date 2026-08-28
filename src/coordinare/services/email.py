from __future__ import annotations

from typing import Any


class SMTPDeliveryError(RuntimeError): ...
class TransientSMTPError(SMTPDeliveryError): ...
class PermanentSMTPError(SMTPDeliveryError): ...


_DEFAULT_RETRY_KWARGS: dict[str, Any] = {
    "attempts": 1,
    "wait_initial": 0.1,
    "wait_max": 20.0,
    "wait_jitter": 0.0,
    "wait_exp_base": 2.0,
}


class EmailService:
    """Passive configuration holder for SMTP delivery.

    Direct SMTP delivery is handled by EmailChannelSender in notification.py.
    """

    def __init__(
        self,
        host: str,
        port: int,
        *,
        username: str | None = None,
        password: str | None = None,
        sender: str = "coordinare@localhost",
        circuit_breaker: Any = None,
        retry_kwargs: dict[str, Any] | None = None,
    ) -> None:
        self._host = host
        self._port = port
        self._username = username
        self._password = password
        self._sender = sender
        self._circuit_breaker = circuit_breaker
        self._retry_kwargs = retry_kwargs if retry_kwargs is not None else dict(_DEFAULT_RETRY_KWARGS)
