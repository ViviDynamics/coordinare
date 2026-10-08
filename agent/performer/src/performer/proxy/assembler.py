"""080 — response assembler.

Merges the orchestration result (an ``LLMResponse`` whose ``reasoning`` holds the
planner's plan) into one wire-correct response body for the originating CLI,
applying ``expose_plan_as`` (FR-013):

- ``thinking``         — surface the plan in a reasoning channel (anthropic
  ``thinking`` block / openai ``reasoning_content``)
- ``prepend_content``  — prepend the plan to the answer text
- ``drop``             — internal only; do not surface the plan

JSON (non-streaming) rendering lives here. The SSE path (act phase streamed to
the CLI with synthesized plan events first) is layered on in the proxy shell and
reuses this merge logic per chunk.
"""

from __future__ import annotations

import json
from typing import Any, Literal

from performer.proxy.llm_turn import LLMResponse, StreamDelta

ExposePlanAs = Literal["thinking", "prepend_content", "drop"]
WireFormat = Literal["openai", "anthropic", "responses"]

_RESPONSE_ID = "resp_dualproxy"
_CHAT_COMPLETION_ID = "chatcmpl-dualproxy"


def assemble_json(
    response: LLMResponse, *, expose_plan_as: ExposePlanAs, wire_format: WireFormat,
) -> dict[str, Any]:
    """Render a merged ``LLMResponse`` to the CLI's wire JSON body."""
    plan = response.reasoning
    if wire_format == "anthropic":
        return _assemble_anthropic(response, plan, expose_plan_as)
    if wire_format == "responses":
        return _assemble_responses(response, plan, expose_plan_as)
    return _assemble_openai(response, plan, expose_plan_as)


def _content_with_plan(content: str, plan: str | None, expose: ExposePlanAs) -> str:
    if plan and expose == "prepend_content":
        return f"{plan}\n\n{content}" if content else plan
    return content


def _assemble_openai(resp: LLMResponse, plan: str | None, expose: ExposePlanAs) -> dict[str, Any]:
    message: dict[str, Any] = {
        "role": "assistant",
        "content": _content_with_plan(resp.content, plan, expose),
    }
    if plan and expose == "thinking":
        message["reasoning_content"] = plan
    if resp.tool_calls:
        message["tool_calls"] = [
            {
                "id": tc.id,
                "type": "function",
                "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)},
            }
            for tc in resp.tool_calls
        ]
    finish = "tool_calls" if resp.tool_calls else "stop"
    # 082 FR-013: emit a COMPLETE ChatCompletion envelope. A bare
    # {"choices": [...]} body is rejected by strict OpenAI clients (junie's
    # OpenAICompletion deserializer → "Failed to build 'issue.md.junie_standalone'"
    # after a successful 200), even though lenient clients tolerate it. Echo the
    # upstream exec response's real id/model/created when preserved in ``raw``;
    # fall back to stable proxy constants otherwise.
    raw = resp.raw or {}
    return {
        "id": raw.get("id") or _CHAT_COMPLETION_ID,
        "object": "chat.completion",
        "created": raw.get("created") if isinstance(raw.get("created"), int) else 0,
        "model": raw.get("model", ""),
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
    }


def _assemble_anthropic(resp: LLMResponse, plan: str | None, expose: ExposePlanAs) -> dict[str, Any]:
    blocks: list[dict[str, Any]] = []
    if plan and expose == "thinking":
        blocks.append({"type": "thinking", "thinking": plan})
    text = _content_with_plan(resp.content, plan, expose)
    if text:
        blocks.append({"type": "text", "text": text})
    for tc in resp.tool_calls:
        blocks.append({"type": "tool_use", "id": tc.id, "name": tc.name, "input": tc.arguments})
    stop = "tool_use" if resp.tool_calls else "end_turn"
    return {"role": "assistant", "content": blocks, "stop_reason": stop}


