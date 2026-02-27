from __future__ import annotations

import json
from typing import Any, ClassVar

import stamina
from anthropic import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncAnthropic,
    AuthenticationError,
)

from coordinare.metrics import METRICS

# ---------------------------------------------------------------------------
# Exception taxonomy (T023)
# ---------------------------------------------------------------------------


class AnthropicCallError(RuntimeError): ...


class TransientAnthropicError(AnthropicCallError): ...


class PermanentAnthropicError(AnthropicCallError): ...


class ClaudeService:
    _DEFAULT_RETRY_KWARGS: ClassVar[dict[str, Any]] = {
        "attempts": 1,
        "wait_initial": 0.1,
        "wait_max": 30.0,
        "wait_jitter": 0.0,
        "wait_exp_base": 2.0,
    }

    def __init__(
        self,
        api_key: str | None = None,
        model: str = "claude-3-5-sonnet-latest",
        circuit_breaker: Any = None,
        retry_kwargs: dict[str, Any] | None = None,
    ) -> None:
        self._client = AsyncAnthropic(api_key=api_key, max_retries=0)
        self._model = model
        self._circuit_breaker = circuit_breaker
        self._retry_kwargs = retry_kwargs if retry_kwargs is not None else dict(self._DEFAULT_RETRY_KWARGS)

    async def assess_card_sufficiency(self, card: dict[str, Any]) -> dict[str, Any]:
        prompt = (
            "Assess whether the card is sufficient for execution. "
            "Return JSON with fields: sufficient (bool), questions (list[str]), rationale (str)."
        )

        @stamina.retry(on=TransientAnthropicError, **self._retry_kwargs)
        async def _retried_create() -> Any:
            try:
                return await self._client.messages.create(
                    model=self._model,
                    max_tokens=300,
                    messages=[
                        {
                            "role": "user",
                            "content": f"{prompt}\nCard:\n{card}",
                        }
                    ],
                )
            except (APIConnectionError, APITimeoutError) as exc:
                raise TransientAnthropicError(str(exc)) from exc
            except AuthenticationError as exc:
                raise PermanentAnthropicError(str(exc)) from exc
            except APIStatusError as exc:
                if exc.status_code >= 500 or exc.status_code == 429:
                    raise TransientAnthropicError(str(exc)) from exc
                raise PermanentAnthropicError(str(exc)) from exc

        try:
            if self._circuit_breaker is not None:
                async with self._circuit_breaker.guard():
                    response = await _retried_create()
            else:
                response = await _retried_create()
            METRICS.service_calls_total.labels(
                service="anthropic", action="assess_card", outcome="success",
            ).inc()
        except Exception:
            METRICS.service_calls_total.labels(
                service="anthropic", action="assess_card", outcome="failure",
            ).inc()
            raise

        text = ""
        if getattr(response, "content", None):
            blocks = response.content
            if blocks and hasattr(blocks[0], "text"):
                text = blocks[0].text
        if not text:
            return {"sufficient": True, "questions": [], "rationale": "empty model response"}
        try:
            parsed = json.loads(text)
            return {
                "sufficient": bool(parsed.get("sufficient", False)),
                "questions": list(parsed.get("questions", [])),
                "rationale": str(parsed.get("rationale", text)),
            }
        except (json.JSONDecodeError, AttributeError):
            # Fall back to heuristic if model response is not valid JSON
            lowered = text.lower()
            sufficient = '"sufficient": true' in lowered or "sufficient: true" in lowered
            if sufficient:
                return {"sufficient": True, "questions": [], "rationale": text}
            return {
                "sufficient": False,
                "questions": ["Please clarify acceptance criteria."],
                "rationale": text,
            }
