"""Pluggable card assessment backends for the assess_card graph node."""
from __future__ import annotations

import asyncio
import contextlib
import json
import re
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

import structlog

from coordinare.services.conductor_calls import (
    NULL_CALL_LOG,
    ConductorCallLog,
    ModelReply,
    ModelRequest,
    split_messages,
)
from coordinare.services.environment_evidence import check_evidence_context
from coordinare.services.environment_function_wire import (
    INSTRUCTION,
    decode_function,
    functions,
    load_unique,
)
from coordinare.services.environment_proposal_patch import text_schema
from coordinare.services.model_output_cap import (
    REPETITION_FINISH,
    capped,
    degenerate,
    provider_outage_seconds,
    repetition_guard,
)

if TYPE_CHECKING:
    from coordinare.config import ProjectConfiguration
    from coordinare.services.claude import ClaudeService
    from coordinare.services.environment_attempt import DiscoveryChatReply

log = structlog.get_logger(__name__)

def _build_assess_prompt(card: dict[str, Any]) -> str:
    """Build a context-aware assessment prompt from card data.

    Accepts both raw issue dicts (from get_issue_details, which includes url,
    labels, and comments) and card dicts stored in state (which use 'description'
    instead of 'body').  Inlines all available context so the model can ask
    targeted questions and avoid repeating ones already answered.

    If card contains a non-empty ``persona_instructions`` key, those instructions
    are prepended as a ``## Assessor Instructions`` section (018-performer-personas).
    """
    # Persona instructions prefix (018-performer-personas)
    persona_prefix = ""
    persona_instructions = str(card.get("persona_instructions", "") or "").strip()
    if persona_instructions:
        persona_prefix = f"## Assessor Instructions\n{persona_instructions}\n\n"

    title = str(card.get("title", "")).strip()
    body = str(card.get("body", "") or card.get("description", "")).strip()
    url = str(card.get("url", "")).strip()
    clarifications: list[dict[str, Any]] = card.get("clarifications", []) if isinstance(card, dict) else []

    # Labels
    labels_node = card.get("labels")
    label_names: list[str] = []
    if isinstance(labels_node, dict):
        label_names = [
            str(n.get("name", "")) for n in labels_node.get("nodes", [])
            if isinstance(n, dict) and n.get("name")
        ]
    elif isinstance(labels_node, list):
        label_names = [str(lbl) for lbl in labels_node if lbl]

    # Existing comments written by humans (exclude coordinare "Needs input:" comments)
    comments_node = card.get("comments")
    human_comments: list[str] = []
    if isinstance(comments_node, dict):
        for c in comments_node.get("nodes", []):
            if not isinstance(c, dict):
                continue
            body_text = str(c.get("body", "")).strip()
            author = str(c.get("author", {}).get("login", "") if isinstance(c.get("author"), dict) else "")
            if body_text and not body_text.startswith("Needs input:"):
                human_comments.append(f"  [{author}]: {body_text}")

    # Build the card section
    lines = [f"Title: {title}"]
    if url:
        lines.append(f"URL: {url}")
    if label_names:
        lines.append(f"Labels: {', '.join(label_names)}")
    lines.append(f"Description:\n{body}" if body else "Description: (none provided)")
    if human_comments:
        lines.append("Existing comments:\n" + "\n".join(human_comments))
    card_text = "\n".join(lines)

    if clarifications:
        history_lines = []
        for entry in clarifications:
            comment = entry.get("body")
            if isinstance(comment, str) and comment.strip():
                identity = ", ".join(
                    f"{key}: {entry[key]}"
                    for key in ("source", "comment_id", "author") if entry.get(key)
                )
                history_lines.append(f"Human clarification ({identity}):\n{comment}")
            qs = entry.get("questions") or []
            ans = str(entry.get("answer", "")).strip()
            if qs:
                history_lines.append("Questions asked:\n" + "\n".join(f"  - {q}" for q in qs))
            if ans:
                history_lines.append(f"User answered:\n  {ans}")
        history_text = "\n".join(history_lines)
        return (
            persona_prefix
            + "You are reviewing a software feature card to determine if it has enough detail to implement.\n\n"
            f"{card_text}\n\n"
            f"Clarification conversation so far:\n{history_text}\n\n"
            "Based on all the above, decide if there is now sufficient information to implement this feature.\n"
            "If yes, set sufficient=true.\n"
            "If information is still missing, generate 3-5 specific follow-up questions targeting exactly "
            "what is still unclear — do not repeat questions already answered, and reference details from "
            "the card or prior answers in your questions.\n\n"
            'Return only valid JSON: {"sufficient": bool, "questions": [str], "rationale": str}'
        )

    return (
        persona_prefix
        + "You are reviewing a software feature card to determine if it has enough detail to implement.\n\n"
        f"{card_text}\n\n"
        "Check for: clear requirements, defined scope, affected components or routes, acceptance criteria, "
        "and technical constraints or edge cases.\n"
        "If the card has enough information to begin implementation, set sufficient=true.\n"
        "If critical information is missing, set sufficient=false and generate 3-5 specific, concrete "
        "questions that would directly unblock a developer. Reference the actual feature title and any "
        "details from the card in your questions — do NOT ask generic questions like "
        "'What are the requirements?' or 'Please provide more details'.\n\n"
        'Return only valid JSON: {"sufficient": bool, "questions": [str], "rationale": str}'
    )


