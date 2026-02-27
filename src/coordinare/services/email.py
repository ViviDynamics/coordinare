from __future__ import annotations

from email.message import EmailMessage
from typing import TYPE_CHECKING, Any

import aiosmtplib
import stamina

if TYPE_CHECKING:
    from coordinare.models.notification import Notification


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
    def __init__(
        self,
        host: str,
        port: int,
        *,
        username: str | None = None,
        password: str | None = None,
        sender: str = "coordinare@vividynamics.com",
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

    async def send_notification(self, recipient: str, notification: Notification) -> None:
        @stamina.retry(on=TransientSMTPError, **self._retry_kwargs)
        async def _retried_send() -> None:
            message = EmailMessage()
            message["From"] = self._sender
            message["To"] = recipient
            message["Subject"] = f"[Coordinare] {notification.card_title} -> {notification.card_status}"
            message.set_content(notification.as_text())

            try:
                await aiosmtplib.send(
                    message,
                    hostname=self._host,
                    port=self._port,
                    username=self._username,
                    password=self._password,
                )
            except (
                aiosmtplib.SMTPConnectError,
                aiosmtplib.SMTPServerDisconnected,
                ConnectionError,
                OSError,
            ) as exc:
                raise TransientSMTPError(str(exc)) from exc
            except (
                aiosmtplib.SMTPAuthenticationError,
                aiosmtplib.SMTPRecipientsRefused,
            ) as exc:
                raise PermanentSMTPError(str(exc)) from exc

        from coordinare.metrics import METRICS

        try:
            if self._circuit_breaker is not None:
                async with self._circuit_breaker.guard():
                    await _retried_send()
            else:
                await _retried_send()
            METRICS.service_calls_total.labels(
                service="smtp", action="send_notification", outcome="success",
            ).inc()
        except Exception:
            METRICS.service_calls_total.labels(
                service="smtp", action="send_notification", outcome="failure",
            ).inc()
            raise
