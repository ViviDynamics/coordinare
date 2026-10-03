"""080 — upstream model clients.

An ``Upstream`` is a client for one resolved model_endpoint: it renders an
``LLMRequest`` into its endpoint's wire format, calls the endpoint, and parses
the response back into an ``LLMResponse``. The orchestration strategies depend
only on the ``Upstream`` protocol, so they are unit-testable with fakes.

``HttpUpstream`` is the httpx-backed implementation. It is timeout-bounded so a
hung upstream surfaces (``UpstreamError``) rather than black-holing the job
(FR-018). ``complete`` is non-streaming JSON (the think phase runs non-streamed
internally); ``complete_stream`` (FR-014) consumes an upstream SSE body
incrementally, translating each chunk into a canonical :class:`StreamDelta` and
accumulating the same ``LLMResponse`` ``complete`` would have returned.

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
    StreamDelta,
    ToolCall,
    ToolSchema,
)
from performer.proxy.normalizers import NORMALIZER_REGISTRY
from performer.proxy.normalizers.base import StatefulSSEFilter

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


def _upstream_error(name: str, exc: httpx.HTTPError) -> UpstreamError:
    return UpstreamError(f"upstream '{name}' call failed: {type(exc).__name__}")


def _normalize_raw(normalizers: Any, raw: bytes) -> bytes:
    for key in normalizers:
        normalize_raw = getattr(NORMALIZER_REGISTRY[key], "normalize_raw", None)
        if normalize_raw is not None:
            raw = normalize_raw(raw)
    return raw


def _normalize_json(normalizers: Any, payload: dict[str, Any]) -> dict[str, Any]:
    for key in normalizers:
        payload = NORMALIZER_REGISTRY[key].normalize_json(payload)
    return payload


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


class ActDeltaSink(Protocol):
    """Front-door writer hooks the streaming act phase pushes into (FR-014).

    ``act`` calls ``start(plan)`` once before the upstream call, then
    ``delta(...)`` per canonical fragment; the writer renders each fragment as
    wire-correct SSE blocks as it arrives. ``finish`` is implicit: strategies
    return the accumulated ``LLMResponse`` and the shell renders the closing
    blocks from it.
    """

    def start(self, plan: str | None) -> None: ...

    def delta(self, delta: StreamDelta) -> None: ...


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
                    {"type": "tool_result", "tool_use_id": m.tool_call_id or "", "content": m.content},
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


def _generation_parameters(body: dict[str, Any]) -> dict[str, Any]:
    out = {key: body[key] for key in ("temperature", "top_p", "stop", "seed", "response_format") if key in body}
    for key in ("max_completion_tokens", "max_output_tokens", "max_tokens"):
        if key in body:
            out["max_tokens"] = body[key]
            break
    return out



def _openai_content_text(content: Any) -> str:
    """Plain text from an OpenAI chat-completions message ``content`` — either a
    string, or a list of content-parts (``[{"type": "text", "text": "..."}]``).

    Agent SDKs (e.g. pi, which stores the user turn as ``content: [{type: text,
    text}]`` internally and serializes it verbatim) commonly send the array form.
    Dropping it to "" empties the user task and the exec model replies with a bare
    greeting — the 082 dual-mode greeting root cause."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content if isinstance(b, dict) and b.get("type") == "text")
    return ""


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
                content=_openai_content_text(m.get("content")),
                tool_calls=tcs,
                tool_call_id=m.get("tool_call_id"),
            ),
        )
    tools = tuple(
        ToolSchema(
            name=(t.get("function") or {}).get("name", ""),
            description=(t.get("function") or {}).get("description", ""),
            parameters=(t.get("function") or {}).get("parameters") or {},
        )
        for t in (body.get("tools") or [])
    )
    return LLMRequest(messages=tuple(messages), tools=tools, stream=bool(body.get("stream")),
                      generation=_generation_parameters(body))


def to_llm_request_anthropic(body: dict[str, Any]) -> LLMRequest:
    """Parse an Anthropic messages request body into an LLMRequest."""
    messages: list[Message] = []
    system = body.get("system")
    system_text = _openai_content_text(system)
    if system_text:
        messages.append(Message.system(system_text))
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
                    ToolCall(id=blk.get("id", ""), name=blk.get("name", ""), arguments=blk.get("input") or {}),
                )
            elif bt == "tool_result":
                messages.append(
                    Message(
                        role="tool",
                        content=_block_text(blk.get("content")),
                        tool_call_id=blk.get("tool_use_id"),
                        _is_error=bool(blk.get("is_error")),
                    ),
                )
        if text_parts or tool_calls:
            messages.append(Message(role=role, content="".join(text_parts), tool_calls=tuple(tool_calls)))
    tools = tuple(
        ToolSchema(name=t.get("name", ""), description=t.get("description", ""), parameters=t.get("input_schema") or {})
        for t in (body.get("tools") or [])
    )
    return LLMRequest(messages=tuple(messages), tools=tools, stream=bool(body.get("stream")),
                      generation=_generation_parameters(body))