def _parse_assessment_response(text: str) -> dict[str, Any]:
    """Parse a model text response into an assessment dict.

    Tries several strategies in order:
    1. Direct JSON parse of the full text.
    2. Strip markdown code fences and parse the inner block.
    3. Extract the first JSON object containing "sufficient" via regex.
    4. Keyword heuristic as a last resort.
    """
    candidates: list[str] = [text.strip()]

    # Strategy 2: strip code fences
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        inner = "\n".join(lines[1:-1]) if len(lines) > 2 else stripped
        candidates.append(inner.strip())

    # Strategy 3: find JSON object containing "sufficient" anywhere in the text
    match = re.search(r'\{[^{}]*"sufficient"[^{}]*\}', text, re.DOTALL)
    if match:
        candidates.append(match.group())

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict) and "sufficient" in parsed:
                result: dict[str, Any] = {
                    "sufficient": bool(parsed.get("sufficient", False)),
                    "questions": list(parsed.get("questions", [])),
                    "rationale": str(parsed.get("rationale", text)),
                }
                # 046: Pass through assessor-detected dependencies so
                # assess_card can block cards with implicit blockers.
                raw_deps = parsed.get("dependencies")
                if isinstance(raw_deps, list) and raw_deps:
                    result["dependencies"] = raw_deps
                return result
        except (json.JSONDecodeError, AttributeError, ValueError):
            continue

    # Strategy 4: keyword heuristic
    lowered = text.lower()
    sufficient = (
        '"sufficient": true' in lowered
        or '"sufficient":true' in lowered
        or "sufficient: true" in lowered
    )
    if sufficient:
        return {"sufficient": True, "questions": [], "rationale": text}
    # Return empty questions — handle_blocked will generate them from the card title
    return {"sufficient": False, "questions": [], "rationale": text}


_CODE_FENCE_RE = re.compile(r"```(?:json|JSON)?\s*\n?(.*?)\n?\s*```", re.DOTALL)


def _parse_prompt_response(text: str, response_format: str | None) -> dict[str, Any]:
    """Build a prompt response dict, optionally parsing JSON from the text.

    When ``response_format="json"``, tries multiple strategies:
    1. Parse the full text as JSON.
    2. Extract the first code fence (labeled or unlabeled) and parse its contents.
    3. Log a warning if all strategies fail.
    """
    text = text.strip() if text else ""
    result: dict[str, Any] = {"text": text, "data": None}
    if response_format == "json" and text:
        # Strategy 1: full text is JSON
        with contextlib.suppress(json.JSONDecodeError, ValueError):
            result["data"] = json.loads(text)
        # Strategy 2: extract JSON from code fence anywhere in text
        if result["data"] is None:
            match = _CODE_FENCE_RE.search(text)
            if match:
                with contextlib.suppress(json.JSONDecodeError, ValueError):
                    result["data"] = json.loads(match.group(1).strip())
        if result["data"] is None:
            log.warning("prompt_response_json_parse_failed", text_length=len(text))
    return result


def _openai_usage(data: Any) -> dict[str, int] | None:
    """Usage of a chat completion, in the ledger's kinds (131). Cached prompt tokens are not input."""
    usage = data.get("usage") if isinstance(data, dict) else None
    if not isinstance(usage, dict):
        return None
    prompt_tokens = usage.get("prompt_tokens")
    completion_tokens = usage.get("completion_tokens")
    details = usage.get("prompt_tokens_details")
    cached = details.get("cached_tokens") if isinstance(details, dict) else None
    cached = usage.get("cache_read_input_tokens") if cached is None else cached
    prompt = prompt_tokens if isinstance(prompt_tokens, int) and not isinstance(prompt_tokens, bool) else 0
    output = completion_tokens if isinstance(completion_tokens, int) and not isinstance(completion_tokens, bool) else 0
    read = cached if isinstance(cached, int) and not isinstance(cached, bool) else 0
    return {"input_tokens": max(prompt - read, 0), "output_tokens": output, "cache_read": read, "cache_write": 0}


# A degenerate reply is checked again only after this many new characters.
REPETITION_CHECK_CHARS = 256


