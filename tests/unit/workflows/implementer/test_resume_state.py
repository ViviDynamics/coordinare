"""Tests for resume_state (spec 171 FR-004, FR-005, FR-006).

Mutation check protocol (FR-015): the header names a change to the rule that
must make a test in this file fail. Apply it in the real tree to verify.
- Mutation: drop the prior_paths membership check from the "done" branch
  (``all(p in prior_paths ...)``) -> test_tests_from_the_base_branch_are_not_a_resume fails
- Mutation: drop the prior_paths membership check from the "tests_only" branch
  -> test_source_from_the_base_branch_is_not_a_resume fails
- Mutation: return "tests_only" when the test paths are all present ->
  test_present_but_failing_tests_are_open fails
- Mutation: return "tests_only" for a scope that names no source path ->
  test_a_scope_of_only_tests_is_never_tests_only fails
"""

from performer.workflows.implementer.models import MilestonePlan
from performer.workflows.implementer.resume import resume_state


def _plan(scope: str, index: int = 1) -> MilestonePlan:
    return MilestonePlan(index=index, goal=f"milestone {index}", scope=scope, done_when="it works")


SCOPE = "src/m1.py, tests/test_m1.py"
PASSING = ["tests/test_m1.py::test_0"]


class TestDone:
    def test_prior_tests_present_and_passing_are_done(self):
        state = resume_state(
            _plan(SCOPE),
            prior_paths=frozenset({"src/m1.py", "tests/test_m1.py"}),
            present=frozenset({"src/m1.py", "tests/test_m1.py"}),
            passed=PASSING,
            failed=[],
        )
        assert state == "done"

    def test_tests_from_the_base_branch_are_not_a_resume(self):
        """The brief may name a test file main already has; that is not our work."""
        state = resume_state(
            _plan(SCOPE),
            prior_paths=frozenset(),
            present=frozenset({"src/m1.py", "tests/test_m1.py"}),
            passed=PASSING,
            failed=[],
        )
        assert state == "open"

    def test_present_but_failing_tests_are_open(self):
        state = resume_state(
            _plan(SCOPE),
            prior_paths=frozenset({"src/m1.py", "tests/test_m1.py"}),
            present=frozenset({"src/m1.py", "tests/test_m1.py"}),
            passed=[],
            failed=["tests/test_m1.py::test_0"],
        )
        assert state == "open"


class TestTestsOnly:
    def test_prior_source_without_its_tests_is_tests_only(self):
        """The live shape: milestone zero's turn wrote src/m1.py, the foreign test
        was reverted, so the code is on the branch and the coverage is owed."""
        state = resume_state(
            _plan(SCOPE),
            prior_paths=frozenset({"src/m0.py", "src/m1.py", "tests/test_m0.py"}),
            present=frozenset({"src/m0.py", "src/m1.py", "tests/test_m0.py"}),
            passed=["tests/test_m0.py::test_0"],
            failed=[],
        )
        assert state == "tests_only"

    def test_source_from_the_base_branch_is_not_a_resume(self):
        state = resume_state(
            _plan(SCOPE),
            prior_paths=frozenset({"tests/test_m0.py"}),
            present=frozenset({"src/m1.py", "tests/test_m0.py"}),
            passed=[],
            failed=[],
        )
        assert state == "open"

    def test_absent_source_is_open(self):
        state = resume_state(
            _plan(SCOPE),
            prior_paths=frozenset({"src/m1.py"}),
            present=frozenset(),
            passed=[],
            failed=[],
        )
        assert state == "open"

    def test_a_scope_of_only_tests_is_never_tests_only(self):
        state = resume_state(
            _plan("tests/test_m1.py"),
            prior_paths=frozenset({"tests/test_m1.py"}),
            present=frozenset(),
            passed=[],
            failed=[],
        )
        assert state == "open"


class TestOpen:
    def test_a_scope_naming_no_test_file_is_always_open(self):
        for scope in ("src/m1.py", ".", "src/a.py, src/b.py"):
            state = resume_state(
                _plan(scope),
                prior_paths=frozenset({"src/m1.py", "src/a.py", "src/b.py"}),
                present=frozenset({"src/m1.py", "src/a.py", "src/b.py"}),
                passed=PASSING,
                failed=[],
            )
            assert state == "open", scope

    def test_a_fresh_branch_is_always_open(self):
        state = resume_state(_plan(SCOPE), frozenset(), frozenset(), [], [])
        assert state == "open"
