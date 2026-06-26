"""Spec 120 US1: the env-limited advisory pass must rest on positive verification.

Tests the pure decision helper ``_qa_env_limited_without_verification`` that the
QA terminal path uses to route a zero-criteria env-limited run to
``qa_env_blocked`` instead of the advisory ``qa_passed``.
"""

from __future__ import annotations

import pytest

from performer.main import _qa_env_limited_without_verification


def test_zero_criteria_under_env_limit_is_blocked():
    # checked > 0, passed == 0, env-limited, claimed pass → block (not advisory pass).
    assert _qa_env_limited_without_verification(
        qa_passed_flag=True,
        env_limited=True,
        criteria_checked=5,
        criteria_passed=0,
    ) is True


def test_partial_pass_under_env_limit_is_advisory_pass():
    # >=1 criterion genuinely passed → advisory pass retained (not blocked).
    assert _qa_env_limited_without_verification(
        qa_passed_flag=True,
        env_limited=True,
        criteria_checked=5,
        criteria_passed=1,
    ) is False


def test_not_env_limited_is_not_blocked_here():
    # A non-env-limited run is handled by the normal pass/fail paths.
    assert _qa_env_limited_without_verification(
        qa_passed_flag=True,
        env_limited=False,
        criteria_checked=5,
        criteria_passed=0,
    ) is False


def test_not_passed_flag_is_not_blocked_here():
    # Already failing on a real defect → not this helper's concern.
    assert _qa_env_limited_without_verification(
        qa_passed_flag=False,
        env_limited=True,
        criteria_checked=5,
        criteria_passed=0,
    ) is False


def test_no_criteria_scope_is_not_blocked():
    # checked == 0 → genuine no-scope, not a false pass.
    assert _qa_env_limited_without_verification(
        qa_passed_flag=True,
        env_limited=True,
        criteria_checked=0,
        criteria_passed=0,
    ) is False


@pytest.mark.parametrize("checked,passed", [("five", "zero"), (None, None), ("", "")])
def test_non_numeric_counts_coerce_without_raising(checked, passed):
    # Non-numeric coerces to 0/0 → no-scope → not blocked (never raises).
    assert _qa_env_limited_without_verification(
        qa_passed_flag=True,
        env_limited=True,
        criteria_checked=checked,
        criteria_passed=passed,
    ) is False


def test_non_numeric_checked_with_zero_passed_is_safe():
    assert _qa_env_limited_without_verification(
        qa_passed_flag=True,
        env_limited=True,
        criteria_checked="3",  # numeric string coerces to 3
        criteria_passed=0,
    ) is True
