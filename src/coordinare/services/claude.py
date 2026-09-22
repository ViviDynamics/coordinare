from __future__ import annotations

import json
import re
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
from coordinare.observability import get_current_symphony

# ---------------------------------------------------------------------------
# Exception taxonomy (T023)
# ---------------------------------------------------------------------------


def _repair_truncated_json(text: str) -> str | None:
    """Best-effort repair of a JSON object truncated mid-stream.

    Walks the text tracking string state and bracket depth so we can:
      * Distinguish escaped quotes from real string delimiters.
      * Close the open string (if any) before closing brackets.
      * Strip a trailing comma that would otherwise leave invalid JSON.
      * Close arrays and objects in the correct order.

    Returns the repaired string, or ``None`` if the text cannot plausibly be
    coerced into a JSON object (does not start with ``{``, or repair would
    require fabricating values).
    """
    stripped = text.strip()
    if not stripped.startswith("{"):
        return None
    in_string = False
    escape = False
    # Stack of open container chars: '{' or '['.
    stack: list[str] = []
    last_non_ws: str = ""
    for ch in stripped:
        if escape:
            escape = False
            continue
        if ch == "\\" and in_string:
            escape = True
            continue
        if ch == '"':
            in_string = not in_string
        elif not in_string:
            if ch in "{[":
                stack.append(ch)
            elif ch in "}]":
                if stack and ((ch == "}" and stack[-1] == "{") or (ch == "]" and stack[-1] == "[")):
                    stack.pop()
                else:
                    # Mismatched closer — not safely repairable.
                    return None
        if not ch.isspace():
            last_non_ws = ch
    repair = stripped
    if in_string:
        # If the truncation landed right after a backslash, drop it so the
        # closing quote is not interpreted as an escape sequence.
        repair = repair.removesuffix("\\")
        repair += '"'
    # Trim a dangling comma or colon that would otherwise leave the object
    # expecting another value (e.g. `{"a": 1,` or `{"a":`).
    trimmed = repair.rstrip()
    while trimmed and trimmed[-1] in ",:":
        trimmed = trimmed[:-1].rstrip()
    if trimmed != repair:
        # If we trimmed a `:`, we'd need to invent a value; bail out.
        if last_non_ws == ":":
            return None
        repair = trimmed
    # Close any open containers in reverse order.
    closers = {"{": "}", "[": "]"}
    repair += "".join(closers[c] for c in reversed(stack))
    return repair


