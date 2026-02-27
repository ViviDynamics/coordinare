from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx
import stamina

if TYPE_CHECKING:
    from coordinare.models.notification import Notification


class SlackError(RuntimeError): ...
class TransientSlackError(SlackError): ...
class PermanentSlackError(SlackError): ...


_DEFAULT_RETRY_KWARGS: dict[str, Any] = {
    "attempts": 1,
    "wait_initial": 0.1,
    "wait_max": 15.0,
    "wait_jitter": 0.0,
    "wait_exp_base": 2.0,
}


class SlackService:
    def __init__(
        self,
        webhook_url: str,
        channel: str,
        *,
        circuit_breaker: Any = None,
        retry_kwargs: dict[str, Any] | None = None,
    ) -> None:
        self._webhook_url = webhook_url
        self._channel = channel
        self._circuit_breaker = circuit_breaker
        self._retry_kwargs = retry_kwargs if retry_kwargs is not None else dict(_DEFAULT_RETRY_KWARGS)

    async def send_notification(self, notification: Notification) -> None:
        @stamina.retry(on=TransientSlackError, **self._retry_kwargs)
        async def _retried_send() -> None:
            payload = {
                "channel": self._channel,
                "text": notification.as_text(),
            }
            try:
                async with httpx.AsyncClient(timeout=10) as client:
                    response = await client.post(self._webhook_url, json=payload)
                    response.raise_for_status()
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                raise TransientSlackError(str(exc)) from exc
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code >= 500:
                    raise TransientSlackError(str(exc)) from exc
                raise PermanentSlackError(str(exc)) from exc

        from coordinare.metrics import METRICS

        try:
            if self._circuit_breaker is not None:
                async with self._circuit_breaker.guard():
                    await _retried_send()
            else:
                await _retried_send()
            METRICS.service_calls_total.labels(
                service="slack", action="send_notification", outcome="success",
            ).inc()
        except Exception:
            METRICS.service_calls_total.labels(
                service="slack", action="send_notification", outcome="failure",
            ).inc()
            raise
