"""Tests for no_progress_check gate function (spec 167 FR-013).

Mutation check protocol (FR-019): The mutation header documents a change to the
rule that must make the test fail. Run the test with the mutation to verify.
- Mutation: change "==" to "!=" in set comparison -> test_no_progress_check_true_when_same fails
"""

from performer.workflows.implementer.cycle import no_progress_check


class TestNoProgressCheck:
    """Test no_progress_check gate: detects when CI repairs make no forward progress (FR-013)."""

    def test_no_progress_check_true_when_same(self):
        """No progress check returns True: same checks failing twice (mutation: change == to !=)."""
        prior_failing = ["Lint", "Test"]
        current_failing = ["Lint", "Test"]

        result = no_progress_check(prior_failing, current_failing)
        assert result is True

    def test_no_progress_check_false_when_checks_differ(self):
        """No progress check returns False: different checks, progress made."""
        prior_failing = ["Lint", "Test"]
        current_failing = ["Lint"]

        result = no_progress_check(prior_failing, current_failing)
        assert result is False

    def test_no_progress_check_false_when_new_check_appears(self):
        """No progress check returns False: new check appeared."""
        prior_failing = ["Lint"]
        current_failing = ["Lint", "Build"]

        result = no_progress_check(prior_failing, current_failing)
        assert result is False

    def test_no_progress_check_false_when_all_pass(self):
        """No progress check returns False: all checks now pass."""
        prior_failing = ["Lint", "Test"]
        current_failing = []

        result = no_progress_check(prior_failing, current_failing)
        assert result is False

    def test_no_progress_check_with_empty_prior(self):
        """No progress check returns False: prior was empty."""
        prior_failing = []
        current_failing = ["Lint"]

        result = no_progress_check(prior_failing, current_failing)
        assert result is False

    def test_no_progress_check_true_with_reordered_same_checks(self):
        """No progress check returns True: same checks in different order."""
        prior_failing = ["Lint", "Test", "Build"]
        current_failing = ["Build", "Lint", "Test"]

        result = no_progress_check(prior_failing, current_failing)
        assert result is True

    def test_no_progress_check_true_with_duplicates(self):
        """No progress check returns True: duplicates in lists (as set equality)."""
        prior_failing = ["Lint", "Lint", "Test"]
        current_failing = ["Lint", "Test"]

        result = no_progress_check(prior_failing, current_failing)
        assert result is True
