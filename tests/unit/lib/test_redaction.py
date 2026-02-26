from __future__ import annotations

from coordinare.lib.redaction import redact_mapping


def test_redacts_known_sensitive_keys_case_insensitive() -> None:
    payload = {
        "token": "abc",
        "Password": "secret",
        "meta": {"authorization": "Bearer 123", "safe": "ok"},
    }

    result = redact_mapping(payload)

    assert result["token"] == "***REDACTED***"
    assert result["Password"] == "***REDACTED***"
    assert result["meta"]["authorization"] == "***REDACTED***"
    assert result["meta"]["safe"] == "ok"


def test_keeps_non_sensitive_values() -> None:
    payload = {"phase": "running", "error_count": 0}
    assert redact_mapping(payload) == payload


def test_redacts_sensitive_keys_inside_lists() -> None:
    payload = {"items": [{"token": "abc", "safe": "ok"}]}
    result = redact_mapping(payload)

    assert result["items"][0]["token"] == "***REDACTED***"
    assert result["items"][0]["safe"] == "ok"