async def _streamed_completion(client: Any, url: str, payload: dict[str, Any],
                               headers: dict[str, str]) -> tuple[Any, dict[str, Any] | None]:
    """One streamed chat completion, reassembled as the non-streamed shape; None when the stream is malformed."""
    async with client.stream("POST", url, json=payload, headers=headers) as resp:
        if resp.is_error:
            await resp.aread()
            return resp, None
        return resp, await _collect_stream(resp)


async def _collect_stream(resp: Any) -> dict[str, Any] | None:
    """Accumulate content and reasoning deltas; stop reading once either has degenerated into exact repetition.

    Closing the stream early ends generation on the provider, so a looping reply costs seconds, not the cap.
    """
    text = thought = ""
    checked = 0
    model: str | None = None
    usage: Any = None
    finish: str | None = None
    async for line in resp.aiter_lines():
        if not line.startswith("data:"):
            continue
        body = line[5:].strip()
        if body == "[DONE]":
            break
        try:
            chunk = json.loads(body)
        except ValueError:
            return None
        if not isinstance(chunk, dict):
            return None
        model = chunk.get("model") if isinstance(chunk.get("model"), str) else model
        usage = chunk.get("usage") if isinstance(chunk.get("usage"), dict) else usage
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") if isinstance(choice, dict) else None
            if isinstance(delta, dict):
                text += delta["content"] if isinstance(delta.get("content"), str) else ""
                reasoning = delta.get("reasoning_content", delta.get("reasoning"))
                thought += reasoning if isinstance(reasoning, str) else ""
            if isinstance(choice, dict) and isinstance(choice.get("finish_reason"), str):
                finish = choice["finish_reason"]
        if len(text) + len(thought) - checked >= REPETITION_CHECK_CHARS:
            checked = len(text) + len(thought)
            if degenerate(text) or degenerate(thought):
                finish = REPETITION_FINISH
                break
    # A provider may close a looping stream itself, without a finish reason, before the next check (v33: a reply of
    # blank lines); the final text is checked once more so that stop is still recognised as repetition.
    if finish is None and (degenerate(text) or degenerate(thought)):
        finish = REPETITION_FINISH
    return {"model": model, "usage": usage, "choices": [{"message": {"content": text}, "finish_reason": finish}]}


@runtime_checkable
class ConductingBackend(Protocol):
    async def assess(self, card: dict[str, Any]) -> dict[str, Any]: ...
    async def prompt(self, text: str, response_format: str | None = None) -> dict[str, Any]: ...


class AnthropicApiBackend:
    """Delegates to ClaudeService — uses the Anthropic SDK directly."""

    def __init__(self, claude_service: ClaudeService) -> None:
        self._svc = claude_service

    @property
    def calls(self) -> ConductorCallLog:
        """The call log of the service, so a conductor call opened on this backend is recorded (131)."""
        return self._svc.calls

    async def assess(self, card: dict[str, Any]) -> dict[str, Any]:
        return await self._svc.assess_card_sufficiency(card)

    async def prompt(self, text: str, response_format: str | None = None) -> dict[str, Any]:
        """Send an arbitrary text prompt to the Anthropic API."""
        if not text.strip():
            return {"text": "", "data": None}
        raw = await self._svc.prompt_text(text, response_format=response_format)
        return _parse_prompt_response(raw, response_format)


SCHEMA_LIMIT_ERROR = "schema_limit"
# Provider refusals to compile a strict response schema, before any generation.
SCHEMA_LIMIT_MARKERS = ("grammar is too large", "schema is too complex")


_BAD_REQUEST = 400


