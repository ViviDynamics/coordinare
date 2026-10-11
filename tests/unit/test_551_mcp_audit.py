"""Spec 551: audit emitter key vocabulary."""

from __future__ import annotations

import structlog

from coordinare.mcp.audit import emit_tool_invoked


def test_mutating_call_emits_one_line_with_contract_keys() -> None:
    with structlog.testing.capture_logs() as captured:
        emit_tool_invoked(
            tool="update_config",
            action="update",
            outcome="ok",
            credential_fingerprint="abcd1234",
            duration_ms=12,
        )
    records = [r for r in captured if r.get("event") == "mcp.tool_invoked"]
    assert len(records) == 1
    record = records[0]
    assert record["tool"] == "update_config"
    assert record["action"] == "update"
    assert record["outcome"] == "ok"
    assert record["error_kind"] is None
    assert record["credential_fingerprint"] == "abcd1234"
    assert record["duration_ms"] == 12


def test_error_outcome_carries_error_kind() -> None:
    with structlog.testing.capture_logs() as captured:
        emit_tool_invoked(
            tool="control",
            action="cancel",
            outcome="error",
            error_kind="conflict",
            credential_fingerprint="abcd1234",
            duration_ms=5,
        )
    record = captured[-1]
    assert record["outcome"] == "error"
    assert record["error_kind"] == "conflict"
