"""078 — ``strip_reasoning`` normalizer (claude + qwen reasoners).

Generalizes the 073 ``ClaudeCodeShim`` thinking-block strip. Reasoning/thinking
blocks that leak into a response confuse the agent CLI parser and must be dropped
on BOTH the non-streaming JSON path and the streaming SSE path, leaving the real
assistant output intact (FR-078-8, FR-078-3). Two leak shapes are handled:

* **Anthropic** ``thinking`` (and ``redacted_thinking``) content blocks, in the
  JSON ``content`` array and as streamed ``content_block_*`` events. Streamed
  text blocks following a dropped thinking block are renumbered so the surviving
  block indices stay contiguous from 0.
* **OpenAI / qwen** ``reasoning_content`` on the message (JSON) and on streaming
  ``delta`` objects.

Fail-open (FR-078-9): a response carrying neither shape is returned unchanged.
"""

from __future__ import annotations

import copy
from typing import Any

from .base import StatefulSSEFilter

_THINKING_TYPES = frozenset({"thinking", "redacted_thinking"})


class _StripReasoningSSEFilter(StatefulSSEFilter):
    """Drop reasoning/thinking from Anthropic and OpenAI streaming responses."""

    def __init__(self) -> None:
        super().__init__()
        # Anthropic: old content-block indices we've suppressed, and the remap of
        # surviving old index -> new contiguous index.
        self._suppressed: set[int] = set()
        self._index_map: dict[int, int] = {}
        self._next_index = 0

    def process_frame(self, frame: bytes) -> bytes | None:
        event, payload = self.parse_frame(frame)
        if payload is None:
            return frame

        ptype = payload.get("type")
        if isinstance(ptype, str) and ptype.startswith("content_block"):
            return self._anthropic_frame(event, payload, ptype)

        if "choices" in payload:
            return self._openai_frame(event, payload)

        return frame

    # -- Anthropic content_block_* events --------------------------------- #

    def _anthropic_frame(
        self, event: str | None, payload: dict[str, Any], ptype: str
    ) -> bytes | None:
        index = payload.get("index")
        if ptype == "content_block_start":
            block = payload.get("content_block", {})
            if isinstance(block, dict) and block.get("type") in _THINKING_TYPES:
                if isinstance(index, int):
                    self._suppressed.add(index)
                return None  # drop the thinking block start
            if isinstance(index, int):
                self._index_map[index] = self._next_index
                self._next_index += 1
            return self._reindex(event, payload, index)

        # delta / stop for a previously seen block
        if isinstance(index, int) and index in self._suppressed:
            return None
        return self._reindex(event, payload, index)

    def _reindex(
        self, event: str | None, payload: dict[str, Any], index: Any
    ) -> bytes:
        """Rewrite a surviving block's index to its contiguous new value."""
        if isinstance(index, int) and index in self._index_map:
            new_index = self._index_map[index]
            if new_index != index:
                payload = dict(payload)
                payload["index"] = new_index
                return self.encode_frame(event, payload)
        return self.encode_frame(event, payload)

    # -- OpenAI / qwen reasoning_content deltas --------------------------- #

    def _openai_frame(self, event: str | None, payload: dict[str, Any]) -> bytes | None:
        try:
            choice = payload["choices"][0]
        except (KeyError, IndexError, TypeError):
            return self.encode_frame(event, payload)
        delta = choice.get("delta")
        if not isinstance(delta, dict) or "reasoning_content" not in delta:
            return self.encode_frame(event, payload)
        delta = {k: v for k, v in delta.items() if k != "reasoning_content"}
        choice = {**choice, "delta": delta}
        # A delta that held only reasoning_content is now empty and carries no
        # finish_reason — drop it rather than stream a content-less chunk.
        if not delta and choice.get("finish_reason") is None:
            return None
        payload = {**payload, "choices": [choice, *payload["choices"][1:]]}
        return self.encode_frame(event, payload)


class StripReasoningNormalizer:
    """Strip leaked reasoning/thinking blocks (Anthropic + OpenAI shapes)."""

    @property
    def key(self) -> str:
        return "strip_reasoning"

    def normalize_json(self, body: dict[str, Any]) -> dict[str, Any]:
        # Decide whether either leak shape is present; copy-on-write so we never
        # mutate the caller's parsed body when nothing needs stripping (fail-open).
        content = body.get("content")
        has_thinking = isinstance(content, list) and any(
            isinstance(b, dict) and b.get("type") in _THINKING_TYPES for b in content
        )
        choices = body.get("choices")
        has_reasoning = isinstance(choices, list) and any(
            isinstance(c, dict)
            and isinstance(c.get("message"), dict)
            and "reasoning_content" in c["message"]
            for c in choices
        )
        if not has_thinking and not has_reasoning:
            return body

        body = copy.deepcopy(body)

        # Anthropic: drop thinking blocks from the content array.
        if has_thinking:
            body["content"] = [
                b
                for b in body["content"]
                if not (isinstance(b, dict) and b.get("type") in _THINKING_TYPES)
            ]

        # OpenAI / qwen: drop reasoning_content from each message.
        if has_reasoning:
            for choice in body["choices"]:
                if not isinstance(choice, dict):
                    continue
                message = choice.get("message")
                if isinstance(message, dict):
                    message.pop("reasoning_content", None)

        return body

    def sse_filter(self) -> StatefulSSEFilter:
        return _StripReasoningSSEFilter()