def _assemble_responses(resp: LLMResponse, plan: str | None, expose: ExposePlanAs) -> dict[str, Any]:
    """Render to an OpenAI Responses ``object: "response"`` body.

    Output items, in order: an optional ``reasoning`` item (plan as
    ``summary_text`` when ``expose == thinking``), a ``message`` item carrying the
    answer as an ``output_text`` content block, then one ``function_call`` item per
    tool call (arguments JSON-encoded to a string, as the Responses API requires).
    """
    output: list[dict[str, Any]] = []
    if plan and expose == "thinking":
        output.append({"type": "reasoning", "summary": [{"type": "summary_text", "text": plan}]})
    text = _content_with_plan(resp.content, plan, expose)
    if text:
        output.append({
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": text, "annotations": []}],
        })
    for tc in resp.tool_calls:
        output.append({
            "type": "function_call",
            "call_id": tc.id,
            "name": tc.name,
            "arguments": json.dumps(tc.arguments),
            "status": "completed",
        })
    return {"id": _RESPONSE_ID, "object": "response", "status": "completed", "output": output}


# --- SSE rendering ---------------------------------------------------------
#
# The think phase runs internally (non-streamed); we synthesize a valid SSE
# event stream from the final assembled response so the CLI's streaming parser
# is satisfied. Plan events (when expose_plan_as requires) are emitted first
# (FR-014). Each returned string is a complete SSE block ready to write.


def assemble_sse(
    response: LLMResponse, *, expose_plan_as: ExposePlanAs, wire_format: WireFormat,
) -> list[str]:
    """Render a merged ``LLMResponse`` to an ordered list of SSE event blocks."""
    if wire_format == "anthropic":
        return _sse_anthropic(response, response.reasoning, expose_plan_as)
    if wire_format == "responses":
        return _sse_responses(response, response.reasoning, expose_plan_as)
    return _sse_openai(response, response.reasoning, expose_plan_as)


def _sse_data(payload: dict[str, Any], event: str | None = None) -> str:
    line = f"event: {event}\n" if event else ""
    return f"{line}data: {json.dumps(payload)}\n\n"


def _sse_openai(resp: LLMResponse, plan: str | None, expose: ExposePlanAs) -> list[str]:
    # 082 FR-013 (SSE sibling): carry the full chunk envelope (id/object/
    # created/model), echoing the upstream exec response when preserved in
    # ``raw``, so strict streaming clients accept the synthesized stream.
    raw = resp.raw or {}
    chunk = {
        "id": raw.get("id") or _CHAT_COMPLETION_ID,
        "object": "chat.completion.chunk",
        "created": raw.get("created") if isinstance(raw.get("created"), int) else 0,
        "model": raw.get("model", ""),
        "choices": [{"index": 0, "delta": {}, "finish_reason": None}],
    }

    def delta(d: dict[str, Any], finish: str | None = None) -> str:
        c = json.loads(json.dumps(chunk))
        c["choices"][0]["delta"] = d
        c["choices"][0]["finish_reason"] = finish
        return _sse_data(c)

    events = [delta({"role": "assistant"})]
    if plan and expose == "thinking":
        events.append(delta({"reasoning_content": plan}))
    text = _content_with_plan(resp.content, plan, expose)
    if text:
        events.append(delta({"content": text}))
    for i, tc in enumerate(resp.tool_calls):
        events.append(delta({"tool_calls": [{
            "index": i, "id": tc.id, "type": "function",
            "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)},
        }]}))
    events.append(delta({}, finish="tool_calls" if resp.tool_calls else "stop"))
    events.append("data: [DONE]\n\n")
    return events


