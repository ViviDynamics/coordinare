from __future__ import annotations

from coordinare.lib.redaction import redact_mapping
from coordinare.lib.runtime_events import build_runtime_event


def test_failure_event_contains_failing_step() -> None:
    event = build_runtime_event(
        category="failure",
        message="runtime processing cycle failed",
        failing_step="cycle_execution",
    )

    assert event["category"] == "failure"
    assert event["failing_step"] == "cycle_execution"


def test_sensitive_contract_keys_are_redacted() -> None:
    payload = {
        "token": "abc",
        "api_key": "xyz",
        "safe": "value",
    }

    redacted = redact_mapping(payload)

    assert redacted["token"] == "***REDACTED***"
    assert redacted["api_key"] == "***REDACTED***"
    assert redacted["safe"] == "value"
