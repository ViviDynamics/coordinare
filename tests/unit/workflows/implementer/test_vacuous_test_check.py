"""Tests for vacuous_test_check gate function (spec 167 FR-005).

Mutation check protocol (FR-019): The mutation header documents a change to the
rule that must make the test fail. Run the test with the mutation to verify.
- Mutation: invert "if not summary.passed" -> test_vacuous_test_check_false_when_tests_failed fails
- Mutation: remove baseline regression check -> test_vacuous_test_check_false_when_baseline_regresses fails
"""

from performer.test_results import TestSummary
from performer.workflows.implementer.cycle import vacuous_test_check
from performer.workflows.implementer.models import Baseline


class TestVacuousTestCheck:
    """Test vacuous_test_check gate: detects when tests pass without implementation (FR-005)."""

    def test_vacuous_test_check_true_when_all_baseline_pass(self):
        """Vacuous check returns True: baseline all pass but tests should fail."""
        summary = TestSummary(
            passed=True,
            failed=0,
            test_names_passed=["test_1", "test_2", "test_3", "test_4", "test_signin", "test_logout"],
            exit_code=0,
        )
        baseline = Baseline(
            test_names=["test_1", "test_2", "test_3", "test_4"],
            stack="pytest",
            detected_from="pyproject.toml",
        )

        result = vacuous_test_check(summary, baseline)
        assert result is True

    def test_vacuous_test_check_false_when_tests_failed(self):
        """Vacuous check returns False: tests failed as expected."""
        summary = TestSummary(
            passed=False,
            failed=2,
            test_names_failed=["test_signin", "test_logout"],
            exit_code=1,
        )
        baseline = Baseline(
            test_names=["test_1", "test_2", "test_3", "test_4"],
            stack="pytest",
            detected_from="pyproject.toml",
        )

        result = vacuous_test_check(summary, baseline)
        assert result is False

    def test_vacuous_test_check_false_when_baseline_regresses(self):
        """Vacuous check returns False: baseline regressed (mutation: remove this check to fail)."""
        summary = TestSummary(
            passed=False,
            failed=3,
            test_names_failed=["test_1", "test_2", "test_signin"],
            exit_code=1,
        )
        baseline = Baseline(
            test_names=["test_1", "test_2", "test_3", "test_4"],
            stack="pytest",
            detected_from="pyproject.toml",
        )

        result = vacuous_test_check(summary, baseline)
        assert result is False

    def test_vacuous_test_check_with_count_baseline(self):
        """Vacuous check works with baseline counts instead of test names."""
        summary = TestSummary(
            passed=True,
            failed=0,
            exit_code=0,
        )
        baseline = Baseline(
            pass_count=4,
            fail_count=0,
            stack="pytest",
            detected_from="pyproject.toml",
        )

        result = vacuous_test_check(summary, baseline)
        assert result is True