def _try_parse_json_object(text: str) -> dict[str, Any] | None:
    """Lenient JSON-object extractor for model responses.

    Handles four failure modes we see in production:
      1. Exact JSON — happy path.
      2. JSON wrapped in ```json ... ``` code fences.
      3. JSON embedded in a brief prose preamble (greedy brace extraction).
      4. JSON truncated mid-string by max_tokens — see :func:`_repair_truncated_json`.
    Returns the parsed dict or None if nothing salvageable.
    """
    if not text:
        return None
    candidates: list[str] = [text.strip()]
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        candidates.append(fence.group(1))
    brace = re.search(r"\{.*\}", text, re.DOTALL)
    if brace:
        candidates.append(brace.group(0))
    for cand in candidates:
        try:
            parsed = json.loads(cand)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            continue
    repair = _repair_truncated_json(text)
    if repair is not None:
        try:
            parsed = json.loads(repair)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            return None
    return None


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
        model: str = "claude-sonnet-4-20250514",
        circuit_breaker: Any = None,
        retry_kwargs: dict[str, Any] | None = None,
        max_tokens: int = 4096,
        temperature: float | None = None,
        base_url: str | None = None,
    ) -> None:
        client_kwargs: dict[str, Any] = {"api_key": api_key, "max_retries": 0}
        if base_url:
            client_kwargs["base_url"] = base_url
        self._client = AsyncAnthropic(**client_kwargs)
        self._model = model
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._circuit_breaker = circuit_breaker
        self._retry_kwargs = retry_kwargs if retry_kwargs is not None else dict(self._DEFAULT_RETRY_KWARGS)

    async def prompt_text(self, text: str, *, response_format: str | None = None) -> str:
        """Send an arbitrary text prompt and return the model's raw text response.

        When *response_format* is ``"json"``, a system instruction is added
        asking the model to respond with valid JSON only.
        """
        system_msg: str | None = None
        if response_format == "json":
            system_msg = "Respond with valid JSON only."

        @stamina.retry(on=TransientAnthropicError, **self._retry_kwargs)
        async def _retried_create() -> Any:
            try:
                kwargs: dict[str, Any] = {
                    "model": self._model,
                    "max_tokens": self._max_tokens,
                    "messages": [{"role": "user", "content": text}],
                }
                if system_msg:
                    kwargs["system"] = system_msg
                if self._temperature is not None:
                    kwargs["temperature"] = self._temperature
                return await self._client.messages.create(**kwargs)
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
                symphony=get_current_symphony(),
                service="anthropic",
                action="prompt",
                outcome="success",
            ).inc()
        except Exception:
            METRICS.service_calls_total.labels(
                symphony=get_current_symphony(),
                service="anthropic",
                action="prompt",
                outcome="failure",
            ).inc()
            raise

        if getattr(response, "content", None):
            blocks = response.content
            if blocks and hasattr(blocks[0], "text"):
                return str(blocks[0].text)
        return ""

    async def assess_card_sufficiency(self, card: dict[str, Any]) -> dict[str, Any]:
        clarifications: list[dict[str, Any]] = card.get("clarifications", []) if isinstance(card, dict) else []

        if clarifications:
            history_lines = []
            for entry in clarifications:
                qs = entry.get("questions") or []
                ans = str(entry.get("answer", "")).strip()
                if qs:
                    history_lines.append("Questions asked:\n" + "\n".join(f"  - {q}" for q in qs))
                if ans:
                    history_lines.append(f"User answered:\n  {ans}")
            history_text = "\n".join(history_lines)
            prompt = (
                "You are helping clarify a software feature before implementation.\n\n"
                f"Card:\n{card}\n\n"
                f"Clarification history:\n{history_text}\n\n"
                "Based on the card and the conversation above, decide if there is now enough "
                "information to implement this feature. If yes, set sufficient=true. "
                "If information is still missing, generate 3-5 specific follow-up questions "
                "targeting exactly what is still unclear — do not repeat questions already answered.\n\n"
                'Return JSON: {"sufficient": bool, "questions": [str], "rationale": str}'
            )
        else:
            prompt = (
                "You are assessing whether a software feature card has enough information to implement.\n\n"
                f"Card:\n{card}\n\n"
                "Check for: clear requirements, defined scope, acceptance criteria, affected components, "
                "edge cases, and any technical constraints.\n"
                "If the card is missing critical information, generate 3-5 specific targeted questions "
                "that will unblock implementation — be concrete, not generic.\n\n"
                'Return JSON: {"sufficient": bool, "questions": [str], "rationale": str}'
            )

        @stamina.retry(on=TransientAnthropicError, **self._retry_kwargs)
        async def _retried_create() -> Any:
            try:
                kwargs: dict[str, Any] = {
                    "model": self._model,
                    "max_tokens": self._max_tokens,
                    "system": "Respond ONLY with valid JSON. Do not include any prose, markdown, or code fences.",
                    "messages": [{"role": "user", "content": prompt}],
                }
                if self._temperature is not None:
                    kwargs["temperature"] = self._temperature
                return await self._client.messages.create(**kwargs)
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
                symphony=get_current_symphony(),
                service="anthropic",
                action="assess_card",
                outcome="success",
            ).inc()
        except Exception:
            METRICS.service_calls_total.labels(
                symphony=get_current_symphony(),
                service="anthropic",
                action="assess_card",
                outcome="failure",
            ).inc()
            raise

        text = ""
        if getattr(response, "content", None):
            blocks = response.content
            if blocks and hasattr(blocks[0], "text"):
                text = blocks[0].text
        if not text:
            return {"sufficient": True, "questions": [], "rationale": "empty model response"}
        parsed = _try_parse_json_object(text)
        if parsed is not None:
            return {
                "sufficient": bool(parsed.get("sufficient", False)),
                "questions": list(parsed.get("questions", [])),
                "rationale": str(parsed.get("rationale", text)),
            }
        # Model returned non-JSON (e.g. safety refusal or formatting error).
        # Block the card so the operator can inspect rather than silently
        # dispatching an unassessed card to implementation.
        # Non-empty questions prevents assess_card's auto-promotion logic
        # (not sufficient + no questions → treated as sufficient).
        return {
            "sufficient": False,
            "questions": ["assessment parse error — model returned non-JSON"],
            "rationale": f"assessment parse error: {text}",
        }
