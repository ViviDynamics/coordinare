"""OpenAI-compatible HTTP strategy for the service-inference ``LLMClient``.

Targets any server that implements the OpenAI ``/v1/chat/completions`` API
with tool-use — LiteLLM proxy, vLLM, Ollama-via-LiteLLM, LM Studio, llama.cpp
server, TGI, etc. Lets operators route env_bootstrap inference at their own
hardware instead of phoning Anthropic.

The agent (``agent.py``) builds the running message history in Anthropic's
block-shape (``role: assistant`` with ``content=[{type:tool_use,...}]`` and
``role: user`` with ``content=[{type:tool_result,...}]``). This module
translates that shape into OpenAI's flat ``tool_calls`` + ``role:tool``
shape on the way out, and translates the response back into the shared
``LLMStep`` / ``ToolCall`` types on the way in. The agent itself stays
strategy-agnostic.

Retry policy matches ``ClaudeServiceLLMClient`` — transient (connection,
timeout, 5xx, 429) retries via stamina; auth/4xx raises
``PermanentLLMError`` immediately.
"""

from __future__ import annotations

import json
from typing import Any, ClassVar

import httpx
import stamina

from coordinare_service_inference.agent import LLMStep, ToolCall
from coordinare_service_inference.claude_llm_client import (
    SUBMIT_MANIFEST_TOOL,
    PermanentLLMError,
    TransientLLMError,
    _build_tool_catalogue,
)


