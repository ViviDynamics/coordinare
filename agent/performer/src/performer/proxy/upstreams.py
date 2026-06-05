"""080 — upstream model clients.

An ``Upstream`` is a client for one resolved model_endpoint: it renders an
``LLMRequest`` into its endpoint's wire format, calls the endpoint, and parses
the response back into an ``LLMResponse``. The orchestration strategies depend
only on the ``Upstream`` protocol, so they are unit-testable with fakes.

``HttpUpstream`` is the httpx-backed implementation. It is timeout-bounded so a
hung upstream surfaces (``UpstreamError``) rather than black-holing the job
(FR-018). Non-streaming JSON only — the proxy runs the think phase internally
and assembles/streams to the CLI in the ``assembler`` layer.

The render/parse functions are module-level and pure, so they test without a
network. Two wire formats are supported (FR-010): ``openai`` (chat-completions)
and ``anthropic`` (messages); LiteLLM normalizes most self-hosted models to the
openai shape.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Literal, Protocol, runtime_checkable

import httpx

from performer.proxy.llm_turn import (
    LLMRequest,
    LLMResponse,
    Message,
    ToolCall,
    ToolSchema,
)

WireFormat = Literal["openai", "anthropic"]
AuthStyle = Literal["bearer", "x-api-key"]

_DEFAULT_BASE = {
    "openai": "https://api.openai.com/v1",
    "anthropic": "https://api.anthropic.com/v1",
}
_ANTHROPIC_VERSION = "2023-06-01"
_DEFAULT_TIMEOUT_S = 600.0
_DEFAULT_MAX_TOKENS = 4096  # anthropic requires max_tokens; harmless default


class UpstreamError(RuntimeError):
    """Raised when an upstream call fails or times out."""


@runtime_checkable
class Upstream(Protocol):
    """A model endpoint the proxy can call."""

    name: str

    async def complete(self, request: LLMRequest, *, tools_enabled: bool = True) -> LLMResponse:
        """Run a single completion.

        ``tools_enabled=False`` hides tool schemas (the think/planner phase).
        Raises ``UpstreamError`` on failure/timeout.
        """
        ...


# --- request rendering -----------------------------------------------------


def _tool_to_openai(t: ToolSchema) -> dict[str, Any]:
    return {
        "type": "function",
        "function": {"name": t.name, "description": t.description, "parameters": t.parameters},
    }


def _msg_to_openai(m: Message) -> dict[str, Any]:
    out: dict[str, Any] = {"role": m.role, "content": m.content}
    if m.role == "tool" and m.tool_call_id:
        out["tool_call_id"] = m.tool_call_id
    if m.tool_calls:
        out["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)},
            }
            for tc in m.tool_calls
        ]
    return out


def render_openai(request: LLMRequest, model: str, *, tools_enabled: bool) -> dict[str, Any]:
    body: dict[str, Any] = {
        "model": model,
        "messages": [_msg_to_openai(m) for m in request.messages],
    }
    if tools_enabled and request.tools:
        body["tools"] = [_tool_to_openai(t) for t in request.tools]
    if request.stream:
        body["stream"] = True
    return body


def render_anthropic(request: LLMRequest, model: str, *, tools_enabled: bool) -> dict[str, Any]:
    # Anthropic carries system text as a top-level field, not a message.
    system_parts = [m.content for m in request.messages if m.role == "system" and m.content]
    msgs: list[dict[str, Any]] = []
    for m in request.messages:
        if m.role == "system":
            continue
        if m.role == "tool":
            msgs.append({
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": m.tool_call_id or "", "content": m.content}
                ],
            })
            continue
        if m.tool_calls:
            content: list[dict[str, Any]] = []
            if m.content:
                content.append({"type": "text", "text": m.content})
            content.extend(
                {"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.arguments}
                for tc in m.tool_calls
            )
            msgs.append({"role": m.role, "content": content})
        else:
            msgs.append({"role": m.role, "content": m.content})
    body: dict[str, Any] = {"model": model, "messages": msgs, "max_tokens": _DEFAULT_MAX_TOKENS}
    if system_parts:
        body["system"] = "\n\n".join(system_parts)
    if tools_enabled and request.tools:
        body["tools"] = [
            {"name": t.name, "description": t.description, "input_schema": t.parameters}
            for t in request.tools
        ]
    if request.stream:
        body["stream"] = True
    return body


# --- inbound request parsing (CLI body -> LLMRequest) ----------------------


def to_llm_request_openai(body: dict[str, Any]) -> LLMRequest:
    """Parse an OpenAI chat-completions request body into an LLMRequest."""
    messages = []
    for m in body.get("messages") or []:
        tcs = tuple(
            ToolCall(
                id=tc.get("id", ""),
                name=(tc.get("function") or {}).get("name", ""),
                arguments=_loads((tc.get("function") or {}).get("arguments")),
            )
            for tc in (m.get("tool_calls") or [])
        )
        messages.append(
            Message(
                role=m.get("role", "user"),
                content=m.get("content") or "" if isinstance(m.get("content"), str) else "",
                tool_calls=tcs,
                tool_call_id=m.get("tool_call_id"),
            )
        )
    tools = tuple(
        ToolSchema(
            name=(t.get("function") or {}).get("name", ""),
            description=(t.get("function") or {}).get("description", ""),
            parameters=(t.get("function") or {}).get("parameters") or {},
        )
        for t in (body.get("tools") or [])
    )
    return LLMRequest(messages=tuple(messages), tools=tools, stream=bool(body.get("stream")))


def to_llm_request_anthropic(body: dict[str, Any]) -> LLMRequest:
    """Parse an Anthropic messages request body into an LLMRequest."""
    messages: list[Message] = []
    system = body.get("system")
    if isinstance(system, str) and system:
        messages.append(Message.system(system))
    for m in body.get("messages") or []:
        role = m.get("role", "user")
        content = m.get("content")
        if isinstance(content, str):
            messages.append(Message(role=role, content=content))
            continue
        text_parts, tool_calls = [], []
        for blk in content or []:
            bt = blk.get("type")
            if bt == "text":
                text_parts.append(blk.get("text", ""))
            elif bt == "tool_use":
                tool_calls.append(
                    ToolCall(id=blk.get("id", ""), name=blk.get("name", ""), arguments=blk.get("input") or {})
                )
            elif bt == "tool_result":
                messages.append(
                    Message(
                        role="tool",
                        content=_block_text(blk.get("content")),
                        tool_call_id=blk.get("tool_use_id"),
                        _is_error=bool(blk.get("is_error")),
                    )
                )
        if text_parts or tool_calls:
            messages.append(Message(role=role, content="".join(text_parts), tool_calls=tuple(tool_calls)))
    tools = tuple(
        ToolSchema(name=t.get("name", ""), description=t.get("description", ""), parameters=t.get("input_schema") or {})
        for t in (body.get("tools") or [])
    )
    return LLMRequest(messages=tuple(messages), tools=tools, stream=bool(body.get("stream")))


def _block_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content if isinstance(b, dict))
    return ""


# --- response parsing ------------------------------------------------------


def parse_openai(body: dict[str, Any]) -> LLMResponse:
    choices = body.get("choices") or [{}]
    msg = (choices[0] or {}).get("message") or {}
    tool_calls = tuple(
        ToolCall(
            id=tc.get("id", ""),
            name=(tc.get("function") or {}).get("name", ""),
            arguments=_loads((tc.get("function") or {}).get("arguments")),
        )
        for tc in (msg.get("tool_calls") or [])
    )
    return LLMResponse(
        content=msg.get("content") or "",
        tool_calls=tool_calls,
        reasoning=msg.get("reasoning_content") or None,
        raw=body,
    )


def parse_anthropic(body: dict[str, Any]) -> LLMResponse:
    blocks = body.get("content") or []
    text_parts: list[str] = []
    reasoning_parts: list[str] = []
    tool_calls: list[ToolCall] = []
    for b in blocks:
        btype = b.get("type")
        if btype == "text":
            text_parts.append(b.get("text", ""))
        elif btype == "thinking":
            reasoning_parts.append(b.get("thinking", ""))
        elif btype == "tool_use":
            tool_calls.append(
                ToolCall(id=b.get("id", ""), name=b.get("name", ""), arguments=b.get("input") or {})
            )
    return LLMResponse(
        content="".join(text_parts),
        tool_calls=tuple(tool_calls),
        reasoning="".join(reasoning_parts) or None,
        raw=body,
    )


def _loads(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        val = json.loads(raw)
        return val if isinstance(val, dict) else {"_": val}
    except (json.JSONDecodeError, TypeError):
        return {}


# --- httpx client ----------------------------------------------------------


@dataclass
class HttpUpstream:
    """httpx-backed Upstream for one model_endpoint."""

    name: str
    model: str
    wire_format: WireFormat = "openai"
    base_url: str | None = None
    auth_token: str | None = None
    auth_style: AuthStyle = "bearer"
    timeout_s: float = _DEFAULT_TIMEOUT_S
    client: httpx.AsyncClient | None = None

    def _base(self) -> str:
        return (self.base_url or _DEFAULT_BASE[self.wire_format]).rstrip("/")

    def _path(self) -> str:
        return "/messages" if self.wire_format == "anthropic" else "/chat/completions"

    def _headers(self) -> dict[str, str]:
        h: dict[str, str] = {"content-type": "application/json"}
        if self.wire_format == "anthropic":
            h["anthropic-version"] = _ANTHROPIC_VERSION
        if self.auth_token:
            if self.auth_style == "x-api-key":
                h["x-api-key"] = self.auth_token
            else:
                h["authorization"] = f"Bearer {self.auth_token}"
        return h

    async def complete(self, request: LLMRequest, *, tools_enabled: bool = True) -> LLMResponse:
        if self.wire_format == "anthropic":
            body = render_anthropic(request, self.model, tools_enabled=tools_enabled)
            parse = parse_anthropic
        else:
            body = render_openai(request, self.model, tools_enabled=tools_enabled)
            parse = parse_openai
        # think phase is always non-streaming internally
        body.pop("stream", None)
        url = self._base() + self._path()
        client = self.client or httpx.AsyncClient(timeout=self.timeout_s)
        try:
            # pass timeout per-request so an injected/shared client (whose own
            # default may be httpx's 5s) still honors this upstream's bound.
            resp = await client.post(url, json=body, headers=self._headers(), timeout=self.timeout_s)
            resp.raise_for_status()
            return parse(resp.json())
        except httpx.HTTPError as exc:
            raise UpstreamError(f"upstream '{self.name}' call failed: {type(exc).__name__}") from exc
        finally:
            if self.client is None:
                await client.aclose()