def _sse_anthropic(resp: LLMResponse, plan: str | None, expose: ExposePlanAs) -> list[str]:
    events = [_sse_data({"type": "message_start", "message": {"role": "assistant", "content": []}}, "message_start")]
    idx = 0

    def block(start: dict[str, Any], delta: dict[str, Any]) -> None:
        nonlocal idx
        events.append(_sse_data({"type": "content_block_start", "index": idx, "content_block": start}, "content_block_start"))
        events.append(_sse_data({"type": "content_block_delta", "index": idx, "delta": delta}, "content_block_delta"))
        events.append(_sse_data({"type": "content_block_stop", "index": idx}, "content_block_stop"))
        idx += 1

    if plan and expose == "thinking":
        block({"type": "thinking", "thinking": ""}, {"type": "thinking_delta", "thinking": plan})
    text = _content_with_plan(resp.content, plan, expose)
    if text:
        block({"type": "text", "text": ""}, {"type": "text_delta", "text": text})
    for tc in resp.tool_calls:
        block(
            {"type": "tool_use", "id": tc.id, "name": tc.name, "input": {}},
            {"type": "input_json_delta", "partial_json": json.dumps(tc.arguments)},
        )
    stop = "tool_use" if resp.tool_calls else "end_turn"
    events.append(_sse_data({"type": "message_delta", "delta": {"stop_reason": stop}}, "message_delta"))
    events.append(_sse_data({"type": "message_stop"}, "message_stop"))
    return events


def _sse_responses(resp: LLMResponse, plan: str | None, expose: ExposePlanAs) -> list[str]:
    """Synthesize the documented Responses SSE event stream from the final body.

    Bracketed by ``response.created``/``response.completed`` (the terminal event
    carries the fully-populated ``response`` so a delta-ignoring client still
    reads the final output). Each output item is announced (``output_item.added``),
    streamed (``output_text.delta`` for messages, ``function_call_arguments.delta``
    for tool calls), and closed (``output_item.done``). Every event carries a
    monotonic ``sequence_number``.
    """
    final = _assemble_responses(resp, plan, expose)
    events: list[str] = []
    seq = 0

    def emit(payload: dict[str, Any]) -> None:
        nonlocal seq
        events.append(_sse_data({**payload, "sequence_number": seq}))
        seq += 1

    head = {"id": final["id"], "object": "response", "status": "in_progress", "output": []}
    emit({"type": "response.created", "response": head})
    emit({"type": "response.in_progress", "response": head})

    for out_index, item in enumerate(final["output"]):
        if item["type"] == "reasoning":
            emit({"type": "response.output_item.added", "output_index": out_index, "item": item})
            emit({"type": "response.output_item.done", "output_index": out_index, "item": item})
        elif item["type"] == "message":
            text = item["content"][0]["text"]
            emit({
                "type": "response.output_item.added",
                "output_index": out_index,
                "item": {"type": "message", "role": "assistant", "content": []},
            })
            emit({
                "type": "response.content_part.added",
                "output_index": out_index,
                "content_index": 0,
                "part": {"type": "output_text", "text": "", "annotations": []},
            })
            emit({
                "type": "response.output_text.delta",
                "output_index": out_index,
                "content_index": 0,
                "delta": text,
            })
            emit({
                "type": "response.output_text.done",
                "output_index": out_index,
                "content_index": 0,
                "text": text,
            })
            emit({
                "type": "response.content_part.done",
                "output_index": out_index,
                "content_index": 0,
                "part": {"type": "output_text", "text": text, "annotations": []},
            })
            emit({"type": "response.output_item.done", "output_index": out_index, "item": item})
        elif item["type"] == "function_call":
            args = item["arguments"]
            emit({
                "type": "response.output_item.added",
                "output_index": out_index,
                "item": {
                    "type": "function_call",
                    "call_id": item["call_id"],
                    "name": item["name"],
                    "arguments": "",
                },
            })
            emit({
                "type": "response.function_call_arguments.delta",
                "output_index": out_index,
                "delta": args,
            })
            emit({
                "type": "response.function_call_arguments.done",
                "output_index": out_index,
                "arguments": args,
            })
            emit({"type": "response.output_item.done", "output_index": out_index, "item": item})

    emit({"type": "response.completed", "response": final})
    return events


# --- incremental SSE writing (FR-014) ---------------------------------------
#
# The true-streaming path consumes canonical StreamDeltas as the act upstream
# produces them and renders wire-correct SSE blocks incrementally. With a
# single text fragment the emitted block sequence is identical to
# ``assemble_sse`` for the same response; with multiple fragments only the text
# splits across chunks (same event order, same envelopes). Tool calls are
# rendered at ``finish`` from the accumulated LLMResponse — the writer never
# re-encodes streamed argument fragments, so terminal frames are
# byte-identical to the buffered emitters.