def _schema_limit_rejection(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    return (getattr(response, "status_code", None) == _BAD_REQUEST
            and any(marker in str(getattr(response, "text", "")).lower() for marker in SCHEMA_LIMIT_MARKERS))


class OpenAiApiBackend:
    """Direct OpenAI Chat Completions call via httpx — no SDK dep required.

    Supports any OpenAI-compatible endpoint (Azure, OpenRouter, local proxies)
    by setting ``base_url``.  Auth comes from OPENAI_API_KEY.
    """

    # 180s, not 60: the gateway's only self-hosted model (glm-5.3-flash) is a
    # reasoning model and spent 29s on a short assessment probe; a real card
    # body thinks longer. A timeout here returns a synthetic 'insufficient'
    # assessment, so a cap that is too tight silently stalls every card.
    _TIMEOUT: float = 180.0

    def __init__(
        self,
        api_key: str | None,
        model: str = "gpt-4o-mini",
        max_tokens: int = 4096,
        temperature: float | None = None,
        base_url: str | None = None,
        effort: str | None = None,
        calls: ConductorCallLog | None = None,
        endpoint_name: str = "openai_api",
        structured_transport: str = "json_schema",
    ) -> None:
        self._api_key = api_key or ""
        self._model = model
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._effort = effort
        self._base_url = (base_url or "https://api.openai.com/v1").rstrip("/")
        # 131: every send is recorded on the conductor call that is open, if any.
        self.calls = calls if calls is not None else NULL_CALL_LOG
        self._endpoint_name = endpoint_name
        if structured_transport not in {"json_schema", "tool_call", "json_object"}:
            message = "unsupported structured transport"
            raise ValueError(message)
        self._structured_transport = structured_transport

    # Transient-error retry budget. Three attempts total at 0.5s / 1.5s
    # backoff — enough to ride out a LiteLLM/proxy hiccup or a brief 429
    # without compounding latency on a hard outage.
    _MAX_ATTEMPTS: int = 3
    _RETRY_DELAYS_S: tuple[float, ...] = (0.5, 1.5)
    # A caller that sets provider_outage_seconds keeps retrying past those, with growing waits up to this one.
    _OUTAGE_DELAYS_S: tuple[float, ...] = (15.0, 30.0, 60.0, 120.0)

    def _retry_delay(self, attempt: int, first_failure: float) -> float | None:
        """The wait before retrying a transient failure, or None when the call should give up.

        Dettmore e2e-26 and e2e-28 to e2e-31 each held within seconds of a provider outage (dropped connections,
        then HTTP 500s for over an hour) on the three quick attempts below; a caller's outage window rides it out.
        """
        if attempt < len(self._RETRY_DELAYS_S):
            return self._RETRY_DELAYS_S[attempt]
        remaining = provider_outage_seconds.get() - (time.monotonic() - first_failure)
        if remaining <= 0:
            return None
        step = min(attempt - len(self._RETRY_DELAYS_S), len(self._OUTAGE_DELAYS_S) - 1)
        return min(self._OUTAGE_DELAYS_S[step], remaining)

    async def _waited_to_retry(self, attempt: int, first_failure: float, **failure: Any) -> bool:
        """Logs and waits before another attempt; False when the call should give up instead."""
        if (delay := self._retry_delay(attempt, first_failure)) is None:
            return False
        log.warning("openai_api_retry", attempt=attempt + 1, **failure)
        await asyncio.sleep(delay)
        return True

    async def _chat(self, messages: list[dict[str, str]], json_mode: bool) -> str:
        return (await self._chat_reply(messages, json_mode)).text

    async def _chat_reply(self, messages: list[dict[str, str]], json_mode: bool, *, response_schema: dict[str, Any] | None = None,
                          transport: str | None = None) -> ModelReply:
        """The reply text of one chat call ('' on any failure), recorded on the open call (131)."""
        system, user = split_messages(messages)
        parameters = self._request_parameters(json_mode)
        if response_schema is not None:
            parameters["response_schema"] = response_schema
            parameters["structured_transport"] = transport or self._structured_transport
        request = ModelRequest(
            model=self._model,
            endpoint=self._endpoint_name,
            system=system,
            prompt=user,
            parameters=parameters,
        )
        if response_schema is not None:
            return await self.calls.invoke(request, lambda: self._post_chat(messages, json_mode, response_schema=response_schema,
                                                                            transport=transport))
        return await self.calls.invoke(request, lambda: self._post_chat(messages, json_mode))

    def _request_parameters(self, json_mode: bool) -> dict[str, Any]:
        parameters: dict[str, Any] = {"max_tokens": self._max_tokens, "json_mode": json_mode}
        if self._effort is not None:
            parameters["effort"] = self._effort
        if self._temperature is not None:
            parameters["temperature"] = self._temperature
        return parameters

    def _response_parameters(self, schema: dict[str, Any] | None, json_mode: bool, transport: str | None = None) -> dict[str, Any]:
        transport = transport or self._structured_transport
        if schema is not None and transport == "json_object":
            return {"response_format": {"type": "json_object"}}
        if schema is not None and transport == "tool_call":
            return {"tools": functions(schema), "tool_choice": "required", "parallel_tool_calls": False}
        if schema is not None:
            return {"response_format": {"type": "json_schema", "json_schema": {
                "name": "coordinare_environment_response", "strict": True, "schema": schema}}}
        return {"response_format": {"type": "json_object"}} if json_mode else {}

    def _streams(self, payload: dict[str, Any], response_schema: dict[str, Any] | None, transport: str | None) -> bool:
        """Streamed only so a degenerate reply can be stopped early; native tool calls stay whole."""
        stream = repetition_guard.get() and not (response_schema is not None
                                                 and (transport or self._structured_transport) == "tool_call")
        if stream:
            payload.update(stream=True, stream_options={"include_usage": True})
        return stream

    @staticmethod
    async def _request_completion(client: Any, url: str, payload: dict[str, Any], headers: dict[str, str],
                                  mode: str) -> tuple[Any, Any, str | None]:
        """One request: the response, its parsed completion, and why the completion could not be parsed.

        ``mode`` is ``stream``, ``strict`` (duplicate keys rejected) or ``plain``.
        """
        if mode == "stream":
            resp, streamed = await _streamed_completion(client, url, payload, headers)
            return resp, streamed, None if streamed is not None else "invalid provider JSON"
        resp = await client.post(url, json=payload, headers=headers)
        try:
            return resp, load_unique(resp.text) if mode == "strict" else resp.json(), None
        except (ValueError, RecursionError):
            return resp, None, "invalid provider JSON"

    async def _post_chat(self, messages: list[dict[str, str]], json_mode: bool, *, response_schema: dict[str, Any] | None = None,
                         transport: str | None = None) -> ModelReply:
        """The HTTP send with its retries. Failures come back as ``error``, never raised."""
        import httpx
        if not self._api_key:
            log.error("openai_api_missing_key")
            return ModelReply(error="missing api key")
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": messages,
        }
        # Reasoning models (o-series / gpt-5) reject `max_tokens` and `temperature`
        # and require `max_completion_tokens`. Use the presence of `effort` as the
        # signal: the config layer ties `effort` to reasoning-capable models.
        if self._effort is not None:
            payload["max_completion_tokens"] = capped(self._max_tokens)
            payload["reasoning_effort"] = self._effort
        else:
            payload["max_tokens"] = capped(self._max_tokens)
            if self._temperature is not None:
                payload["temperature"] = self._temperature
        payload.update(self._response_parameters(response_schema, json_mode, transport))
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }

        stream = self._streams(payload, response_schema, transport)
        data: Any = None
        last_error: str | None = None
        first_failure: float | None = None
        attempt = -1
        while True:
            attempt += 1
            try:
                async with httpx.AsyncClient(timeout=self._TIMEOUT) as client:
                    resp, parsed, parse_error = await self._request_completion(
                        client, f"{self._base_url}/chat/completions", payload, headers,
                        "stream" if stream else "strict" if response_schema is not None else "plain")
                # Retry only transient server-side conditions; 4xx auth/bad-
                # request errors won't recover and burning the budget on them
                # just delays the eventual empty-string return.
                if resp.status_code >= 500 or resp.status_code == 429:
                    last_error = f"http_{resp.status_code}"
                    first_failure = time.monotonic() if first_failure is None else first_failure
                    if await self._waited_to_retry(attempt, first_failure, status=resp.status_code):
                        continue
                resp.raise_for_status()
                data, last_error = parsed, parse_error or last_error
                break
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                last_error = type(exc).__name__
                first_failure = time.monotonic() if first_failure is None else first_failure
                if await self._waited_to_retry(attempt, first_failure, error=last_error):
                    continue
                log.error("openai_api_http_error", error=str(exc))
                return ModelReply(error=last_error, timed_out=isinstance(exc, httpx.TimeoutException))
            except httpx.HTTPError as exc:
                # Non-retryable (e.g. 4xx after raise_for_status).
                log.error("openai_api_http_error", error=str(exc))
                limited = response_schema is not None and _schema_limit_rejection(exc)
                return ModelReply(error=SCHEMA_LIMIT_ERROR if limited else type(exc).__name__)

        if data is None:
            log.error("openai_api_http_error", error=last_error or "retries_exhausted")
            return ModelReply(error=last_error or "retries_exhausted")
        try:
            message = data["choices"][0]["message"]
            text = json.dumps(message) if response_schema is not None and (transport or self._structured_transport) == "tool_call" else str(message["content"] or "")
        except (KeyError, IndexError, TypeError):
            log.warning("openai_api_unexpected_response", keys=list(data.keys()) if isinstance(data, dict) else None)
            return ModelReply(error="unexpected response")
        model = data.get("model") if isinstance(data, dict) else None
        return ModelReply(
            text=text,
            model=model if isinstance(model, str) else None,
            usage=_openai_usage(data),
            finish_reason=data["choices"][0].get("finish_reason") if isinstance(data["choices"][0].get("finish_reason"), str) else None,
        )

    async def chat_json(self, messages: list[dict[str, str]]) -> str:
        """Public JSON-mode chat for ad-hoc coordinare prompts (e.g. 077 env
        manifest README pass). Returns the assistant text ('' on any failure)."""
        return await self._chat(messages, json_mode=True)

    async def chat_json_with_metadata(self, messages: list[dict[str, str]], *, response_schema: dict[str, Any] | None = None,
                                      max_context_bytes: int | None = None, context_phase: str = "discovery") -> DiscoveryChatReply:
        """Keep diagnostics tied to this request without changing string-only callers."""
        from coordinare.services.environment_attempt import DiscoveryChatReply

        reply, text, error_code = await self._structured_chat(messages, response_schema, self._structured_transport,
                                                              max_context_bytes, context_phase)
        fallback = None
        if (reply.error == SCHEMA_LIMIT_ERROR and response_schema is not None
                and self._structured_transport != "json_object"):
            # The provider refused to compile this schema before generating anything; resend the same
            # request once with the schema in the prompt. Local validation remains authoritative.
            log.warning("structured_schema_fallback", phase=context_phase, transport="json_object")
            fallback = "json_object"
            reply, text, error_code = await self._structured_chat(messages, response_schema, fallback,
                                                                  max_context_bytes, context_phase)
        return DiscoveryChatReply(text=text, usage=reply.usage, finish_reason=reply.finish_reason, error_code=error_code,
            wire_text=reply.text if response_schema is not None else None, transport_fallback=fallback)

    async def _structured_chat(self, messages: list[dict[str, str]], canonical_schema: dict[str, Any] | None, transport: str,
                               max_context_bytes: int | None, context_phase: str) -> tuple[ModelReply, str, str | None]:
        from coordinare.services.environment_structured_wire import (
            WIRE_INSTRUCTION,
            decode_wire,
            wire_schema,
        )

        function_mode = transport == "tool_call"
        text_mode = transport == "json_object"
        instruction = ("Transport: return exactly one canonical JSON object in the logical phase envelope described above. "
            "No native function calls, prose, markdown, or response wrapper. All local validation, source evidence, "
            "independent review and required execution rules remain mandatory.") if text_mode else (INSTRUCTION if function_mode else WIRE_INSTRUCTION)
        response_schema = canonical_schema
        if canonical_schema is not None:
            if text_mode:
                instruction += "\n\nAuthoritative logical response schema for this request:\n" + json.dumps(text_schema(canonical_schema))
            messages = [dict(message) for message in messages]
            if messages and messages[0]["role"] == "system":
                messages[0]["content"] += "\n\n" + instruction
            else:
                messages.insert(0, {"role": "system", "content": instruction})
            response_schema = canonical_schema if function_mode or text_mode else wire_schema(canonical_schema)
        if max_context_bytes is not None:
            check_evidence_context(messages, max_bytes=max_context_bytes, phase=context_phase)
        reply = await self._chat_reply(messages, json_mode=True, response_schema=response_schema, transport=transport)
        text = reply.text
        error_code = "provider_request_failed" if reply.error else None
        if canonical_schema is not None and not error_code and not text_mode:
            try:
                text = decode_function(text, canonical_schema) if function_mode else decode_wire(text, canonical_schema)
            except (ValueError, TypeError, RecursionError, KeyError):
                text, error_code = "", "provider_response_invalid"
        return reply, text, error_code

    async def assess(self, card: dict[str, Any]) -> dict[str, Any]:
        prompt = _build_assess_prompt(card)
        text = await self._chat(
            [
                {"role": "system", "content": "Respond ONLY with valid JSON. No prose, markdown, or code fences."},
                {"role": "user", "content": prompt},
            ],
            json_mode=True,
        )
        if not text:
            return {"sufficient": False, "questions": [], "rationale": "empty openai response"}
        return _parse_assessment_response(text)

    async def prompt(self, text: str, response_format: str | None = None) -> dict[str, Any]:
        if not text.strip():
            return {"text": "", "data": None}
        messages: list[dict[str, str]] = []
        if response_format == "json":
            messages.append({"role": "system", "content": "Respond with valid JSON only."})
        messages.append({"role": "user", "content": text})
        raw = await self._chat(messages, json_mode=(response_format == "json"))
        return _parse_prompt_response(raw, response_format)


