"""078 — ``harmony_tool_calls`` normalizer (gpt-oss via LiteLLM).

LiteLLM's streaming harmony->tool_calls transform for gpt-oss leaks the raw
harmony commentary channel into the assistant ``content`` instead of emitting
structured ``tool_calls`` (LiteLLM #13300/#17246). The leaked text looks like::

    <|channel|>commentary to=functions.<tool> <|constrain|>json<|message|>{args}<|call|>

This normalizer reassembles that text into a well-formed OpenAI ``tool_calls``
entry on BOTH the non-streaming JSON path (:meth:`normalize_json`) and the
streaming SSE path (:meth:`sse_filter`), leaking zero ``<|channel|>``/``<|call|>``
markers and never emitting a half-parsed call (FR-078-7, FR-078-3).

Fail-open (FR-078-9): a response with no harmony markers is returned unchanged.
"""

from __future__ import annotations

import copy
import re
from typing import Any

from .base import StatefulSSEFilter

# One leaked harmony tool call: the commentary channel, an optional <|constrain|>
# directive, the JSON arguments between <|message|> and <|call|>.
_HARMONY_CALL = re.compile(
    r"<\|channel\|>commentary\s+to=functions\.(?P<name>[\w.\-]+)\s*"
    r"(?:<\|constrain\|>\w+)?"
    r"<\|message\|>(?P<args>.*?)<\|call\|>",
    re.DOTALL,
)

# Cheap presence check so we can fail-open without running the full regex.
_HARMONY_MARKER = "<|channel|>"


def _extract_calls(content: str) -> tuple[list[dict[str, Any]], str]:
    """Pull harmony tool calls out of ``content``.

    Returns ``(tool_calls, residual_content)`` where ``residual_content`` is the
    assistant text with every harmony call segment removed. When no harmony call
    is present, returns ``([], content)`` unchanged (fail-open).
    """
    calls: list[dict[str, Any]] = []
    for i, m in enumerate(_HARMONY_CALL.finditer(content)):
        calls.append(
            {
                "id": f"call_{i}",
                "type": "function",
                "function": {
                    "name": m.group("name"),
                    "arguments": m.group("args").strip(),
                },
            }
        )
    if not calls:
        return [], content
    residual = _HARMONY_CALL.sub("", content).strip()
    return calls, residual


class _HarmonySSEFilter(StatefulSSEFilter):
    """Reassemble harmony tool calls leaked across OpenAI streaming deltas.

    Harmony content is split across ``delta.content`` fragments (and the markers
    themselves split across chunk boundaries — the base class already buffers
    whole frames). Once a content delta carries a harmony marker we enter harmony
    mode: subsequent content fragments are accumulated and the original
    content-bearing frames are dropped (they carry markers). When the buffer holds
    a complete call (``<|call|>`` seen), a single well-formed ``tool_calls`` delta
    frame is emitted in its place.
    """

    def __init__(self) -> None:
        super().__init__()
        self._content_buf = ""
        self._in_harmony = False
        self._call_index = 0

    def process_frame(self, frame: bytes) -> bytes | None:
        _event, payload = self.parse_frame(frame)
        if payload is None:
            # [DONE], non-JSON, or a frame without a JSON object — pass through.
            return frame
        try:
            choice = payload["choices"][0]
            delta = choice.get("delta", {})
        except (KeyError, IndexError, TypeError):
            return frame

        content = delta.get("content")
        is_harmony_frag = isinstance(content, str) and (
            self._in_harmony or _HARMONY_MARKER in content
        )
        if not is_harmony_frag:
            return frame  # role / normal content / finish — untouched

        # Accumulate harmony text; drop the original (marker-bearing) content.
        self._in_harmony = True
        self._content_buf += content

        emitted = self._drain_complete_calls(payload)
        return emitted  # None drops the frame until a call completes

    def _drain_complete_calls(self, template: dict[str, Any]) -> bytes | None:
        """Emit a tool_calls frame for each complete call now in the buffer."""
        out = bytearray()
        while True:
            m = _HARMONY_CALL.search(self._content_buf)
            if m is None:
                break
            tool_call = {
                "index": self._call_index,
                "id": f"call_{self._call_index}",
                "type": "function",
                "function": {
                    "name": m.group("name"),
                    "arguments": m.group("args").strip(),
                },
            }
            self._call_index += 1
            self._content_buf = (
                self._content_buf[: m.start()] + self._content_buf[m.end() :]
            )
            frame_payload: dict[str, Any] = {
                k: v for k, v in template.items() if k != "choices"
            }
            src_choice = template["choices"][0]
            frame_payload["choices"] = [
                {
                    "index": src_choice.get("index", 0),
                    "delta": {"tool_calls": [tool_call]},
                    "finish_reason": None,
                }
            ]
            out.extend(self.encode_frame(None, frame_payload))
            out.extend(b"\n\n")
        if not out:
            return None
        # Strip the trailing separator the base class re-adds per emitted frame.
        return bytes(out[:-2])


class HarmonyToolCallsNormalizer:
    """Reassemble leaked harmony commentary into structured ``tool_calls``."""

    @property
    def key(self) -> str:
        return "harmony_tool_calls"

    def normalize_json(self, body: dict[str, Any]) -> dict[str, Any]:
        choices = body.get("choices")
        if not isinstance(choices, list):
            return body  # not an OpenAI chat.completion shape — fail-open
        # Only a message whose content carries the harmony marker needs rewriting;
        # copy-on-write so we never mutate the caller's body (fail-open otherwise).
        if not any(
            isinstance(c, dict)
            and isinstance(c.get("message"), dict)
            and isinstance(c["message"].get("content"), str)
            and _HARMONY_MARKER in c["message"]["content"]
            for c in choices
        ):
            return body
        body = copy.deepcopy(body)
        for choice in body["choices"]:
            if not isinstance(choice, dict):
                continue
            message = choice.get("message")
            if not isinstance(message, dict):
                continue
            content = message.get("content")
            if not isinstance(content, str) or _HARMONY_MARKER not in content:
                continue
            calls, residual = _extract_calls(content)
            if not calls:
                continue
            message["tool_calls"] = calls
            message["content"] = residual or None
        return body

    def sse_filter(self) -> StatefulSSEFilter:
        return _HarmonySSEFilter()
