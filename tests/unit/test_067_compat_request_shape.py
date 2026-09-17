"""Unit tests for LCD payload shape validation (spec 067 T018).

Table-driven assertions that ``_assert_lcd_payload`` accepts every fixture in
``tests/fixtures/lcd_payloads/valid_lcd_request.json`` and rejects every entry
in ``forbidden_fields_request.json`` with an error message that names the
offending field.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest
from performer.backends.opencode_compat import LcdPayloadError, _assert_lcd_payload

FIXTURE_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "lcd_payloads"


def _load(name: str) -> dict:
    return json.loads((FIXTURE_DIR / name).read_text())


def test_valid_lcd_request_accepted():
    payload = _load("valid_lcd_request.json")
    _assert_lcd_payload(payload)  # must not raise


_FORBIDDEN = _load("forbidden_fields_request.json")
_FORBIDDEN_CASES = [
    (key, payload)
    for key, payload in _FORBIDDEN.items()
    if not key.startswith("_")
]


@pytest.mark.parametrize("case_name,payload", _FORBIDDEN_CASES, ids=[c[0] for c in _FORBIDDEN_CASES])
def test_forbidden_payload_rejected(case_name: str, payload: dict):
    with pytest.raises(LcdPayloadError) as excinfo:
        _assert_lcd_payload(payload)
    message = str(excinfo.value).lower()
    # Each case name must reference the field it is denying in the error.
    expected_fragments = {
        "hosted_tool_type_web_search": ["web_search", "tools"],
        "hosted_tool_type_file_search": ["file_search", "tools"],
        "developer_role": ["developer"],
        "prompt_cache_key": ["prompt_cache_key"],
        "response_format_json_schema": ["json_schema"],
        "parallel_tool_calls": ["parallel_tool_calls"],
        "user_telemetry_tag": ["user"],
    }
    needles = expected_fragments[case_name]
    assert any(n.lower() in message for n in needles), (
        f"error message for {case_name!r} should name one of {needles}; got: {message!r}"
    )


def test_response_format_text_accepted():
    _assert_lcd_payload(
        {
            "model": "qwen3-coder-30b",
            "messages": [{"role": "user", "content": "Hi"}],
            "response_format": {"type": "text"},
        },
    )


def test_unknown_role_rejected():
    with pytest.raises(LcdPayloadError, match="role"):
        _assert_lcd_payload(
            {
                "model": "x",
                "messages": [{"role": "function", "content": "Hi"}],
            },
        )
