"""Tests for tests_cover (spec 171 FR-004).

Mutation check protocol (FR-015): the header names a change to the rule that
must make a test in this file fail. Apply it in the real tree to verify.
- Mutation: drop the failing-name check (remove the ``any(_anchored(name, path)
  for name in failed_names)`` branch) -> test_a_failing_test_in_the_file_is_not_cover fails
- Mutation: drop the presence check -> test_a_missing_file_is_not_cover fails
- Mutation: return True for an empty test_paths list ->
  test_no_declared_test_paths_is_not_cover fails
- Mutation: relax _anchored to a substring match (``path in name``) ->
  test_a_neighbouring_file_does_not_cover fails
"""

# imported as a module: pytest would collect a bare ``tests_cover`` name as a test
from performer.workflows.implementer import resume


class TestTestsCover:
    """Every named test file must exist, pass, and hold no failure."""

    def test_present_and_passing_is_cover(self):
        assert resume.tests_cover(
            ["tests/test_m1.py"],
            {"tests/test_m1.py"},
            ["tests/test_m1.py::test_0", "tests/test_base.py::test_0"],
            [],
        ) is True

    def test_a_missing_file_is_not_cover(self):
        assert resume.tests_cover(["tests/test_m1.py"], set(), ["tests/test_m1.py::test_0"], []) is False

    def test_a_failing_test_in_the_file_is_not_cover(self):
        assert resume.tests_cover(
            ["tests/test_m1.py"],
            {"tests/test_m1.py"},
            ["tests/test_m1.py::test_0"],
            ["tests/test_m1.py::test_1"],
        ) is False

    def test_a_file_with_no_reported_pass_is_not_cover(self):
        """A collected-but-errored file reports no passing name of its own."""
        assert resume.tests_cover(["tests/test_m1.py"], {"tests/test_m1.py"}, ["tests/test_base.py::test_0"], []) is False

    def test_no_declared_test_paths_is_not_cover(self):
        assert resume.tests_cover([], {"tests/test_m1.py"}, ["tests/test_m1.py::test_0"], []) is False

    def test_every_declared_file_must_be_covered(self):
        assert resume.tests_cover(
            ["tests/test_m1.py", "tests/test_m2.py"],
            {"tests/test_m1.py", "tests/test_m2.py"},
            ["tests/test_m1.py::test_0"],
            [],
        ) is False

    def test_a_neighbouring_file_does_not_cover(self):
        """tests/test_m1.py is not covered by tests/test_m10.py's results."""
        assert resume.tests_cover(
            ["tests/test_m1.py"],
            {"tests/test_m1.py"},
            ["extra/tests/test_m1.py::test_0"],
            [],
        ) is False

    def test_bare_names_anchor_nothing(self):
        """A runner that reports no path cannot show which file passed: fail closed."""
        assert resume.tests_cover(["tests/test_m1.py"], {"tests/test_m1.py"}, ["test_0", "test_1"], []) is False

    def test_none_result_sets_are_not_cover(self):
        assert resume.tests_cover(["tests/test_m1.py"], {"tests/test_m1.py"}, None, None) is False
