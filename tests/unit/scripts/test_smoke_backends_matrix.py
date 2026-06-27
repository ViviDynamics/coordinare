"""Spec 122 US2: tests for the compatibility-matrix row builder in
scripts/smoke_backends.py — pure logic, no containers.

Verifies the contract in specs/122-litellm-backend-routing/contracts/compatibility-matrix.md.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))
smoke_backends = pytest.importorskip("smoke_backends")

build_matrix_row = smoke_backends.build_matrix_row
compute_verdict = smoke_backends.compute_verdict
gateway_unavailable = smoke_backends.gateway_unavailable


def _result(**kw) -> dict:
    base = {
        "endpoint": "claude-litellm", "backend": "claude_code",
        "model": "spark/gpt-oss:120b", "launched": True, "state": "succeeded",
        "output_len": 500, "performer_status": "qa_passed", "error": "", "detail": "",
    }
    base.update(kw)
    return base


# --- compute_verdict ------------------------------------------------------


def test_verdict_compatible_when_all_signals_pass():
    assert compute_verdict(launched=True, completed=True, contract_satisfied=True,
                           gateway_available=True) == "compatible"


def test_verdict_incompatible_when_contract_missed():
    assert compute_verdict(launched=True, completed=True, contract_satisfied=False,
                           gateway_available=True) == "incompatible"


def test_verdict_incompatible_when_not_completed():
    assert compute_verdict(launched=True, completed=False, contract_satisfied=False,
                           gateway_available=True) == "incompatible"


def test_verdict_gateway_unavailable_takes_precedence():
    # Even if other signals are false, an unavailable gateway is its own bucket.
    assert compute_verdict(launched=True, completed=False, contract_satisfied=False,
                           gateway_available=False) == "gateway_unavailable"


# --- gateway_unavailable detection ----------------------------------------


@pytest.mark.parametrize("text", [
    "litellm.BadRequestError: no healthy deployments for this model",
    "POST /jobs -> 503 Service Unavailable",
    "ConnectError: connection refused",
    "ReadTimeout: read timed out",
])
def test_gateway_unavailable_true_on_availability_failures(text):
    assert gateway_unavailable(text) is True


@pytest.mark.parametrize("text", [
    "", "qa verdict missing criteria_checked", "backend produced empty output",
    "BACKEND_FORMAT_ERROR: prose not JSON",
])
def test_gateway_unavailable_false_on_backend_failures(text):
    assert gateway_unavailable(text) is False


# --- build_matrix_row (full contract) -------------------------------------


def test_row_compatible_for_clean_pass():
    row = build_matrix_row(_result(performer_status="qa_passed"))
    assert row["verdict"] == "compatible"
    assert row["launched"] and row["completed"] and row["output_present"]
    assert row["contract_satisfied"] is True
    assert row["normalizers_needed"] == []
    assert row["gateway_available"] is True
    for f in ("backend", "endpoint_id", "model", "launched", "completed",
              "output_present", "contract_satisfied", "normalizers_needed",
              "gateway_available", "verdict", "note"):
        assert f in row


def test_row_qa_env_blocked_is_compatible():
    # A valid qa_env_blocked verdict proves the backend reached the model through
    # the gateway and emitted its role contract — the env limit is incidental.
    row = build_matrix_row(_result(performer_status="qa_env_blocked", state="failed",
                                   detail="Ruby runtime not available"))
    assert row["completed"] is True
    assert row["verdict"] == "compatible"


def test_row_qa_failed_is_compatible():
    row = build_matrix_row(_result(performer_status="qa_failed", state="failed"))
    assert row["verdict"] == "compatible"


def test_row_incompatible_on_backend_format_error():
    row = build_matrix_row(_result(performer_status="error", state="failed",
                                   detail="BACKEND_FORMAT_ERROR: prose not JSON"))
    assert row["completed"] is False
    assert row["contract_satisfied"] is False
    assert row["verdict"] == "incompatible"


def test_row_gateway_unavailable_distinct_from_incompatible():
    row = build_matrix_row(_result(performer_status="", state="failed", output_len=0,
                                   error="no healthy deployments for this model"))
    assert row["gateway_available"] is False
    assert row["verdict"] == "gateway_unavailable"


def test_row_output_present_false_on_empty():
    row = build_matrix_row(_result(output_len=0, performer_status="error", state="failed",
                                   detail="empty output"))
    assert row["output_present"] is False
    assert row["verdict"] == "incompatible"  # not a gateway problem


# --- secret invariant (FR-014) --------------------------------------------


def test_row_carries_no_secret_value():
    # The row builder only sees the run result (no auth); the master key cannot
    # appear in any field. Even if a detail string contained key-like text, the
    # builder must not surface a secret it was never given.
    secret = "sk-litellm-MASTERKEY-should-never-appear"
    row = build_matrix_row(_result())  # result has no secret
    assert secret not in str(row)
    # And nothing in the row equals a credential field name carrying a value.
    assert "MASTERKEY" not in str(row)