@dataclass(frozen=True)
class _CliSpec:
    """How one model CLI reports: its note label, its timeout, and its log-event template (``{purpose}``)."""

    label: str
    limit: float
    events: str


async def _run_cli(
    executable: str, args: list[str], prompt: str, *, spec: _CliSpec, purpose: str,
) -> ModelReply:
    """Pipe ``prompt`` to a model CLI on stdin and return its stdout. The one subprocess model send (131).

    The prompt goes on stdin, not argv: card text may hold user-authored issue bodies and
    comments, which stay out of process listings and argv length limits. A timeout or a launch
    failure comes back as ``error``, with a ``note`` for the caller's rationale. The log events keep
    the backends' existing names (e.g. ``prompt_cli_timeout``): alerts key on them.
    """
    label, limit = spec.label, spec.limit
    events = spec.events.format(purpose=purpose)
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            executable,
            *args,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(input=prompt.encode()), timeout=limit,
        )
    except TimeoutError:
        if proc is not None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(proc.wait(), timeout=2)
        log.warning(f"{events}_timeout", backend=label, timeout=limit)
        return ModelReply(error="timeout", timed_out=True, note=f"{label} timeout")
    except OSError as exc:
        log.error(f"{events}_launch_failed", backend=label, error=str(exc))
        return ModelReply(error=f"launch failed: {exc}", note=f"{label} error: {exc}")
    if proc.returncode and proc.returncode != 0:
        err = stderr.decode(errors="replace").strip() if stderr else ""
        log.warning(f"{events}_nonzero_exit", backend=label, returncode=proc.returncode, stderr_length=len(err))
    return ModelReply(text=stdout.decode(errors="replace").strip() if stdout else "")


