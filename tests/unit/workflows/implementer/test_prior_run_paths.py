"""Tests for prior_run_paths (spec 171 FR-002).

Mutation check protocol (FR-015): the header names a change to the rule that
must make a test in this file fail. Apply it in the real tree to verify.
- Mutation: drop the issue number from the subject pattern (``\\(#{int(issue_number)}\\):``
  -> ``\\(#\\d+\\):``) -> test_another_cards_commits_are_not_prior_work fails
- Mutation: return the union of ALL entry paths, ignoring the subject ->
  test_a_human_commit_is_not_prior_work fails
"""

from performer.workflows.implementer.resume import prior_run_paths


class TestPriorRunPaths:
    """Only this card's own driver-written commits count as previous-run work."""

    def test_this_cards_commits_contribute_their_paths(self):
        entries = [
            ("feat(#7): milestone 0", ["src/m0.py", "src/m1.py"]),
            ("test(#7): failing tests for milestone 0", ["tests/test_m0.py"]),
        ]
        assert prior_run_paths(entries, 7) == frozenset({"src/m0.py", "src/m1.py", "tests/test_m0.py"})

    def test_every_driver_prefix_counts(self):
        entries = [
            ("test(#7): a", ["a"]),
            ("feat(#7): b", ["b"]),
            ("fix(#7): c", ["c"]),
            ("chore(#7): d", ["d"]),
            ("refactor(#7): e", ["e"]),
        ]
        assert prior_run_paths(entries, 7) == frozenset({"a", "b", "c", "d", "e"})

    def test_another_cards_commits_are_not_prior_work(self):
        """A branch can carry another card's commits; they are not this card's work."""
        entries = [
            ("feat(#8): someone else's milestone", ["src/other.py"]),
            ("feat(#7): milestone 0", ["src/m0.py"]),
        ]
        assert prior_run_paths(entries, 7) == frozenset({"src/m0.py"})

    def test_a_human_commit_is_not_prior_work(self):
        """A subject without a driver prefix was not written by a previous run."""
        entries = [("Merge branch 'main' into feat/x", ["src/hand_written.py"])]
        assert prior_run_paths(entries, 7) == frozenset()

    def test_no_issue_number_means_no_prior_work(self):
        entries = [("feat(#7): milestone 0", ["src/m0.py"])]
        assert prior_run_paths(entries, 0) == frozenset()
        assert prior_run_paths(entries, None) == frozenset()

    def test_empty_history_is_empty(self):
        assert prior_run_paths([], 7) == frozenset()

    def test_a_prefix_inside_the_subject_does_not_count(self):
        """The prefix anchors at the start; a mention mid-subject is prose."""
        entries = [("docs: describe feat(#7): milestone 0", ["docs/x.md"])]
        assert prior_run_paths(entries, 7) == frozenset()