def _responses_content_text(content: Any) -> str:
    """Plain text from a Responses message ``content`` — a string, or a list of
    ``input_text``/``output_text`` blocks."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content if isinstance(b, dict))
    return ""


def _responses_input_item_to_messages(item: dict[str, Any]) -> list[Message]:
    """Convert one Responses ``input`` array item into canonical Message(s)."""
    itype = item.get("type", "message")
    if itype == "message":
        return [Message(role=item.get("role", "user"), content=_responses_content_text(item.get("content")))]
    if itype == "function_call":
        return [
            Message(
                role="assistant",
                content="",
                tool_calls=(
                    ToolCall(
                        id=item.get("call_id", ""),
                        name=item.get("name", ""),
                        arguments=_loads(item.get("arguments")),
                    ),
                ),
            ),
        ]
    if itype == "function_call_output":
        return [
            Message(
                role="tool",
                content=_block_text(item.get("output")),
                tool_call_id=item.get("call_id"),
            ),
        ]
    return []


def to_llm_request_responses(body: dict[str, Any]) -> LLMRequest:
    """Parse an OpenAI Responses-API request body into an LLMRequest.

    codex 0.137.0 is hard-locked to this wire at request time (it POSTs to
    ``/responses`` regardless of provider ``wire_api`` config), so the proxy must
    accept it as a third front-door format alongside openai/anthropic. Shape:
    ``instructions`` (system text), ``input`` (string OR an array of message /
    function_call / function_call_output items), flat ``tools``
    (``{type, name, description, parameters}`` — not nested under ``function``).
    """
    messages: list[Message] = []
    instructions = body.get("instructions")
    if isinstance(instructions, str) and instructions:
        messages.append(Message.system(instructions))
    inp = body.get("input")
    if isinstance(inp, str):
        messages.append(Message.user(inp))
    elif isinstance(inp, list):
        for item in inp:
            if isinstance(item, dict):
                messages.extend(_responses_input_item_to_messages(item))
    tools = tuple(
        ToolSchema(
            name=t.get("name", ""),
            description=t.get("description", ""),
            parameters=t.get("parameters") or {},
        )
        for t in (body.get("tools") or [])
        if t.get("type") == "function"
    )
    return LLMRequest(messages=tuple(messages), tools=tools, stream=bool(body.get("stream")),
                      generation=_generation_parameters(body))


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
                ToolCall(id=b.get("id", ""), name=b.get("name", ""), arguments=b.get("input") or {}),
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
    reasoning_policy: Literal["disable_thinking"] | None = None
    normalizers: tuple[str, ...] = ()
    preserve_generation: bool = False

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

    def _render_body(self, request: LLMRequest, *, tools_enabled: bool, streaming: bool) -> dict[str, Any]:
        if self.wire_format == "anthropic":
            body = render_anthropic(request, self.model, tools_enabled=tools_enabled)
        else:
            body = render_openai(request, self.model, tools_enabled=tools_enabled)
        if self.preserve_generation:
            body.update(request.generation)
        if self.reasoning_policy is not None:
            if self.reasoning_policy != "disable_thinking" or self.wire_format != "openai":
                raise UpstreamError("reasoning policy requires OpenAI-compatible self-hosted requests")
            if self.model.rsplit("/", 1)[-1].lower() == "glm-5.3-flash":
                raise UpstreamError("disable_thinking is measured harmful for glm-5.3-flash")
            body.update(request.generation)
            body["chat_template_kwargs"] = {"enable_thinking": False}
        if streaming:
            body["stream"] = True
        else:
            # think phase is always non-streaming internally
            body.pop("stream", None)
        return body

    async def complete(self, request: LLMRequest, *, tools_enabled: bool = True) -> LLMResponse:
        body = self._render_body(request, tools_enabled=tools_enabled, streaming=False)
        url = self._base() + self._path()
        client = self.client or httpx.AsyncClient(timeout=self.timeout_s)
        try:
            # pass timeout per-request so an injected/shared client (whose own
            # default may be httpx's 5s) still honors this upstream's bound.
            resp = await client.post(url, json=body, headers=self._headers(), timeout=self.timeout_s)
            resp.raise_for_status()
            raw = resp.content
            if self.normalizers:
                raw = _normalize_raw(self.normalizers, raw)
            payload = json.loads(raw) if self.normalizers else resp.json()
            if self.normalizers:
                payload = _normalize_json(self.normalizers, payload)
            return parse_payload(self.wire_format, payload)
        except httpx.HTTPError as exc:
            raise _upstream_error(self.name, exc) from exc
        finally:
            if self.client is None:
                await client.aclose()

    async def complete_stream(
        self, request: LLMRequest, sink: Any, *, tools_enabled: bool = True,
    ) -> LLMResponse:
        """Streaming variant of ``complete`` (FR-014).

        Renders the request with ``stream: true``, consumes the upstream SSE
        body incrementally, and pushes one canonical :class:`StreamDelta` to
        ``sink.delta(...)`` per upstream fragment. Returns the fully-accumulated
        ``LLMResponse`` — byte-equivalent to what ``complete`` returns for the
        same upstream turn, because accumulation feeds the same ``parse_*``
        functions with a virtual final payload.
        """
        body = self._render_body(request, tools_enabled=tools_enabled, streaming=True)
        url = self._base() + self._path()
        client = self.client or httpx.AsyncClient(timeout=self.timeout_s)
        reassembler = StatefulSSEFilter()
        filters = self._sse_filters()
        raw_parts: list[bytes] = []
        try:
            async with client.stream("POST", url, json=body, headers=self._headers(), timeout=self.timeout_s) as resp:
                resp.raise_for_status()
                acc = _StreamAccumulator(self.wire_format, sink)
                async for chunk in resp.aiter_bytes():
                    raw_parts.append(chunk)
                    data = chunk
                    for f in (reassembler, *filters):
                        data = f.feed(data)
                    for event, payload, done in _parse_frames(data):
                        if done:
                            break
                        acc.consume_frame(event, payload)
                data = reassembler.flush()
                for f in filters:
                    data = f.feed(data)
                for event, payload, done in _parse_frames(data):
                    if done:
                        break
                    acc.consume_frame(event, payload)
                if acc.is_empty() and raw_parts and b"".join(raw_parts).lstrip().startswith(b"{"):
                    # Some OpenAI-compatible servers ignore ``stream: true`` and
                    # answer with a plain JSON body — degrade to buffered parsing
                    # so the writer still emits a complete stream at finish.
                    return await self._parse_json_body(
                        b"".join(raw_parts), wire_format=self.wire_format,
                    )
                return acc.response()
        except httpx.HTTPError as exc:
            raise _upstream_error(self.name, exc) from exc
        finally:
            if self.client is None:
                await client.aclose()

    async def _parse_json_body(self, raw: bytes, *, wire_format: str) -> LLMResponse:
        if self.normalizers:
            raw = _normalize_raw(self.normalizers, raw)
        payload = json.loads(raw)
        if self.normalizers:
            payload = _normalize_json(self.normalizers, payload)
        return parse_payload(wire_format, payload)

    def _sse_filters(self) -> list[StatefulSSEFilter]:
        if not self.normalizers:
            return []
        out = []
        for key in self.normalizers:
            factory = getattr(NORMALIZER_REGISTRY[key], "sse_filter", None)
            if factory is not None:
                out.append(factory())
        return out


# --- upstream SSE chunk translation (FR-014) --------------------------------


def parse_payload(wire_format: str, payload: dict[str, Any]) -> LLMResponse:
    """Parse a virtual/real final payload for ``wire_format`` into an LLMResponse."""
    return parse_anthropic(payload) if wire_format == "anthropic" else parse_openai(payload)


def _parse_frames(data: bytes) -> list[tuple[str | None, dict[str, Any] | None, bool]]:
    """Split already-reassembled SSE bytes into ``(event, payload, done)`` frames."""
    out: list[tuple[str | None, dict[str, Any] | None, bool]] = []
    for frame in data.split(b"\n\n"):
        if not frame.strip():
            continue
        event, payload = StatefulSSEFilter.parse_frame(frame)
        data_lines = [
            ln[5:].strip()
            for ln in frame.decode("utf-8", "replace").splitlines()
            if ln.startswith("data:")
        ]
        done = "\n".join(data_lines) == "[DONE]"
        out.append((event, payload, done))
    return out


class _StreamAccumulator:
    """Consumes canonical deltas, forwards them to the sink, and accumulates a
    virtual final payload that ``parse_payload`` turns into the LLMResponse."""

    def __init__(self, wire_format: str, sink: Any) -> None:
        self._anthropic = wire_format == "anthropic"
        self._sink = sink
        self.content: list[str] = []
        self.reasoning: list[str] = []
        self.tools: dict[int, dict[str, str]] = {}
        self.stop: str | None = None
        self.envelope: dict[str, Any] = {}

    def consume_frame(self, event: str | None, payload: dict[str, Any] | None) -> None:
        if payload is None:
            return
        for d in (
            _anthropic_event_deltas(event, payload)
            if self._anthropic else _openai_chunk_deltas(payload)
        ):
            self._consume(d)

    def _consume(self, d: StreamDelta) -> None:
        if d.envelope and not self.envelope:
            self.envelope = d.envelope
        if d.kind == "text":
            self.content.append(d.text)
        elif d.kind == "reasoning":
            self.reasoning.append(d.text)
        elif d.kind == "tool":
            t = self.tools.setdefault(d.tool_index or 0, {"id": "", "name": "", "arguments": ""})
            if d.tool_id:
                t["id"] = d.tool_id
            if d.tool_name:
                t["name"] = d.tool_name
            if d.args_fragment:
                t["arguments"] += d.args_fragment
        elif d.kind == "finish":
            self.stop = d.finish_reason
        if self._sink is not None:
            self._sink.delta(d)

    def is_empty(self) -> bool:
        """True when the upstream produced no SSE state at all (JSON-fallback candidate)."""
        return not (self.content or self.reasoning or self.tools or self.stop or self.envelope)

    def response(self) -> LLMResponse:
        if self._anthropic:
            payload = self._anthropic_payload()
        else:
            payload = self._openai_payload()
        return parse_payload("anthropic" if self._anthropic else "openai", payload)

    def _openai_payload(self) -> dict[str, Any]:
        tools = [
            {
                "id": t["id"],
                "type": "function",
                "function": {"name": t["name"], "arguments": t["arguments"]},
            }
            for _i, t in sorted(self.tools.items())
        ]
        message: dict[str, Any] = {"content": "".join(self.content) or None}
        if self.reasoning:
            message["reasoning_content"] = "".join(self.reasoning)
        if tools:
            message["tool_calls"] = tools
        return {
            "id": self.envelope.get("id"),
            "object": "chat.completion",
            "created": self.envelope.get("created"),
            "model": self.envelope.get("model"),
            "choices": [{"index": 0, "finish_reason": self.stop, "message": message}],
        }

    def _anthropic_payload(self) -> dict[str, Any]:
        content: list[dict[str, Any]] = []
        if self.reasoning:
            content.append({"type": "thinking", "thinking": "".join(self.reasoning)})
        if self.content:
            content.append({"type": "text", "text": "".join(self.content)})
        for _i, t in sorted(self.tools.items()):
            content.append({
                "type": "tool_use", "id": t["id"], "name": t["name"],
                "input": _loads(t["arguments"]),
            })
        payload: dict[str, Any] = {"content": content}
        if self.stop:
            payload["stop_reason"] = self.stop
        payload.update(self.envelope)
        return payload


def _openai_chunk_deltas(payload: dict[str, Any]) -> list[StreamDelta]:
    choice = (payload.get("choices") or [{}])[0]
    delta = (choice or {}).get("delta") or {}
    envelope = {k: payload[k] for k in ("id", "created", "model") if k in payload} or None
    out: list[StreamDelta] = []
    if delta.get("reasoning_content"):
        out.append(StreamDelta("reasoning", text=delta["reasoning_content"], envelope=envelope))
    if delta.get("content"):
        out.append(StreamDelta("text", text=delta["content"], envelope=envelope))
    for tc in delta.get("tool_calls") or []:
        fn = tc.get("function") or {}
        out.append(StreamDelta(
            "tool", tool_index=tc.get("index", 0), tool_id=tc.get("id"),
            tool_name=fn.get("name"), args_fragment=fn.get("arguments") or "",
            envelope=envelope,
        ))
    finish = (choice or {}).get("finish_reason")
    if finish is not None:
        out.append(StreamDelta("finish", finish_reason=finish, envelope=envelope))
    return out


def _anthropic_event_deltas(event: str | None, payload: dict[str, Any]) -> list[StreamDelta]:
    out: list[StreamDelta] = []
    ptype = payload.get("type") or event
    if ptype == "message_start":
        msg = payload.get("message") or {}
        envelope = {k: msg[k] for k in ("id", "model") if k in msg}
        if envelope:
            out.append(StreamDelta("envelope", envelope=envelope))
    elif ptype == "content_block_start":
        blk = payload.get("content_block") or {}
        if blk.get("type") == "tool_use":
            out.append(StreamDelta(
                "tool", tool_index=payload.get("index", 0),
                tool_id=blk.get("id"), tool_name=blk.get("name"),
            ))
    elif ptype == "content_block_delta":
        d = payload.get("delta") or {}
        dt = d.get("type")
        if dt == "text_delta":
            out.append(StreamDelta("text", text=d.get("text", "")))
        elif dt == "thinking_delta":
            out.append(StreamDelta("reasoning", text=d.get("thinking", "")))
        elif dt == "input_json_delta":
            out.append(StreamDelta(
                "tool", tool_index=payload.get("index", 0),
                args_fragment=d.get("partial_json", ""),
            ))
    elif ptype == "message_delta":
        stop = (payload.get("delta") or {}).get("stop_reason")
        if stop:
            out.append(StreamDelta("finish", finish_reason=stop))
    return out