class ClaudeCliBackend:
    """Runs `claude --print <prompt>` as a subprocess using local CLI auth."""

    _TIMEOUT: int = 30
    endpoint_name = "claude_cli"

    def __init__(self, executable: str = "claude", calls: ConductorCallLog | None = None) -> None:
        self._executable = executable
        self.calls = calls if calls is not None else NULL_CALL_LOG

    async def _send(self, prompt: str, purpose: str) -> ModelReply:
        return await _run_cli(
            self._executable, ["--print", "-"], prompt,
            spec=_CliSpec("cli", self._TIMEOUT, "{purpose}_cli"), purpose=purpose,
        )

    def _request(self, prompt: str) -> ModelRequest:
        # The CLI picks its own model and reports no token usage.
        return ModelRequest(model=self._executable, endpoint=self.endpoint_name, system="", prompt=prompt)

    async def assess(self, card: dict[str, Any]) -> dict[str, Any]:
        prompt = _build_assess_prompt(card)
        reply = await self.calls.invoke(self._request(prompt), lambda: self._send(prompt, "assessment"))
        if not reply.text:
            return {"sufficient": False, "questions": [], "rationale": reply.note or "empty cli response"}
        return _parse_assessment_response(reply.text)

    async def prompt(self, text: str, response_format: str | None = None) -> dict[str, Any]:
        """Send an arbitrary text prompt via the Claude CLI via stdin."""
        if not text.strip():
            return {"text": "", "data": None}
        reply = await self.calls.invoke(self._request(text), lambda: self._send(text, "prompt"))
        return _parse_prompt_response(reply.text, response_format)


