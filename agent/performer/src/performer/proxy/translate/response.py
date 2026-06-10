"""OpenAI ``/v1/chat/completions`` → Anthropic ``/v1/messages`` non-streaming
response translation (spec 084, FR-002 / FR-004 / FR-005).

Pure function, no network/disk I/O and no logging of body/token content
(FR-011). Runs as the **outermost** response transform: by the time it sees the
body, the normalizers (``harmony_tool_calls``, ``strip_reasoning``) have already
produced a clean OpenAI shape (Decision 4), so this consumes structured
``tool_calls`` rather than harmony-leaked text.

The ``finish_reason`` → ``stop_reason`` map is imported from
:mod:`proxy.translate.finish_reason` — the single source of truth shared with the
streaming path so the two cannot drift (Decision 6).
"""

from __future__ import annotations

import json
from typing import Any

from .finish_reason import map_finish_reason

#: Deterministic id synthesized when the upstream omits ``id`` (no randomness so
#: the translator stays pure and testable). The Anthropic wire only requires the
#: ``msg_`` prefix; uniqueness is not contractually load-bearing here.
_SYNTHETIC_ID = "msg_translated"


def _tool_use_block(tool_call: dict[str, Any]) -> dict[str, Any]:
    """Map one OpenAI ``tool_calls[]`` entry to an Anthropic ``tool_use`` block.

    ``function.arguments`` is a JSON *string* on the wire; it is parsed into the
    ``input`` object. Unparsable arguments degrade to ``input: {}`` (the
    documented degenerate case — never a silent drop of the block).
    """
    function = tool_call.get("function", {})
    raw_args = function.get("arguments", "")
    try:
        parsed = json.loads(raw_args) if raw_args else {}
    except (json.JSONDecodeError, TypeError):
        parsed = {}
    if not isinstance(parsed, dict):
        parsed = {}
    return {
        "type": "tool_use",
        "id": tool_call.get("id"),
        "name": function.get("name"),
        "input": parsed,
    }


def translate_response(openai_body: dict) -> dict:
    """Translate an OpenAI chat-completion object to an Anthropic
    ``/v1/messages`` response object.

    Pure: the input dict is never mutated; a fresh dict is returned.
    """
    choice = (openai_body.get("choices") or [{}])[0]
    message = choice.get("message", {})

    content: list[dict[str, Any]] = []

    text = message.get("content")
    tool_calls = message.get("tool_calls") or []

    # A text block is emitted only when there is real text. ``content: None``
    # alongside tool calls must NOT produce an empty text block (contract).
    if text:
        content.append({"type": "text", "text": text})

    for tool_call in tool_calls:
        content.append(_tool_use_block(tool_call))

    usage_in = openai_body.get("usage") or {}
    usage_out: dict[str, Any] = {}
    if "prompt_tokens" in usage_in:
        usage_out["input_tokens"] = usage_in["prompt_tokens"]
    if "completion_tokens" in usage_in:
        usage_out["output_tokens"] = usage_in["completion_tokens"]

    out: dict[str, Any] = {
        "id": openai_body.get("id") or _SYNTHETIC_ID,
        "type": "message",
        "role": "assistant",
        "model": openai_body.get("model"),
        "content": content,
        "stop_reason": map_finish_reason(choice.get("finish_reason")),
        "stop_sequence": None,
    }
    if usage_out:
        out["usage"] = usage_out
    return out
