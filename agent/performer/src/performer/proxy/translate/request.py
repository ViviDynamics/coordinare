"""Anthropic ``/v1/messages`` → OpenAI ``/v1/chat/completions`` request
translation (spec 084, FR-001 / FR-004).

Pure function, no network/disk I/O and no logging of body content (FR-011), so it
is unit-testable directly against dict bodies. The mapping is the testable
artifact described in ``contracts/request-translation.md`` — every transform here
is deterministic and any dropped field is dropped *explicitly* (no silent
passthrough of Anthropic-only fields, which the OpenAI upstream would reject —
Decision 5).

US1 landed the top-level field map plus text/system message turns. US2 (T016)
adds the ``tools`` / ``tool_choice`` / ``tool_use`` / ``tool_result`` mappings so
a full tool exchange survives the round trip.
"""

from __future__ import annotations

import json
from typing import Any

#: Anthropic top-level fields with a direct OpenAI counterpart (same name).
_PASSTHROUGH_FIELDS = ("model", "max_tokens", "temperature", "top_p", "stream")

#: Anthropic top-level fields intentionally dropped — recorded here, never
#: silently carried (Decision 5). ``metadata`` has no OpenAI equivalent;
#: ``thinking`` is handled response-side by the ``strip_reasoning`` normalizer.
_DROPPED_FIELDS = ("metadata", "thinking")


def _content_to_text(content: Any) -> str:
    """Flatten Anthropic message ``content`` (string or block array) to text.

    A string is returned as-is. A block array contributes each ``text`` block's
    ``text`` value, ``"\\n"``-joined in order. Non-text blocks (e.g. ``tool_use``,
    ``tool_result``) are ignored here — those are handled by the tool mapping.
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            block["text"]
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    return ""


def _translate_tool(tool: dict[str, Any]) -> dict[str, Any]:
    """Map one Anthropic tool definition to an OpenAI ``tools[]`` entry.

    ``input_schema`` (JSON Schema) becomes ``function.parameters``; ``name`` and
    ``description`` are carried; ``type: "function"`` is the constant wrapper.
    """
    function: dict[str, Any] = {"name": tool.get("name")}
    if "description" in tool:
        function["description"] = tool["description"]
    if "input_schema" in tool:
        function["parameters"] = tool["input_schema"]
    return {"type": "function", "function": function}


def _translate_tool_choice(tool_choice: Any) -> Any:
    """Map the Anthropic ``tool_choice`` shape to the OpenAI ``tool_choice`` shape.

    ``auto`` → ``"auto"``; ``any`` → ``"required"``; ``{type:tool,name:N}`` →
    ``{type:function,function:{name:N}}``. An unrecognized shape is returned
    unchanged rather than dropped (no silent loss — Decision 5).
    """
    if not isinstance(tool_choice, dict):
        return tool_choice
    kind = tool_choice.get("type")
    if kind == "auto":
        return "auto"
    if kind == "any":
        return "required"
    if kind == "tool":
        return {"type": "function", "function": {"name": tool_choice.get("name")}}
    return tool_choice


def _tool_call_entry(block: dict[str, Any]) -> dict[str, Any]:
    """Map an Anthropic ``tool_use`` block to an OpenAI ``tool_calls[]`` entry.

    ``input`` (object) is JSON-stringified into ``function.arguments`` (the OpenAI
    wire carries arguments as a string).
    """
    return {
        "id": block.get("id"),
        "type": "function",
        "function": {
            "name": block.get("name"),
            "arguments": json.dumps(block.get("input", {})),
        },
    }


def _tool_result_message(block: dict[str, Any]) -> dict[str, Any]:
    """Map an Anthropic ``tool_result`` block to an OpenAI ``tool`` message.

    ``tool_use_id`` → ``tool_call_id``; string content is carried directly, a
    block array is flattened to joined text. ``is_error`` has no OpenAI flag — the
    textual content already carries the error (contract).
    """
    return {
        "role": "tool",
        "tool_call_id": block.get("tool_use_id"),
        "content": _content_to_text(block.get("content", "")),
    }


def _translate_turn(turn: dict[str, Any]) -> list[dict[str, Any]]:
    """Map one Anthropic message turn to one or more OpenAI messages.

    * an assistant turn with ``tool_use`` blocks becomes a single assistant
      message carrying ``tool_calls`` (and text content, or ``null`` when the turn
      is tool-calls-only);
    * a user turn with ``tool_result`` blocks becomes one ``tool`` message per
      block, with any non-tool_result text emitted as a separate ``user`` message;
    * every other turn is a plain text message.
    """
    role = turn["role"]
    content = turn.get("content", "")

    blocks = content if isinstance(content, list) else None
    tool_use = (
        [b for b in blocks if isinstance(b, dict) and b.get("type") == "tool_use"]
        if blocks
        else []
    )
    tool_result = (
        [b for b in blocks if isinstance(b, dict) and b.get("type") == "tool_result"]
        if blocks
        else []
    )

    if role == "assistant" and tool_use:
        text = _content_to_text(content)
        message: dict[str, Any] = {
            "role": "assistant",
            "content": text if text else None,
            "tool_calls": [_tool_call_entry(b) for b in tool_use],
        }
        return [message]

    if tool_result:
        messages = [_tool_result_message(b) for b in tool_result]
        text = _content_to_text(content)
        if text:
            messages.insert(0, {"role": role, "content": text})
        return messages

    return [{"role": role, "content": _content_to_text(content)}]


def translate_request(anthropic_body: dict) -> dict:
    """Translate an Anthropic ``/v1/messages`` body to an OpenAI
    ``/v1/chat/completions`` body.

    Pure: the input dict is never mutated; a fresh dict is returned.
    """
    out: dict[str, Any] = {}

    for field in _PASSTHROUGH_FIELDS:
        if field in anthropic_body:
            out[field] = anthropic_body[field]

    if "stop_sequences" in anthropic_body:
        out["stop"] = anthropic_body["stop_sequences"]

    messages: list[dict[str, Any]] = []

    system = anthropic_body.get("system")
    if system is not None:
        messages.append({"role": "system", "content": _content_to_text(system)})

    for turn in anthropic_body.get("messages", []):
        messages.extend(_translate_turn(turn))

    out["messages"] = messages

    tools = anthropic_body.get("tools")
    if tools:
        out["tools"] = [_translate_tool(t) for t in tools]

    if "tool_choice" in anthropic_body:
        out["tool_choice"] = _translate_tool_choice(anthropic_body["tool_choice"])

    return out