class OpenCodeBackend:
    """Runs `opencode run --print <prompt>` as a subprocess.

    Uses the same opencode auth already configured for the performer.
    """

    _TIMEOUT: int = 60
    endpoint_name = "opencode_cli"

    def __init__(
        self,
        executable: str = "opencode",
        effort: str | None = None,
        calls: ConductorCallLog | None = None,
    ) -> None:
        self._executable = executable
        self._effort = effort
        self.calls = calls if calls is not None else NULL_CALL_LOG

    def _effort_args(self) -> list[str]:
        return ["--effort", self._effort] if self._effort else []

    async def _send(self, prompt: str, purpose: str) -> ModelReply:
        return await _run_cli(
            self._executable, ["run", *self._effort_args(), "-"], prompt,
            spec=_CliSpec("opencode", self._TIMEOUT, "{purpose}_opencode"), purpose=purpose,
        )

    def _request(self, prompt: str) -> ModelRequest:
        parameters: dict[str, Any] = {"effort": self._effort} if self._effort else {}
        return ModelRequest(
            model=self._executable, endpoint=self.endpoint_name, system="", prompt=prompt,
            parameters=parameters,
        )

    async def assess(self, card: dict[str, Any]) -> dict[str, Any]:
        prompt = _build_assess_prompt(card)
        reply = await self.calls.invoke(self._request(prompt), lambda: self._send(prompt, "assessment"))
        if not reply.text:
            return {"sufficient": False, "questions": [], "rationale": reply.note or "empty opencode response"}
        return _parse_assessment_response(reply.text)

    async def prompt(self, text: str, response_format: str | None = None) -> dict[str, Any]:
        """Send an arbitrary text prompt via the opencode CLI via stdin."""
        if not text.strip():
            return {"text": "", "data": None}
        reply = await self.calls.invoke(self._request(text), lambda: self._send(text, "prompt"))
        return _parse_prompt_response(reply.text, response_format)


class CodexCliBackend:
    """Runs `codex exec` as a subprocess. It pairs with the codex-backed performers.

    Uses the same codex CLI (and OPENAI_API_KEY) the performer fleet already
    relies on, so no new auth is required.
    """

    _TIMEOUT: int = 60
    endpoint_name = "codex_cli"

    def __init__(
        self,
        executable: str = "codex",
        model: str | None = None,
        effort: str | None = None,
        calls: ConductorCallLog | None = None,
    ) -> None:
        self._executable = executable
        self._model = model
        self._effort = effort
        self.calls = calls if calls is not None else NULL_CALL_LOG

    def _build_args(self) -> list[str]:
        # Order: subcommand → options → positional stdin sentinel.
        # `codex exec` parses `[PROMPT]` positionally; `-` means "read from
        # stdin." Flags must precede the positional so clap doesn't bind them
        # past the sentinel. The -c value is passed unquoted: codex parses it
        # as TOML and falls back to a literal string when TOML parse fails.
        args = [self._executable, "exec"]
        if self._model:
            args.extend(["--model", self._model])
        if self._effort:
            args.extend(["-c", f"model_reasoning_effort={self._effort}"])
        args.append("-")
        return args

    async def _send(self, prompt: str, purpose: str) -> ModelReply:
        executable, *args = self._build_args()
        return await _run_cli(
            executable, args, prompt, spec=_CliSpec("codex", self._TIMEOUT, "codex_cli"), purpose=purpose,
        )

    def _request(self, prompt: str) -> ModelRequest:
        parameters: dict[str, Any] = {"effort": self._effort} if self._effort else {}
        return ModelRequest(
            model=self._model or self._executable, endpoint=self.endpoint_name, system="",
            prompt=prompt, parameters=parameters,
        )

    async def assess(self, card: dict[str, Any]) -> dict[str, Any]:
        prompt = _build_assess_prompt(card)
        reply = await self.calls.invoke(self._request(prompt), lambda: self._send(prompt, "assessment"))
        if not reply.text:
            return {"sufficient": False, "questions": [], "rationale": "empty codex response"}
        return _parse_assessment_response(reply.text)

    async def prompt(self, text: str, response_format: str | None = None) -> dict[str, Any]:
        if not text.strip():
            return {"text": "", "data": None}
        reply = await self.calls.invoke(self._request(text), lambda: self._send(text, "prompt"))
        return _parse_prompt_response(reply.text, response_format)


