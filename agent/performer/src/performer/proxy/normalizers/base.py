"""Normalizer protocol + stateful SSE filter base for spec 078.

A *normalizer* is a pure, reusable response transform keyed by response format /
model-family (not by agent — the quirks are per-format and shared across every
agent that talks to that format). Each normalizer handles BOTH response paths:

* the non-streaming JSON body (``normalize_json``), and
* the streaming Server-Sent-Events body (``sse_filter`` → a stateful filter).

The SSE filter MUST buffer across network-chunk boundaries and never emit a
half-parsed tool call or a torn marker (FR-078-3). ``StatefulSSEFilter`` provides
the chunk-buffering + frame-splitting scaffold (lifted from the 073
``ClaudeCodeShim._SSEThinkingFilter``); concrete filters override
``process_frame`` to transform a single complete SSE frame.

Unknown-format rule: a normalizer that does not recognize the response shape
returns it **unchanged** (fail-open on normalization, FR-078-9). Health gating is
the only fail-*closed* surface in the layer.
"""

from __future__ import annotations

import json
from typing import Any, Protocol, runtime_checkable


class StatefulSSEFilter:
    """Buffer raw SSE bytes across chunk boundaries and transform whole frames.

    SSE frames are separated by a blank line (``\\n\\n``). A network chunk may
    split a frame anywhere — mid-marker, mid-JSON, mid-frame — so we accumulate
    bytes in an internal buffer and only hand *complete* frames to
    :meth:`process_frame`. This guarantees a subclass never sees (and so never
    emits) a half-parsed tool call or a torn ``<|channel|>`` marker.

    Subclasses override :meth:`process_frame`. The default implementation is the
    identity transform (pass-through), which is also the correct fail-open
    behavior for an unrecognized shape.
    """

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, chunk: bytes) -> bytes:
        """Append ``chunk``; return transformed bytes for any now-complete frames."""
        self._buf.extend(chunk)
        out = bytearray()
        while True:
            sep = self._buf.find(b"\n\n")
            if sep == -1:
                break
            frame = bytes(self._buf[:sep])
            del self._buf[: sep + 2]
            transformed = self.process_frame(frame)
            if transformed is not None:
                out.extend(transformed)
                out.extend(b"\n\n")
        return bytes(out)

    def flush(self) -> bytes:
        """Return any trailing partial frame (no terminating blank line seen).

        A trailing frame without ``\\n\\n`` is malformed per the SSE spec, but we
        pass it through verbatim rather than swallow data.
        """
        if not self._buf:
            return b""
        tail = bytes(self._buf)
        self._buf.clear()
        return tail

    def process_frame(self, frame: bytes) -> bytes | None:
        """Transform one complete SSE frame.

        Return the (possibly rewritten) frame bytes to emit it, or ``None`` to
        drop the frame entirely. The base implementation passes frames through
        unchanged. Subclasses override this.
        """
        return frame

    # -- helpers shared by concrete filters ------------------------------- #

    @staticmethod
    def parse_frame(frame: bytes) -> tuple[str | None, dict[str, Any] | None]:
        """Decode an SSE frame into ``(event_name, json_payload)``.

        Returns ``(event_name, None)`` when the data is absent or not a JSON
        object, so callers can fall back to passing the frame through unchanged.
        ``event_name`` is ``None`` when the frame carries no ``event:`` line
        (common for OpenAI-style ``data:``-only streams).
        """
        event_name: str | None = None
        data_lines: list[str] = []
        for line in frame.split(b"\n"):
            try:
                text = line.decode("utf-8")
            except UnicodeDecodeError:
                return None, None
            if text.startswith("event:"):
                event_name = text[6:].strip()
            elif text.startswith("data:"):
                data_lines.append(text[5:].lstrip())
        if not data_lines:
            return event_name, None
        data_str = "\n".join(data_lines)
        if data_str.strip() == "[DONE]":
            return event_name, None
        try:
            payload = json.loads(data_str)
        except json.JSONDecodeError:
            return event_name, None
        if not isinstance(payload, dict):
            return event_name, None
        return event_name, payload

    @staticmethod
    def encode_frame(event_name: str | None, payload: dict[str, Any]) -> bytes:
        """Re-encode a JSON payload as an SSE frame (no trailing blank line)."""
        data = json.dumps(payload, separators=(",", ":"))
        if event_name is not None:
            return f"event: {event_name}\ndata: {data}".encode("utf-8")
        return f"data: {data}".encode("utf-8")


@runtime_checkable
class Normalizer(Protocol):
    """A format-keyed response transform registered in ``NORMALIZER_REGISTRY``."""

    @property
    def key(self) -> str:
        """Registry key, e.g. ``harmony_tool_calls`` or ``strip_reasoning``."""
        ...

    def normalize_json(self, body: dict[str, Any]) -> dict[str, Any]:
        """Transform a non-streaming JSON response body.

        MUST return the body unchanged when the shape is unrecognized
        (fail-open, FR-078-9).
        """
        ...

    def sse_filter(self) -> StatefulSSEFilter:
        """Return a fresh stateful filter for one SSE response stream."""
        ...