class SseStreamWriter:
    """Front-door SSE writer doubling as the strategy ``ActDeltaSink``.

    Lifecycle: ``start(plan)`` once (act phase entry), ``delta(...)`` per
    upstream fragment, then ``finish(response)`` by the shell after the
    strategy returns. Rendered blocks are appended to ``blocks`` and pushed to
    ``emit`` (when set) as they are produced, so the shell can drain the queue
    while the strategy is still running.
    """

    def __init__(
        self, *, expose_plan_as: ExposePlanAs, wire_format: WireFormat,
        emit: Any = None,
    ) -> None:
        self.expose_plan_as = expose_plan_as
        self.wire_format = wire_format
        self.blocks: list[str] = []
        self.emit = emit
        self._plan: str | None = None
        self._envelope: dict[str, Any] | None = None
        self._text_streamed = False
        self._started = False
        # anthropic block lifecycle
        self._next_index = 0
        self._open_kind: str | None = None  # "text" | "thinking"
        self._open_index = 0
        # responses sequence numbers
        self._seq = 0
        self._message_out_index = 0
        self._responses_out = 0

    def _append(self, block: str) -> None:
        self.blocks.append(block)
        if self.emit is not None:
            self.emit(block)

    # -- sink surface (ActDeltaSink) --

    def start(self, plan: str | None) -> None:
        """Record the plan. Prefix blocks are deferred until the first upstream
        delta (or finish) so the upstream envelope is captured first and echoed
        on every chunk, buffered parity."""
        self._plan = plan

    def _ensure_started(self) -> None:
        if self._started:
            return
        self._started = True
        plan_text = self._plan if (self._plan and self.expose_plan_as != "drop") else None
        if self.wire_format == "anthropic":
            self._anthropic_start(plan_text)
        elif self.wire_format == "responses":
            self._responses_start(plan_text)
        else:
            self._openai_start(plan_text)

    def delta(self, d: StreamDelta) -> None:
        if d.envelope and self._envelope is None:
            self._envelope = d.envelope
        self._ensure_started()
        if d.kind == "text":
            if self.wire_format == "anthropic":
                self._anthropic_text_delta(d.text)
            elif self.wire_format == "responses":
                self._responses_text_delta(d.text)
            else:
                self._append(self._chunk_body({"content": d.text}))
            self._text_streamed = True
        # "reasoning" upstream fragments are dropped (the plan is the surfaced
        # reasoning, buffered parity); "finish" terminals render at finish();
        # tool calls render at finish() from the accumulated LLMResponse, so
        # terminal frames stay byte-identical to the buffered emitters.

    def finish(self, response: LLMResponse) -> None:
        self._ensure_started()
        if self.wire_format == "anthropic":
            self._anthropic_finish(response)
        elif self.wire_format == "responses":
            self._responses_finish(response)
        else:
            self._openai_finish(response)

    # -- openai ------------------------------------------------------------

    def _chunk_body(self, d: dict[str, Any], finish: str | None = None) -> str:
        env = self._envelope or {}
        created = env.get("created")
        chunk = {
            "id": env.get("id") or _CHAT_COMPLETION_ID,
            "object": "chat.completion.chunk",
            "created": created if isinstance(created, int) else 0,
            "model": env.get("model", ""),
            "choices": [{"index": 0, "delta": d, "finish_reason": finish}],
        }
        return _sse_data(chunk)

    def _openai_start(self, plan_text: str | None) -> None:
        self._append(self._chunk_body({"role": "assistant"}))
        if plan_text and self.expose_plan_as == "thinking":
            self._append(self._chunk_body({"reasoning_content": plan_text}))
        if plan_text and self.expose_plan_as == "prepend_content":
            self._append(self._chunk_body({"content": plan_text + "\n\n"}))

    def _openai_finish(self, response: LLMResponse) -> None:
        if not self._text_streamed:
            # the plan prefix was already emitted by _ensure_started()
            text = _content_with_plan(response.content, None, self.expose_plan_as)
            if text:
                self._append(self._chunk_body({"content": text}))
        for i, tc in enumerate(response.tool_calls):
            self._append(self._chunk_body({"tool_calls": [{
                "index": i, "id": tc.id, "type": "function",
                "function": {"name": tc.name, "arguments": json.dumps(tc.arguments)},
            }]}))
        self._append(self._chunk_body({}, finish="tool_calls" if response.tool_calls else "stop"))
        self._append("data: [DONE]\n\n")

    # -- anthropic -----------------------------------------------------------

    def _anthropic_start(self, plan_text: str | None) -> None:
        self._append(_sse_data(
            {"type": "message_start", "message": {"role": "assistant", "content": []}},
            "message_start",
        ))
        if plan_text and self.expose_plan_as == "thinking":
            self._anthropic_open_block("thinking", plan_text)
        if plan_text and self.expose_plan_as == "prepend_content":
            self._anthropic_open_block("text", plan_text + "\n\n")

    def _anthropic_open_block(self, kind: str, text: str) -> None:
        index = self._next_index
        self._next_index += 1
        self._append(_sse_data(
            {"type": "content_block_start", "index": index, "content_block": {"type": kind, kind: ""}},
            "content_block_start",
        ))
        delta = (
            {"type": "thinking_delta", "thinking": text}
            if kind == "thinking" else {"type": "text_delta", "text": text}
        )
        self._append(_sse_data(
            {"type": "content_block_delta", "index": index, "delta": delta},
            "content_block_delta",
        ))
        self._open_kind = kind
        self._open_index = index

    def _anthropic_text_delta(self, text: str) -> None:
        if self._open_kind != "text":
            self._anthropic_close_block()
            self._anthropic_open_block("text", text)
            self._text_streamed = True
            return
        self._append(_sse_data(
            {"type": "content_block_delta", "index": self._open_index,
             "delta": {"type": "text_delta", "text": text}},
            "content_block_delta",
        ))
        self._text_streamed = True

    def _anthropic_close_block(self) -> None:
        if self._open_kind is None:
            return
        self._append(_sse_data(
            {"type": "content_block_stop", "index": self._open_index},
            "content_block_stop",
        ))
        self._open_kind = None

    def _anthropic_finish(self, response: LLMResponse) -> None:
        if not self._text_streamed:
            # the plan prefix was already emitted by _ensure_started()
            text = _content_with_plan(response.content, None, self.expose_plan_as)
            if text:
                self._anthropic_close_block()  # the plan block, if open
                self._anthropic_open_block("text", text)
        self._anthropic_close_block()
        for tc in response.tool_calls:
            index = self._next_index
            self._next_index += 1
            self._append(_sse_data(
                {"type": "content_block_start", "index": index,
                 "content_block": {"type": "tool_use", "id": tc.id, "name": tc.name, "input": {}}},
                "content_block_start",
            ))
            self._append(_sse_data(
                {"type": "content_block_delta", "index": index,
                 "delta": {"type": "input_json_delta", "partial_json": json.dumps(tc.arguments)}},
                "content_block_delta",
            ))
            self._append(_sse_data(
                {"type": "content_block_stop", "index": index}, "content_block_stop",
            ))
        stop = "tool_use" if response.tool_calls else "end_turn"
        self._append(_sse_data(
            {"type": "message_delta", "delta": {"stop_reason": stop}}, "message_delta",
        ))
        self._append(_sse_data({"type": "message_stop"}, "message_stop"))

    # -- responses -----------------------------------------------------------

    def _emit(self, payload: dict[str, Any]) -> None:
        self._append(_sse_data({**payload, "sequence_number": self._seq}))
        self._seq += 1

    def _responses_start(self, plan_text: str | None) -> None:
        head = {"id": _RESPONSE_ID, "object": "response", "status": "in_progress", "output": []}
        self._emit({"type": "response.created", "response": head})
        self._emit({"type": "response.in_progress", "response": head})
        if plan_text and self.expose_plan_as == "thinking":
            item = {"type": "reasoning", "summary": [{"type": "summary_text", "text": plan_text}]}
            self._emit({"type": "response.output_item.added", "output_index": 0, "item": item})
            self._emit({"type": "response.output_item.done", "output_index": 0, "item": item})
            self._responses_out = 1

    def _responses_text_delta(self, text: str) -> None:
        if not self._text_streamed:
            plan_prefix = ""
            if self._plan and self.expose_plan_as == "prepend_content":
                plan_prefix = self._plan + "\n\n"
            out_index = self._responses_out
            self._emit({
                "type": "response.output_item.added", "output_index": out_index,
                "item": {"type": "message", "role": "assistant", "content": []},
            })
            self._emit({
                "type": "response.content_part.added", "output_index": out_index,
                "content_index": 0, "part": {"type": "output_text", "text": "", "annotations": []},
            })
            self._message_out_index = out_index
            self._text_streamed = True
            text = plan_prefix + text
        self._emit({
            "type": "response.output_text.delta", "output_index": self._message_out_index,
            "content_index": 0, "delta": text,
        })

    def _responses_finish(self, response: LLMResponse) -> None:
        final = assemble_json(response, expose_plan_as=self.expose_plan_as, wire_format="responses")
        text_full = _content_with_plan(response.content, self._plan, self.expose_plan_as)
        if self._text_streamed:
            self._emit({
                "type": "response.output_text.done", "output_index": self._message_out_index,
                "content_index": 0, "text": text_full,
            })
            self._emit({
                "type": "response.content_part.done", "output_index": self._message_out_index,
                "content_index": 0,
                "part": {"type": "output_text", "text": text_full, "annotations": []},
            })
            self._emit({
                "type": "response.output_item.done", "output_index": self._message_out_index,
                "item": {
                    "type": "message", "role": "assistant",
                    "content": [{"type": "output_text", "text": text_full, "annotations": []}],
                },
            })
            self._responses_out += 1
        elif text_full:
            self._responses_message_item(text_full)
        for item in final["output"]:
            if item["type"] == "function_call":
                self._responses_tool_item(item)
        self._emit({"type": "response.completed", "response": final})

    def _responses_message_item(self, text: str) -> None:
        out_index = self._responses_out
        self._emit({
            "type": "response.output_item.added", "output_index": out_index,
            "item": {"type": "message", "role": "assistant", "content": []},
        })
        self._emit({
            "type": "response.content_part.added", "output_index": out_index,
            "content_index": 0, "part": {"type": "output_text", "text": "", "annotations": []},
        })
        self._emit({
            "type": "response.output_text.delta", "output_index": out_index,
            "content_index": 0, "delta": text,
        })
        self._emit({
            "type": "response.output_text.done", "output_index": out_index,
            "content_index": 0, "text": text,
        })
        self._emit({
            "type": "response.content_part.done", "output_index": out_index,
            "content_index": 0, "part": {"type": "output_text", "text": text, "annotations": []},
        })
        self._emit({
            "type": "response.output_item.done", "output_index": out_index,
            "item": {
                "type": "message", "role": "assistant",
                "content": [{"type": "output_text", "text": text, "annotations": []}],
            },
        })
        self._responses_out += 1

    def _responses_tool_item(self, item: dict[str, Any]) -> None:
        out_index = self._responses_out
        self._emit({
            "type": "response.output_item.added", "output_index": out_index,
            "item": {"type": "function_call", "call_id": item["call_id"],
                     "name": item["name"], "arguments": ""},
        })
        self._emit({
            "type": "response.function_call_arguments.delta",
            "output_index": out_index, "delta": item["arguments"],
        })
        self._emit({
            "type": "response.function_call_arguments.done",
            "output_index": out_index, "arguments": item["arguments"],
        })
        self._emit({"type": "response.output_item.done", "output_index": out_index, "item": item})
        self._responses_out += 1
