"""098 US2 (T007): control-char-stripping normalizer.

A flaky self-hosted upstream sometimes emits invalid/unescaped control bytes
(< 0x20 except \\t \\n \\r) in the response body, which breaks the strict JSON
parser the assessor (junie) harness uses. This normalizer removes those bytes on
the raw-bytes pre-parse path, the parsed-JSON path, and the SSE path. Fail-open:
a clean body passes through unchanged.
"""
from __future__ import annotations

import json

from performer.proxy.normalizers import NORMALIZER_REGISTRY
from performer.proxy.normalizers.control_chars import ControlCharNormalizer


def _norm() -> ControlCharNormalizer:
    return ControlCharNormalizer()


def test_registered_under_key() -> None:
    assert "strip_control_chars" in NORMALIZER_REGISTRY
    assert NORMALIZER_REGISTRY["strip_control_chars"].key == "strip_control_chars"


def test_normalize_raw_strips_invalid_control_bytes_and_parses() -> None:
    # An unescaped control byte (0x07 BEL) inside a JSON string value — illegal
    # JSON, breaks json.loads. After raw stripping the body must parse cleanly.
    broken = b'{"choices":[{"message":{"content":"hel\x07lo"}}]}'
    repaired = _norm().normalize_raw(broken)
    parsed = json.loads(repaired)
    assert parsed["choices"][0]["message"]["content"] == "hello"


def test_normalize_raw_preserves_structural_whitespace() -> None:
    clean = b'{\n\t"a": "b\\nc"\n}'  # real \n/\t structure + an escaped \n in value
    out = _norm().normalize_raw(clean)
    assert json.loads(out) == {"a": "b\nc"}


def test_normalize_raw_clean_body_unchanged() -> None:
    clean = b'{"choices":[{"message":{"content":"all good"}}]}'
    assert _norm().normalize_raw(clean) == clean


def test_normalize_json_strips_control_chars_in_string_values() -> None:
    body = {"choices": [{"message": {"content": "he\x07l\x00lo"}}]}
    out = _norm().normalize_json(body)
    assert out["choices"][0]["message"]["content"] == "hello"


def test_normalize_json_clean_body_is_fail_open() -> None:
    body = {"choices": [{"message": {"content": "fine\nwith newline"}}]}
    out = _norm().normalize_json(body)
    assert out["choices"][0]["message"]["content"] == "fine\nwith newline"


def test_sse_filter_strips_control_chars_in_frame() -> None:
    f = _norm().sse_filter()
    frame = b'data: {"choices":[{"delta":{"content":"hi\x07!"}}]}\n\n'
    out = f.feed(frame) + f.flush()
    # The emitted frame must parse and carry the cleaned content.
    text = out.decode("utf-8")
    payload = json.loads(text.split("data:", 1)[1].strip())
    assert payload["choices"][0]["delta"]["content"] == "hi!"


def test_sse_filter_clean_frame_roundtrips() -> None:
    f = _norm().sse_filter()
    frame = b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
    out = f.feed(frame) + f.flush()
    payload = json.loads(out.decode("utf-8").split("data:", 1)[1].strip())
    assert payload["choices"][0]["delta"]["content"] == "ok"
