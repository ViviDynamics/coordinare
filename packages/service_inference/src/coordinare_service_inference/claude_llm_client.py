"""Anthropic-SDK strategy implementation of ``LLMClient`` (spec 063 T026b).

This module is one strategy behind the :class:`LLMClient` facade defined in
``agent.py`` — it owns *every* Anthropic-specific concern (tool schema shape,
``messages.create`` arguments, response-block parsing, transient retries) so
that the agent loop stays vendor-agnostic. A future CLI-backed or alternate-
vendor strategy plugs in at the same seam without touching the agent.

Tool catalogue surfaced to the model:

* The six read-only sandbox operations (``read_file``, ``list_dir``, ``which``,
  ``probe_version``, ``grep_repo``, ``web_search``) — ``web_search`` is omitted
  when ``web_search_enabled=False`` to keep the model from requesting a tool
  the sandbox will reject.
* ``submit_manifest`` — a sentinel tool whose ``input_schema`` is the
  ``ServicesManifest`` JSON schema. When the model invokes it the strategy
  returns ``LLMStep(manifest=...)`` and the agent loop exits.
"""

from __future__ import annotations

from typing import Any, ClassVar, Protocol

import stamina
from anthropic import (
    APIConnectionError,
    APIStatusError,
    APITimeoutError,
    AsyncAnthropic,
    AuthenticationError,
)

from coordinare_service_inference.agent import LLMStep, ToolCall
from coordinare_service_inference.schema import manifest_json_schema


class TransientLLMError(RuntimeError):
    """Network/timeout/5xx/429 — safe for stamina to retry."""


class PermanentLLMError(RuntimeError):
    """Auth/4xx that won't be fixed by retrying."""


class _AnthropicMessagesAPI(Protocol):
    """Subset of the AsyncAnthropic surface this strategy actually uses.

    Declared explicitly so tests can inject a stub without importing the SDK.
    """

    messages: Any


SUBMIT_MANIFEST_TOOL = "submit_manifest"


def _build_tool_catalogue(web_search_enabled: bool) -> list[dict[str, Any]]:
    """Return Anthropic-shaped tool definitions for one ``messages.create`` call.

    Schemas mirror :class:`ToolSandbox`'s public method signatures. They are
    deliberately conservative — every argument is typed and ``additionalProperties``
    is false so the model cannot drift into unsupported kwargs.
    """
    catalogue: list[dict[str, Any]] = [
        {
            "name": "read_file",
            "description": (
                "Read a UTF-8 text file inside the project sandbox. "
                "Returns {exists, content, truncated}."
            ),
            "input_schema": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
                "additionalProperties": False,
            },
        },
        {
            "name": "list_dir",
            "description": "List entries of a directory inside the sandbox.",
            "input_schema": {
                "type": "object",
                "properties": {"path": {"type": "string", "default": "."}},
                "additionalProperties": False,
            },
        },
        {
            "name": "which",
            "description": "Locate a binary on PATH inside the performer image.",
            "input_schema": {
                "type": "object",
                "properties": {"binary": {"type": "string"}},
                "required": ["binary"],
                "additionalProperties": False,
            },
        },
        {
            "name": "probe_version",
            "description": (
                "Resolve a binary on PATH then invoke `--version` or `-v`. "
                "Returns {found, version}."
            ),
            "input_schema": {
                "type": "object",
                "properties": {"binary": {"type": "string"}},
                "required": ["binary"],
                "additionalProperties": False,
            },
        },
        {
            "name": "grep_repo",
            "description": (
                "Regex-search for a pattern within the sandbox. Results are "
                "capped server-side."
            ),
            "input_schema": {
                "type": "object",
                "properties": {
                    "pattern": {"type": "string"},
                    "path": {"type": "string", "default": "."},
                },
                "required": ["pattern"],
                "additionalProperties": False,
            },
        },
        {
            "name": SUBMIT_MANIFEST_TOOL,
            "description": (
                "Emit the final ServicesManifest. Call this exactly once when "
                "you are confident every supportive service has been "
                "identified. Do not call other tools in the same turn."
            ),
            "input_schema": manifest_json_schema(),
        },
    ]
    if web_search_enabled:
        catalogue.insert(
            -1,
            {
                "name": "web_search",
                "description": "Search the web for service documentation.",
                "input_schema": {
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                    "additionalProperties": False,
                },
            },
        )
    return catalogue


