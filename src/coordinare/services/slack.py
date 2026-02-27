from __future__ import annotations

from typing import Any


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
    """Passive configuration holder for Slack (webhook URL + circuit breaker).

    Direct webhook delivery is handled by SlackChannelSender in notification.py.
    """

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
