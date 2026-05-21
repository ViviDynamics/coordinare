"""Unit tests for outbound request-body redaction (spec 067 T020).

Covers:
  (a) Each denylisted key is redacted.
  (b) Each secret pattern (``sk-...``, ``gho_...``, ``Bearer ...``, etc.) is
      redacted inside ``messages[].content`` string leaves.
  (c) The outbound body is **not** mutated — only the returned redaction copy
      contains the placeholders.
"""
from __future__ import annotations

import copy

import pytest
from performer.backends._lcd_helpers import REDACTED as _REDACTED
from performer.backends._lcd_helpers import redact_request_body as _redact_request_body


@pytest.mark.parametrize(
    "key",
    ["api_key", "Authorization", "OPENAI_API_KEY", "token", "secret", "password", "bearer"],
)
def test_denylisted_keys_redacted(key: str):
    body = {key: "sensitive-value", "model": "x"}
    out = _redact_request_body(body)
    assert out[key] == _REDACTED
    assert out["model"] == "x"


@pytest.mark.parametrize(
    "key,value",
    [
        # ``max_tokens`` is a first-class LCD field — must NOT be redacted by
        # an over-broad substring match on "token".
        ("max_tokens", 4096),
        # Similar near-misses that were prone to substring false positives.
        ("tokens_processed", 128),
        ("response_format", {"type": "json_object"}),
        ("tool_choice", "auto"),
    ],
)
def test_lcd_payload_fields_not_redacted(key: str, value):
    body = {key: value, "model": "x"}
    out = _redact_request_body(body)
    assert out[key] == value, f"{key!r} must survive redaction; got {out[key]!r}"


@pytest.mark.parametrize(
    "needle,fragment",
    [
        ("sk-aBcDeFgHiJkLmNoPqRsTuVwXyZ012345", "sk-"),
        ("gho_aBcDeFgHiJkLmNoPqRsTuVwXyZ012345", "gho_"),
        ("ghp_aBcDeFgHiJkLmNoPqRsTuVwXyZ012345", "ghp_"),
        ("OPENAI_API_KEY=sk-secretvalue123456", "OPENAI_API_KEY=***REDACTED***"),
        ("Bearer abcdefghij.klmnop.qrstuv", "Bearer ***REDACTED***"),
        # Base64-padded bearer (JWT bodies frequently include '=' / '+' / '/').
        ("Bearer eyJhbGciOiJIUzI1NiJ9.payload+stuff/here.sig==", "Bearer ***REDACTED***"),
        # Bare JWT not prefixed with "Bearer ".
        ("token=eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJqb2UifQ.signaturepart", "REDACTED_JWT"),
        # Bare JWT with base64 padding / '+' / '/' in the body or signature.
        ("see eyJhbGciOiJIUzI1NiJ9.payload+stuff/here.sig== here", "REDACTED_JWT"),
    ],
)
def test_secret_patterns_redacted_in_message_content(needle: str, fragment: str):
    body = {
        "model": "x",
        "messages": [
            {"role": "user", "content": f"prefix {needle} suffix"},
        ],
    }
    out = _redact_request_body(body)
    rendered = out["messages"][0]["content"]
    assert needle not in rendered
    assert "REDACTED" in rendered or fragment in rendered


def test_outbound_body_not_mutated():
    body = {
        "api_key": "sk-realsecret012345678901234567",
        "model": "x",
        "messages": [{"role": "user", "content": "Token sk-aaaaaaaaaaaaaaaa1111"}],
    }
    snapshot = copy.deepcopy(body)
    _ = _redact_request_body(body)
    assert body == snapshot, "redaction must not mutate the input body"


def test_nested_structures_walked():
    body = {
        "model": "x",
        "messages": [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {
                            "name": "fetch",
                            "arguments": '{"authorization": "Bearer abcdef.gh.ij"}',
                        },
                    }
                ],
            }
        ],
    }
    out = _redact_request_body(body)
    args = out["messages"][0]["tool_calls"][0]["function"]["arguments"]
    assert "Bearer abcdef" not in args