def _anthropic_tools_to_openai(tools: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Translate Anthropic ``tools`` shape to OpenAI's ``tools`` shape.

    Anthropic: ``{name, description, input_schema}``.
    OpenAI:    ``{type: "function", function: {name, description, parameters}}``.

    ``input_schema`` and ``parameters`` are both JSON Schema, so the schema
    body itself round-trips unchanged.
    """
    out: list[dict[str, Any]] = []
    for t in tools:
        out.append(
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t.get("description", ""),
                    "parameters": t["input_schema"],
                },
            }
        )
    return out


def _anthropic_messages_to_openai(
    messages: list[dict[str, Any]], system_prompt: str
) -> list[dict[str, Any]]:
    """Translate the agent's Anthropic-shaped history into OpenAI chat messages.

    The agent emits exactly two block shapes:

    * Assistant turns with ``content`` = list of ``tool_use`` blocks.
    * User turns that are either a plain string (the initial prompt) or a
      list of ``tool_result`` blocks.

    OpenAI wants:
    * Assistant: ``{role: assistant, content: <str|null>, tool_calls: [...]}``
    * Tool result: one ``{role: tool, tool_call_id, content}`` message per
      call (not a single bundled user turn like Anthropic).
    * System prompt comes through as a first ``{role: system}`` message.
    """
    out: list[dict[str, Any]] = []
    if system_prompt:
        out.append({"role": "system", "content": system_prompt})

    for msg in messages:
        role = msg.get("role")
        content = msg.get("content")
        if role == "user" and isinstance(content, str):
            out.append({"role": "user", "content": content})
            continue
        if role == "user" and isinstance(content, list):
            # Tool-result batch. Anthropic packs them all into one user turn;
            # OpenAI wants one role:tool message per call_id.
            for block in content:
                if block.get("type") != "tool_result":
                    continue
                payload = block.get("content")
                if not isinstance(payload, str):
                    payload = json.dumps(payload)
                out.append(
                    {
                        "role": "tool",
                        "tool_call_id": str(block.get("tool_use_id", "")),
                        "content": payload,
                    }
                )
            continue
        if role == "assistant" and isinstance(content, list):
            tool_calls: list[dict[str, Any]] = []
            text_parts: list[str] = []
            for block in content:
                btype = block.get("type")
                if btype == "tool_use":
                    tool_calls.append(
                        {
                            "id": str(block.get("id", "")),
                            "type": "function",
                            "function": {
                                "name": block.get("name", ""),
                                "arguments": json.dumps(block.get("input", {})),
                            },
                        }
                    )
                elif btype == "text":
                    text_parts.append(str(block.get("text", "")))
            assistant_msg: dict[str, Any] = {"role": "assistant"}
            assistant_msg["content"] = "\n".join(text_parts) if text_parts else None
            if tool_calls:
                assistant_msg["tool_calls"] = tool_calls
            out.append(assistant_msg)
            continue
        # Unknown shape — pass through; the server can reject if it dislikes it.
        out.append(msg)
    return out


# Keys some OpenAI-compatible models (notably Qwen via LiteLLM) wrap the real
# tool arguments inside. The model emits ``{"parameter": {<real args>}}`` instead
# of ``{<real args>}``. We unwrap a single layer when (and only when) the dict
# has exactly one top-level key from this set and the value is itself a dict —
# anything else is a legitimate payload and we leave it alone.
_ARG_ENVELOPE_KEYS: frozenset[str] = frozenset({"parameter", "parameters", "arguments"})


def _unwrap_arg_envelope(args: dict[str, Any]) -> dict[str, Any]:
    if len(args) != 1:
        return args
    only_key = next(iter(args))
    if only_key not in _ARG_ENVELOPE_KEYS:
        return args
    inner = args[only_key]
    if not isinstance(inner, dict):
        return args
    return inner


def _parse_openai_response(payload: dict[str, Any]) -> LLMStep:
    """Translate one OpenAI chat-completion response into an ``LLMStep``.

    Mirrors the ``submit_manifest``-wins contract from the Anthropic strategy
    — if the model emits a manifest alongside other calls, the manifest
    terminates the loop and the other calls are dropped.
    """
    usage = payload.get("usage") or {}
    input_tokens = int(usage.get("prompt_tokens", 0) or 0)
    output_tokens = int(usage.get("completion_tokens", 0) or 0)

    choices = payload.get("choices") or []
    if not choices:
        return LLMStep(input_tokens=input_tokens, output_tokens=output_tokens)
    message = choices[0].get("message") or {}
    raw_calls = message.get("tool_calls") or []

    tool_calls: list[ToolCall] = []
    manifest: dict[str, Any] | None = None
    for call in raw_calls:
        if call.get("type") != "function":
            continue
        fn = call.get("function") or {}
        name = fn.get("name", "")
        args_raw = fn.get("arguments", "{}")
        try:
            args = json.loads(args_raw) if isinstance(args_raw, str) else dict(args_raw or {})
        except json.JSONDecodeError:
            # A malformed tool-call payload from the server is the model's
            # problem, not ours — surface it as an empty-args call and let
            # the sandbox's argument validation reject it with a precise error.
            args = {}
        args = _unwrap_arg_envelope(args)
        if name == SUBMIT_MANIFEST_TOOL:
            manifest = args
            break
        tool_calls.append(
            ToolCall(
                id=str(call.get("id", "")),
                name=name,
                arguments=args,
            )
        )

    if manifest is not None:
        return LLMStep(
            manifest=manifest,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
        )
    return LLMStep(
        tool_calls=tool_calls,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
    )


class OpenAICompatServiceLLMClient:
    """OpenAI-compatible HTTP strategy for ``LLMClient``.

    Issues one ``POST /chat/completions`` per ``step()`` call against
    ``base_url``. Designed for a LiteLLM proxy in front of vLLM/Ollama on
    the operator's own hardware, but works with any server that follows the
    OpenAI tools API.
    """

    _DEFAULT_RETRY_KWARGS: ClassVar[dict[str, Any]] = {
        "attempts": 3,
        "wait_initial": 0.5,
        "wait_max": 30.0,
        "wait_jitter": 0.5,
        "wait_exp_base": 2.0,
    }

    def __init__(
        self,
        *,
        http_client: httpx.AsyncClient,
        base_url: str,
        model: str,
        max_tokens: int,
        system_prompt: str,
        api_key: str | None = None,
        web_search_enabled: bool = False,
        temperature: float | None = None,
        retry_kwargs: dict[str, Any] | None = None,
    ) -> None:
        self._client = http_client
        # Strip trailing slash so we can safely append "/chat/completions".
        self._base_url = base_url.rstrip("/")
        self._model = model
        self._max_tokens = max_tokens
        self._system_prompt = system_prompt
        self._api_key = api_key
        self._temperature = temperature
        # Reuse the existing tool catalogue so behavior is identical to the
        # Anthropic strategy — only the wire format differs.
        self._tools_openai = _anthropic_tools_to_openai(
            _build_tool_catalogue(web_search_enabled)
        )
        self._retry_kwargs = (
            retry_kwargs if retry_kwargs is not None else dict(self._DEFAULT_RETRY_KWARGS)
        )

    @classmethod
    def from_config(
        cls,
        *,
        base_url: str,
        api_key: str | None,
        model: str,
        max_tokens: int,
        system_prompt: str,
        web_search_enabled: bool = False,
        temperature: float | None = None,
        timeout_seconds: float = 600.0,
        retry_kwargs: dict[str, Any] | None = None,
    ) -> OpenAICompatServiceLLMClient:
        """Construct with a fresh ``httpx.AsyncClient``.

        Long timeout because the agent loop runs many tool turns and some
        on-prem models (especially CPU-Ollama) can be slow — stamina handles
        retries around connection failures, not deliberate slow responses.
        """
        return cls(
            http_client=httpx.AsyncClient(timeout=timeout_seconds),
            base_url=base_url,
            model=model,
            max_tokens=max_tokens,
            system_prompt=system_prompt,
            api_key=api_key,
            web_search_enabled=web_search_enabled,
            temperature=temperature,
            retry_kwargs=retry_kwargs,
        )

    async def step(self, messages: list[dict[str, Any]]) -> LLMStep:
        headers: dict[str, str] = {"content-type": "application/json"}
        if self._api_key:
            headers["authorization"] = f"Bearer {self._api_key}"

        body: dict[str, Any] = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "messages": _anthropic_messages_to_openai(messages, self._system_prompt),
            "tools": self._tools_openai,
        }
        if self._temperature is not None:
            body["temperature"] = self._temperature

        url = f"{self._base_url}/chat/completions"

        @stamina.retry(on=TransientLLMError, **self._retry_kwargs)
        async def _call() -> dict[str, Any]:
            try:
                response = await self._client.post(url, headers=headers, json=body)
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout) as exc:
                raise TransientLLMError(str(exc)) from exc
            status = response.status_code
            if status >= 500 or status == 429:
                raise TransientLLMError(f"upstream {status}: {response.text[:200]}")
            if status == 401 or status == 403:
                raise PermanentLLMError(f"auth failed ({status}): {response.text[:200]}")
            if status >= 400:
                raise PermanentLLMError(f"upstream {status}: {response.text[:200]}")
            try:
                return response.json()
            except ValueError as exc:
                # A 2xx with non-JSON body is the proxy misbehaving; safe to retry.
                raise TransientLLMError(f"non-JSON response: {exc}") from exc

        payload = await _call()
        return _parse_openai_response(payload)