class ClaudeServiceLLMClient:
    """Anthropic strategy for the service-inference :class:`LLMClient` facade.

    The agent calls ``step(messages)`` after every tool-result batch. This
    strategy:

    1. Issues a single ``messages.create`` with the running history, the
       system prompt fixed at construction, and the tool catalogue.
    2. Parses ``tool_use`` content blocks into :class:`ToolCall` values.
    3. Intercepts ``submit_manifest`` and routes it to ``LLMStep.manifest`` so
       the agent loop terminates.
    4. Captures per-step token usage on the returned ``LLMStep`` so the
       orchestrator can sum it for cost telemetry.
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
        anthropic_client: _AnthropicMessagesAPI,
        model: str,
        max_tokens: int,
        system_prompt: str,
        web_search_enabled: bool = False,
        temperature: float | None = None,
        retry_kwargs: dict[str, Any] | None = None,
    ) -> None:
        self._client = anthropic_client
        self._model = model
        self._max_tokens = max_tokens
        self._system_prompt = system_prompt
        self._temperature = temperature
        self._tools = _build_tool_catalogue(web_search_enabled)
        self._retry_kwargs = (
            retry_kwargs if retry_kwargs is not None else dict(self._DEFAULT_RETRY_KWARGS)
        )

    @classmethod
    def from_api_key(
        cls,
        *,
        api_key: str | None,
        model: str,
        max_tokens: int,
        system_prompt: str,
        web_search_enabled: bool = False,
        temperature: float | None = None,
        retry_kwargs: dict[str, Any] | None = None,
    ) -> ClaudeServiceLLMClient:
        """Convenience constructor used by production wiring.

        Stamina handles retries on the call site, so we disable the SDK's own
        ``max_retries`` to avoid double-counting attempts.
        """
        return cls(
            anthropic_client=AsyncAnthropic(api_key=api_key, max_retries=0),
            model=model,
            max_tokens=max_tokens,
            system_prompt=system_prompt,
            web_search_enabled=web_search_enabled,
            temperature=temperature,
            retry_kwargs=retry_kwargs,
        )

    async def step(self, messages: list[dict[str, Any]]) -> LLMStep:
        kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "system": self._system_prompt,
            "messages": messages,
            "tools": self._tools,
        }
        if self._temperature is not None:
            kwargs["temperature"] = self._temperature

        @stamina.retry(on=TransientLLMError, **self._retry_kwargs)
        async def _call() -> Any:
            try:
                return await self._client.messages.create(**kwargs)
            except (APIConnectionError, APITimeoutError) as exc:
                raise TransientLLMError(str(exc)) from exc
            except AuthenticationError as exc:
                raise PermanentLLMError(str(exc)) from exc
            except APIStatusError as exc:
                if exc.status_code >= 500 or exc.status_code == 429:
                    raise TransientLLMError(str(exc)) from exc
                raise PermanentLLMError(str(exc)) from exc

        response = await _call()
        return _parse_response(response)


def _parse_response(response: Any) -> LLMStep:
    """Translate one Anthropic response into an :class:`LLMStep`.

    Per the contract the strategy enforces, ``submit_manifest`` always wins:
    if the model emitted it alongside other tool calls (which it shouldn't),
    we treat the run as finished and ignore the rest. Mixed turns indicate a
    confused model and the manifest is what the agent loop needs to exit.
    """
    usage = getattr(response, "usage", None)
    input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
    output_tokens = int(getattr(usage, "output_tokens", 0) or 0)

    tool_calls: list[ToolCall] = []
    manifest: dict[str, Any] | None = None
    for block in getattr(response, "content", []) or []:
        if getattr(block, "type", None) != "tool_use":
            continue
        name = getattr(block, "name", "")
        block_input = getattr(block, "input", {}) or {}
        if name == SUBMIT_MANIFEST_TOOL:
            manifest = dict(block_input)
            break
        tool_calls.append(
            ToolCall(
                id=str(getattr(block, "id", "")),
                name=name,
                arguments=dict(block_input),
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
