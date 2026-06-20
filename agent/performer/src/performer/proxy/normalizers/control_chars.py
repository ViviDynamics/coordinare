"""098 US2 — ``strip_control_chars`` normalizer.

A flaky self-hosted upstream sometimes emits invalid/unescaped control bytes
(codepoint < 0x20, except the JSON-legal ``\\t`` ``\\n`` ``\\r``) inside response
string values. Unescaped control bytes are illegal JSON, so the strict parser the
assessor (junie) harness relies on rejects the whole body — terminal-erroring the
card. This normalizer removes those bytes so the body parses cleanly.

It exposes three surfaces:

* :meth:`normalize_raw` — the raw-bytes pre-parse path. This is the one that
  matters for the failure shape: a control byte breaks ``json.loads`` *before*
  any dict exists, so the shim applies this to the raw body before parsing.
* :meth:`normalize_json` — defensive parsed-JSON path: strips control chars from
  string values that survived parsing (e.g. via a lenient upstream).
* :meth:`sse_filter` — the streaming path: cleans each frame's payload.

Fail-open: a clean body/frame passes through unchanged (FR-078-9 / FR-006).
"""

from __future__ import annotations

import copy
from typing import Any

from .base import StatefulSSEFilter

# Control codepoints to remove: everything below 0x20 except the three JSON
# string-legal whitespace escapes (tab, newline, carriage return). 0x7f (DEL) is
# left alone — it is valid in JSON strings and not part of the observed failures.
_ALLOWED_LOW = frozenset({0x09, 0x0A, 0x0D})
_STRIP_CODEPOINTS = frozenset(c for c in range(0x20) if c not in _ALLOWED_LOW)
# Translation table for str.translate (drop each stripped codepoint).
_STR_DELETE = {c: None for c in _STRIP_CODEPOINTS}
# Byte set for the raw-bytes path.
_STRIP_BYTES = bytes(_STRIP_CODEPOINTS)


def _clean_str(s: str) -> str:
    return s.translate(_STR_DELETE)


def _clean_obj(obj: Any) -> Any:
    """Recursively strip control chars from every string value in a JSON tree."""
    if isinstance(obj, str):
        return _clean_str(obj)
    if isinstance(obj, list):
        return [_clean_obj(v) for v in obj]
    if isinstance(obj, dict):
        return {k: _clean_obj(v) for k, v in obj.items()}
    return obj


def _has_control(obj: Any) -> bool:
    if isinstance(obj, str):
        return any(ord(ch) in _STRIP_CODEPOINTS for ch in obj)
    if isinstance(obj, list):
        return any(_has_control(v) for v in obj)
    if isinstance(obj, dict):
        return any(_has_control(v) for v in obj.values())
    return False


class _ControlCharSSEFilter(StatefulSSEFilter):
    """Strip control chars from each complete SSE frame's JSON payload."""

    def process_frame(self, frame: bytes) -> bytes | None:
        event, payload = self.parse_frame(frame)
        if payload is None:
            # Could not parse (possibly *because* of control bytes) — strip
            # illegal control bytes from the raw frame, preserving the structural
            # newlines/tabs that delimit SSE lines, and pass it through.
            stripped = frame.translate(None, _STRIP_BYTES)
            return stripped
        if not _has_control(payload):
            return frame  # fail-open: untouched
        return self.encode_frame(event, _clean_obj(payload))


class ControlCharNormalizer:
    """Remove invalid/unescaped control bytes from upstream responses."""

    @property
    def key(self) -> str:
        return "strip_control_chars"

    def normalize_raw(self, raw: bytes) -> bytes:
        """Strip illegal control bytes from a raw (pre-parse) response body.

        Safe and fail-open: in valid JSON the only legal sub-0x20 bytes are
        tab/newline/CR (structural whitespace, preserved here); any other control
        byte is already illegal, so removing it can only repair, never corrupt.
        """
        if not any(b in raw for b in _STRIP_BYTES):
            return raw
        return raw.translate(None, _STRIP_BYTES)

    def normalize_json(self, body: dict[str, Any]) -> dict[str, Any]:
        if not _has_control(body):
            return body
        return _clean_obj(copy.deepcopy(body))

    def sse_filter(self) -> StatefulSSEFilter:
        return _ControlCharSSEFilter()
