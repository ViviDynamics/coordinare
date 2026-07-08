"""129 (US2): QA visual-capture env resilience — capture-tooling-unavailable is
a recoverable env-block (HOLD), not a hard bounce/fail; never a false-pass."""
from __future__ import annotations

from coordinare.services.qa_verdict import (
    _capture_tooling_unavailable,
    classify_qa_verdict,
)


def _report(**kw):
    base = {"criteria_checked": 3, "criteria_passed": 3, "visual_validation_required": True}
    base.update(kw)
    return base


def test_capture_unavailable_via_explicit_flag() -> None:
    assert _capture_tooling_unavailable({"visual_capture_unavailable": True}) is True


def test_capture_unavailable_via_environment_error_text() -> None:
    assert _capture_tooling_unavailable({"environment_error": "screenshot tooling unavailable"}) is True
    assert _capture_tooling_unavailable({"environment_error": "headless browser failed to start"}) is True


def test_app_failure_text_is_not_capture_unavailable() -> None:
    # FR-012: an app-failure signal must NOT be mislabeled env-blocked.
    assert _capture_tooling_unavailable({"environment_error": "assertion failed: button missing"}) is False
    assert _capture_tooling_unavailable({}) is False


def test_bare_browser_display_keywords_do_not_match_app_failures() -> None:
    # Adversarial finding: bare "browser"/"display"/"capture" collide with real
    # app-failure messages and would hide a real bug behind a recoverable hold.
    for msg in (
        "browser console: assertion failed",
        "display/render error in app",
        "failed to capture user input value",
        "browser reported: element missing",
    ):
        assert _capture_tooling_unavailable({"environment_error": msg}) is False, msg


def test_capture_unavailable_pass_routes_to_hold_when_recovery_enabled() -> None:
    # visual required, no visual evidence, capture tooling unavailable, recovery
    # enabled → HOLD (recoverable, picked up by US1's env-recovery follow-up).
    route = classify_qa_verdict(
        "qa_passed",
        _report(visual_evidence=[], visual_capture_unavailable=True),
        capture_recovery_enabled=True,
    )
    assert route == "hold"


def test_capture_unavailable_bounces_by_default_no_regression() -> None:
    # Recovery gate OFF (default) → prior behavior: capture-unavailable bounces,
    # never a stuck-forever HOLD while the env-recovery pickup is unwired.
    route = classify_qa_verdict(
        "qa_passed",
        _report(visual_evidence=[], visual_capture_unavailable=True),
    )
    assert route == "bounce"


def test_missing_visual_without_capture_signal_still_bounces() -> None:
    # No capture-unavailable signal + no env signal → still a bounce (unchanged).
    route = classify_qa_verdict(
        "qa_passed",
        _report(visual_evidence=[]),
        capture_recovery_enabled=True,
    )
    assert route == "bounce"


def test_substantiated_pass_still_advances() -> None:
    route = classify_qa_verdict(
        "qa_passed",
        _report(visual_evidence=[{"path_or_url": "https://github.com/o/r/raw/qa-assets/x.png"}]),
    )
    assert route == "advance"


def test_zero_criteria_passed_never_advances_even_if_capture_unavailable() -> None:
    # spec-120 floor intact: 0-of-N passed is unsubstantiated regardless.
    route = classify_qa_verdict(
        "qa_passed",
        _report(criteria_checked=5, criteria_passed=0, visual_evidence=[], visual_capture_unavailable=True),
    )
    assert route != "advance"


def test_non_qa_passed_unchanged() -> None:
    assert classify_qa_verdict("qa_failed", _report()) == "advance"