class NullBackend:
    """Skips assessment — assumes all cards are sufficient."""

    async def assess(self, card: dict[str, Any]) -> dict[str, Any]:
        return {"sufficient": True, "questions": [], "rationale": "assessment disabled"}

    async def prompt(self, text: str, response_format: str | None = None) -> dict[str, Any]:
        """No-op prompt — returns empty text."""
        return {"text": "", "data": None}


def build_conducting_backend(
    config: ProjectConfiguration,
    circuit_breaker: Any = None,
    retry_kwargs: dict[str, Any] | None = None,
    calls: ConductorCallLog | None = None,
) -> ConductingBackend:
    """Factory: construct the configured conducting backend.

    Reads ``config.conducting``.  Model / max_tokens / temperature are threaded
    into each backend where the underlying API supports them; subprocess
    backends use ``executable`` overrides when provided.
    """
    import os

    a = config.conducting
    match a.backend:
        case "anthropic_api":
            from coordinare.services.claude import ClaudeService
            api_key_env = a.api_key_env or "ANTHROPIC_API_KEY"
            claude = ClaudeService(
                api_key=os.getenv(api_key_env),
                model=a.model or "claude-sonnet-4-20250514",
                max_tokens=a.max_tokens,
                temperature=a.temperature,
                circuit_breaker=circuit_breaker,
                retry_kwargs=retry_kwargs,
                base_url=a.base_url,
                calls=calls,
            )
            return AnthropicApiBackend(claude)
        case "openai_api":
            api_key_env = a.api_key_env or "OPENAI_API_KEY"
            return OpenAiApiBackend(
                api_key=os.getenv(api_key_env),
                model=a.model or "gpt-4o-mini",
                max_tokens=a.max_tokens,
                temperature=a.temperature,
                base_url=a.base_url,
                effort=a.effort,
                structured_transport=a.structured_transport,
                calls=calls,
            )
        case "claude_cli":
            return ClaudeCliBackend(executable=a.executable or "claude", calls=calls)
        case "codex_cli":
            return CodexCliBackend(
                executable=a.executable or "codex",
                model=a.model,
                effort=a.effort,
                calls=calls,
            )
        case "opencode":
            return OpenCodeBackend(
                executable=a.executable or "opencode",
                effort=a.effort,
                calls=calls,
            )
        case "none":
            return NullBackend()
        case _:
            msg = f"Unknown conducting backend: {a.backend!r}"
            raise ValueError(msg)


def build_model_endpoint_backend(
    config: ProjectConfiguration | None,
    name: str | None,
    calls: ConductorCallLog | None = None,
) -> AnthropicApiBackend | OpenAiApiBackend | None:
    """Resolve a named {model @ endpoint} pair (080) into a prompt backend.

    Returns None when the name is absent from the catalogs — callers treat that
    as "no observer", never as a failure (425 fail-safe).
    """
    import os

    if not name or config is None:
        return None
    me = config.resolve_model_endpoint(name)
    if me is None:
        return None
    ep = config.resolve_endpoint(me.endpoint)
    if ep is None:
        return None
    if ep.kind == "anthropic":
        from coordinare.services.claude import ClaudeService
        api_key_env = ep.auth_env or "ANTHROPIC_API_KEY"
        claude = ClaudeService(
            api_key=os.getenv(api_key_env),
            model=me.model,
            base_url=None,
            calls=calls,
            endpoint_name=name,
        )
        return AnthropicApiBackend(claude)
    api_key_env = ep.auth_env or "OPENAI_API_KEY"
    return OpenAiApiBackend(
        api_key=os.getenv(api_key_env),
        model=me.model,
        base_url=ep.base_url,
        calls=calls,
        endpoint_name=name,
    )
