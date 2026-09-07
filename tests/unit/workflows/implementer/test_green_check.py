"""Tests for green_check gate function (spec 167 FR-006).

Mutation check protocol (FR-019): The mutation header documents a change to the
rule that must make the test fail. Run the test with the mutation to verify.
- Mutation: change "not summary.passed" to "summary.passed" -> test_green_check_fails_when_milestone_test_fails fails
- Mutation: remove baseline regression check -> test_green_check_fails_when_baseline_regresses fails
"""

from performer.test_results import TestSummary
from performer.workflows.implementer.cycle import green_check
from performer.workflows.implementer.models import Baseline


class TestGreenCheck:
    """Test green_check gate: milestone tests and baseline must all pass (FR-006)."""

    def test_green_check_passes_when_all_pass(self):
        """Green check passes: milestone tests pass and baseline still passes."""
        summary = TestSummary(
            passed=True,
            failed=0,
            test_names_passed=["test_1", "test_2", "test_3", "test_4", "test_signin", "test_logout"],
            exit_code=0,
        )
        milestone_tests = ["test_signin", "test_logout"]
        baseline = Baseline(
            test_names=["test_1", "test_2", "test_3", "test_4"],
            stack="pytest",
            detected_from="pyproject.toml",
        )

        result = green_check(summary, milestone_tests, baseline)
        assert result is True

    def test_green_check_fails_when_milestone_test_fails(self):
        """Green check fails: milestone test failed."""
        summary = TestSummary(
            passed=False,
            failed=1,
            test_names_failed=["test_signin"],
            exit_code=1,
        )
        milestone_tests = ["test_signin", "test_logout"]
        baseline = Baseline(
            test_names=["test_1", "test_2", "test_3", "test_4"],
            stack="pytest",
            detected_from="pyproject.toml",
        )

        result = green_check(summary, milestone_tests, baseline)
        assert result is False

    def test_green_check_fails_when_baseline_regresses(self):
        """Green check fails: baseline test failed (mutation: remove this check to fail)."""
        summary = TestSummary(
            passed=False,
            failed=1,
            test_names_failed=["test_1"],
            exit_code=1,
        )
        milestone_tests = ["test_signin", "test_logout"]
        baseline = Baseline(
            test_names=["test_1", "test_2", "test_3", "test_4"],
            stack="pytest",
            detected_from="pyproject.toml",
        )

        result = green_check(summary, milestone_tests, baseline)
        assert result is False

    def test_green_check_passes_with_no_milestone_tests(self):
        """Green check passes: edge case with no milestone tests."""
        summary = TestSummary(
            passed=True,
            failed=0,
            test_names_passed=["test_1", "test_2", "test_3", "test_4"],
            exit_code=0,
        )
        milestone_tests = None
        baseline = Baseline(
            test_names=["test_1", "test_2", "test_3", "test_4"],
            stack="pytest",
            detected_from="pyproject.toml",
        )

        result = green_check(summary, milestone_tests, baseline)
        assert result is True

    def test_green_check_with_count_baseline(self):
        """Green check works with baseline counts instead of test names."""
        summary = TestSummary(
            passed=True,
            failed=0,
            exit_code=0,
        )
        milestone_tests = ["test_signin", "test_logout"]
        baseline = Baseline(
            pass_count=4,
            fail_count=0,
            stack="pytest",
            detected_from="pyproject.toml",
        )

        result = green_check(summary, milestone_tests, baseline)
        assert result is True

    def test_green_check_baseline_regression_with_counts(self):
        """Green check fails: baseline regression detected via counts."""
        summary = TestSummary(
            passed=False,
            failed=1,
            exit_code=1,
        )
        milestone_tests = ["test_signin"]
        baseline = Baseline(
            pass_count=4,
            fail_count=0,
            stack="pytest",
            detected_from="pyproject.toml",
        )

        result = green_check(summary, milestone_tests, baseline)
        assert result is False


def test_green_check_reads_a_failure_inside_the_milestones_test_files_even_if_exit_code_lies():
    """Review of #268: the milestone_tests argument was declared and ignored."""
    from performer.test_results import TestSummary
    from performer.workflows.implementer.cycle import green_check
    from performer.workflows.implementer.models import Baseline

    baseline = Baseline(test_names=["tests/test_base.py::test_0"], stack="pytest", detected_from="x")
    lying = TestSummary(passed=True, failed=0, test_names_passed=["tests/test_base.py::test_0"], test_names_failed=["tests/test_m0.py::test_0"], exit_code=0, raw_tail="")
    assert green_check(lying, ["tests/test_m0.py"], baseline) is False
    assert green_check(lying, ["tests/other.py"], baseline) is True
