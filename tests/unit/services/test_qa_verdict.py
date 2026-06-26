"""Spec 120 US1: tests for the coordinare QA evidence-integrity decision function.

Covers every row of the decision table in
``specs/120-qa-evidence-integrity/contracts/qa-report.md``.
"""

from __future__ import annotations

import pytest

from coordinare.services.qa_verdict import (
    classify_qa_verdict,
    qa_unsubstantiated_reason,
)


def _report(**kw) -> dict:
    base = {
        "criteria_checked": 0,
        "criteria_passed": 0,
        "environment_error": None,
        "visual_validation_required": False,
        "visual_evidence": [],
    }
    base.update(kw)
    return base


# --- advance: substantiated / no-scope passes -----------------------------


def test_advances_legitimate_pass_with_evidence():
    report = _report(criteria_checked=5, criteria_passed=4)
    assert classify_qa_verdict("qa_passed", report, False) == "advance"


def test_advances_genuine_no_criteria_scope():
    # checked == 0 means there was nothing to verify — not a false pass.
    report = _report(criteria_checked=0, criteria_passed=0)
    assert classify_qa_verdict("qa_passed", report, False) == "advance"


def test_advances_visual_pass_with_screenshot():
    report = _report(
        criteria_checked=3,
        criteria_passed=3,
        visual_validation_required=True,
        visual_evidence=[{"path_or_url": "artifacts/home.png"}],
    )
    assert classify_qa_verdict("qa_passed", report, False) == "advance"


# --- hold: unsubstantiated + environment signal ---------------------------


def test_holds_zero_criteria_with_environment_error():
    report = _report(criteria_checked=5, criteria_passed=0,
                     environment_error="ruby runtime not installed")
    assert classify_qa_verdict("qa_passed", report, False) == "hold"


def test_holds_zero_criteria_with_env_cache_health_failed_flag():
    report = _report(criteria_checked=5, criteria_passed=0)
    assert classify_qa_verdict("qa_passed", report, True) == "hold"


def test_holds_missing_visual_evidence_with_env_signal():
    report = _report(
        criteria_checked=3,
        criteria_passed=2,
        visual_validation_required=True,
        visual_evidence=[],
        environment_error="no browser available",
    )
    assert classify_qa_verdict("qa_passed", report, False) == "hold"


# --- bounce: unsubstantiated, no environment signal -----------------------


def test_bounces_zero_criteria_without_env_signal():
    report = _report(criteria_checked=5, criteria_passed=0)
    assert classify_qa_verdict("qa_passed", report, False) == "bounce"


def test_bounces_missing_visual_evidence_without_env_signal():
    report = _report(
        criteria_checked=3,
        criteria_passed=2,
        visual_validation_required=True,
        visual_evidence=[{"path_or_url": ""}],  # empty url does not count
    )
    assert classify_qa_verdict("qa_passed", report, False) == "bounce"


# --- non-pass markers are not gated here ----------------------------------


@pytest.mark.parametrize("marker", ["qa_failed", "qa_env_blocked", "error", "blocked"])
def test_non_qa_passed_markers_advance_through(marker):
    assert classify_qa_verdict(marker, None, False) == "advance"


# --- defensive: partial / missing reports do not raise --------------------


def test_missing_report_on_qa_passed_does_not_raise():
    # No report at all → checked defaults to 0 → genuine no-scope → advance.
    assert classify_qa_verdict("qa_passed", None, False) == "advance"


def test_non_numeric_counts_are_coerced_safely():
    report = _report(criteria_checked="five", criteria_passed="zero")
    # Non-numeric coerces to 0/0 → no-scope → advance (never crashes).
    assert classify_qa_verdict("qa_passed", report, False) == "advance"


def test_non_list_visual_evidence_does_not_raise():
    report = _report(criteria_checked=2, criteria_passed=2,
                     visual_validation_required=True, visual_evidence="oops")
    assert classify_qa_verdict("qa_passed", report, False) == "bounce"


# --- reason strings carry names/counts only (FR-007/019) ------------------


def test_reason_zero_criteria_is_names_and_counts_only():
    report = _report(criteria_checked=5, criteria_passed=0,
                     environment_error="SECRET-VALUE-should-not-appear")
    reason = qa_unsubstantiated_reason(report)
    assert reason == "zero_criteria_passed (0 of 5)"
    assert "SECRET" not in reason


def test_reason_missing_visual_evidence():
    report = _report(criteria_checked=2, criteria_passed=2,
                     visual_validation_required=True, visual_evidence=[])
    assert qa_unsubstantiated_reason(report) == "missing_visual_evidence"


def test_reason_none_for_substantiated():
    report = _report(criteria_checked=4, criteria_passed=4)
    assert qa_unsubstantiated_reason(report) is None
