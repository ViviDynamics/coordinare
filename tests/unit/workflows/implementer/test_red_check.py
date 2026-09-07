"""Tests for red_check gate function (spec 167 FR-005).

Mutation check protocol (FR-019): The mutation header documents a change to the
rule that must make the test fail. Run the test with the mutation to verify.
- Mutation: remove "if not changed_test_files" check -> test_red_check_with_no_changed_test_files fails
- Mutation: change "summary.failed > 0" to "summary.failed >= 0" -> test_red_check_fails_when_all_tests_pass fails
- Mutation: remove baseline regression check -> test_red_check_fails_when_baseline_regresses fails
"""

from performer.test_results import TestSummary
from performer.workflows.implementer.cycle import red_check
from performer.workflows.implementer.models import Baseline


class TestRedCheck:
    """Test red_check gate: new tests must fail, baseline must still pass (FR-005)."""

    def test_red_check_passes_when_test_fails_and_baseline_passes(self):
        """Red check passes: changed test failed and baseline still passes."""
        changed_test_files = ["tests/test_signin.py"]
        summary = TestSummary(
            passed=False,
            failed=1,
            test_names_failed=["test_signin"],
            exit_code=1,
        )
        baseline = Baseline(
            test_names=["test_1", "test_2", "test_3", "test_4", "test_5"],
            stack="pytest",
            detected_from="pyproject.toml",
        )

        result = red_check(changed_test_files, summary, baseline)
        assert result is True

    def test_red_check_fails_when_all_tests_pass(self):
        """Red check fails: all tests pass (vacuous test)."""
        changed_test_files = ["tests/test_signin.py"]
        summary = TestSummary(
            passed=True,
            failed=0,
            test_names_passed=["test_1", "test_2", "test_3", "test_4", "test_5", "test_signin"],
            exit_code=0,
        )
        baseline = Baseline(
            test_names=["test_1", "test_2", "test_3", "test_4", "test_5"],
            stack="pytest",
            detected_from="pyproject.toml",
        )

        result = red_check(changed_test_files, summary, baseline)
        assert result is False

    def test_red_check_fails_when_baseline_regresses(self):
        """Red check fails: baseline test failed (regression)."""
        changed_test_files = ["tests/test_signin.py"]
        summary = TestSummary(
            passed=False,
            failed=2,
            test_names_failed=["test_1", "test_signin"],
            exit_code=1,
        )
        baseline = Baseline(
            test_names=["test_1", "test_2", "test_3", "test_4", "test_5"],
            stack="pytest",
            detected_from="pyproject.toml",
        )

        result = red_check(changed_test_files, summary, baseline)
        assert result is False

    def test_red_check_with_no_changed_test_files(self):
        """Red check fails: no changed test files to verify (mutation: remove this check)."""
        changed_test_files = []
        summary = TestSummary(
            passed=False,
            failed=1,
            test_names_failed=["test_signin"],
            exit_code=1,
        )
        baseline = Baseline(
            test_names=["test_1", "test_2", "test_3", "test_4", "test_5"],
            stack="pytest",
            detected_from="pyproject.toml",
        )

        result = red_check(changed_test_files, summary, baseline)
        assert result is False

    def test_red_check_with_multiple_changed_test_files(self):
        """Red check passes: at least one changed test file failed."""
        changed_test_files = ["tests/test_signin.py", "tests/test_logout.py"]
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

        result = red_check(changed_test_files, summary, baseline)
        assert result is True

    def test_red_check_with_count_baseline(self):
        """Red check works with baseline counts instead of test names."""
        changed_test_files = ["tests/test_signin.py"]
        summary = TestSummary(
            passed=False,
            failed=1,
            exit_code=1,
        )
        baseline = Baseline(
            pass_count=5,
            fail_count=0,
            stack="pytest",
            detected_from="pyproject.toml",
        )

        result = red_check(changed_test_files, summary, baseline)
        assert result is True

    def test_red_check_with_more_failures_than_baseline_counts(self):
        """Red check passes: more failures than baseline suggests new tests fail."""
        changed_test_files = ["tests/test_signin.py"]
        summary = TestSummary(
            passed=False,
            failed=2,
            exit_code=1,
        )
        baseline = Baseline(
            pass_count=5,
            fail_count=0,
            stack="pytest",
            detected_from="pyproject.toml",
        )

        result = red_check(changed_test_files, summary, baseline)
        assert result is True


# third live round: the new test imports a function that does not exist yet,
# so the whole module errors at collection and the baseline test in the same
# file cannot run either. That is red for the right reason.
def test_a_collection_error_in_the_changed_test_file_is_red():
    from performer.test_results import parse_test_output
    from performer.workflows.implementer.cycle import red_check
    from performer.workflows.implementer.models import Baseline

    baseline = Baseline(test_names=["tests/test_core.py::test_add"], stack="pytest", detected_from="x")
    out = "ERROR tests/test_core.py - ImportError: cannot import name 'subtract' from 'calc.core'\n1 error in 0.03s\n"
    summary = parse_test_output("pytest", out, 2)
    assert summary.test_names_failed == ["tests/test_core.py"]
    assert red_check(["tests/test_core.py"], summary, baseline) is True


def test_a_collection_error_in_an_unrelated_file_is_not_red():
    from performer.test_results import TestSummary
    from performer.workflows.implementer.cycle import red_check
    from performer.workflows.implementer.models import Baseline

    baseline = Baseline(test_names=["tests/test_other.py::test_x", "tests/test_core.py::test_add"], stack="pytest", detected_from="x")
    summary = TestSummary(passed=False, failed=1, test_names_passed=["tests/test_core.py::test_add"], test_names_failed=["tests/test_other.py"], exit_code=2, raw_tail="")
    assert red_check(["tests/test_core.py"], summary, baseline) is False


def test_a_baseline_test_failing_by_name_outside_the_changed_files_is_a_regression_not_red():
    from performer.test_results import TestSummary
    from performer.workflows.implementer.cycle import red_check
    from performer.workflows.implementer.models import Baseline

    baseline = Baseline(test_names=["tests/test_other.py::test_x", "tests/test_core.py::test_add"], stack="pytest", detected_from="x")
    summary = TestSummary(passed=False, failed=2, test_names_passed=["tests/test_core.py::test_add"],
                          test_names_failed=["tests/test_other.py::test_x", "tests/test_core.py::test_new"], exit_code=1, raw_tail="")
    assert red_check(["tests/test_core.py"], summary, baseline) is False
