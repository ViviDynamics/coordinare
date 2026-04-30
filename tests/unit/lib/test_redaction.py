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


def test_redacts_job_secrets_dict() -> None:
    """Verify secrets dict (from JobInitPayload) is redacted."""
    payload = {
        "job_id": "123",
        "secrets": {"GITHUB_TOKEN": "ghp_abc123", "API_KEY": "sk-123"},
    }
    result = redact_mapping(payload)

    assert result["job_id"] == "123"
    assert result["secrets"] == "***REDACTED***"


def test_redacts_auth_token() -> None:
    """Verify auth_token is redacted."""
    payload = {
        "id": "perf-1",
        "auth_token": "secret_token_value",
        "endpoint": "http://localhost:8088",
    }
    result = redact_mapping(payload)

    assert result["id"] == "perf-1"
    assert result["auth_token"] == "***REDACTED***"
    assert result["endpoint"] == "http://localhost:8088"
