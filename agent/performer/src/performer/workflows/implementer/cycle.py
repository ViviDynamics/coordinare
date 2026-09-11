"""Milestone cycle orchestration for the implementer workflow (spec 167).

Per-milestone test-first loop: tests turn, red check, implementation turns,
green check, commit. Pure functions for gate checks (FR-005, FR-006, FR-008, FR-013),
plus orchestration for running one milestone through the cycle.

Gate functions are pure and testable (FR-019): each with its own mutation test
file (test_red_check.py, test_green_check.py, test_scope_violations.py,
test_vacuous_test_check.py, test_no_progress.py, test_next_attempt_allowed.py,
test_test_paths.py) where the mutation check header documents a mutation that
must make the test fail.
"""
from __future__ import annotations

import re
from typing import TYPE_CHECKING

import structlog

from performer.test_results import TestSummary
from performer.workflows.implementer.models import Baseline

if TYPE_CHECKING:
    pass

log = structlog.get_logger(__name__)

__all__ = [
    "is_test_path",
    "red_check",
    "green_check",
    "vacuous_test_check",
    "scope_violations",
    "no_progress_check",
    "changed_test_files",
    "next_attempt_allowed",
]


# The one test-file convention every rule reads (spec 171 FR-003). Two copies of
# this list drifted apart in 167: ``changed_test_files`` knew about Go and Java
# test files and ``scope_violations`` did not, so a foreign Go test survived an
# implementation turn that a Python one would not have.
_TEST_PATTERNS = (
    re.compile(r"test_.*\.(py|rb|js|go|java)$"),
    re.compile(r".*_test\.(py|rb|js|go)$"),
    re.compile(r".*\.test\.(js|ts)$"),
    re.compile(r"spec/.*_spec\.rb$"),
    re.compile(r".*_spec\.(rb)$"),
)


def is_test_path(path: str) -> bool:
    """Whether a repository path is a test file by convention (spec 171 FR-003)."""
    return any(pattern.search(path) for pattern in _TEST_PATTERNS)


def changed_test_files(changed: dict[str, str], runner_kind: str, scope_paths: list[str] | None = None) -> list[str]:
    """Identify changed test files (FR-008).

    Test files are identified by convention: **/test_*, **/*_test.*, etc.
    across any stack (pytest, rspec, jest, minitest).

    Args:
        changed: Dict of {path -> 'added'|'modified'|'deleted'}.
        runner_kind: Stack kind (pytest, rspec, jest, etc.).
        scope_paths: Optional scope paths to filter by.

    Returns:
        List of test file paths that changed.
    """
    return sorted(path for path in changed if is_test_path(path))


def red_check(changed_test_files: list[str], summary: TestSummary, baseline: Baseline) -> bool:
    """Check that red is observed (FR-005): at least one test inside a changed
    test file fails, and the baseline still passes.

    A test file that cannot even import the new code fails at collection;
    pytest reports that as ``ERROR tests/test_x.py`` and none of that file's
    tests run, baseline ones included. That is red for the right reason, so
    baseline tests living in a changed test file are not counted as
    regressions here (the green check requires all of them to pass).
    """
    if not changed_test_files:
        return False
    if summary.passed:
        return False

    baseline_names = set(baseline.test_names or [])

    def in_changed(name: str) -> bool:
        if "::" not in name and "/" not in name:
            # a bare test name (a runner that reports no path) cannot be placed
            # by file; a name the baseline knows is a baseline test, anything
            # else is attributed to the changed files
            return name not in baseline_names
        return any(name == f or name.startswith(f + "::") or name.startswith(f + " ") for f in changed_test_files)

    if summary.test_names_failed is not None or summary.test_names_passed is not None:
        failed = summary.test_names_failed or []
        if not any(in_changed(n) for n in failed):
            return False
        for name in baseline.test_names or []:
            if name in failed and not in_changed(name):
                return False  # a baseline test outside the changed files regressed
        if summary.test_names_passed is not None:
            passed_now = set(summary.test_names_passed)
            for name in baseline.test_names or []:
                if name not in passed_now and name not in failed and not in_changed(name):
                    # vanished for another reason (a collection error elsewhere): not red
                    return False
        return True

    # counts only: more failures than the baseline had
    prior = baseline.fail_count or 0
    return summary.failed is not None and summary.failed > prior


def green_check(summary: TestSummary, milestone_tests: list[str] | None, baseline: Baseline) -> bool:
    """Check that green is observed: milestone tests pass, baseline still passes (FR-006).

    Args:
        summary: Test results after the implementation turn.
        milestone_tests: List of milestone test names (if available).
        baseline: Baseline results.

    Returns:
        True if the green check passes (all tests pass, no regression).
    """
    if not summary.passed:
        return False
    # a failing test inside one of the milestone's test files is red for this
    # milestone even when the runner's overall exit code lies
    if milestone_tests and summary.test_names_failed:
        for name in summary.test_names_failed:
            if any(name.split("::")[0] == f or name.startswith(f) for f in milestone_tests):
                return False

    if baseline.test_names:
        baseline_passed_now = summary.test_names_passed or []
        for baseline_test in baseline.test_names:
            if baseline_test not in baseline_passed_now and summary.test_names_failed:
                if baseline_test in summary.test_names_failed:
                    return False

    if baseline.pass_count is not None and baseline.fail_count is not None:
        if summary.failed is not None and summary.failed > 0:
            return False

    return True


def vacuous_test_check(summary: TestSummary, baseline: Baseline) -> bool:
    """Check for vacuous tests: all tests pass without implementation (FR-005).

    After a tests turn, the red check verifies that at least one test failed
    and baseline still passed. If the red check failed due to all tests passing,
    vacuous_test_check detects this: the milestone is reprompted. If reprompted
    and still all pass, vacuous_test_check fails the milestone as FR-005 requires.

    Args:
        summary: Test run output after tests turn.
        baseline: Baseline state.

    Returns:
        True if tests are vacuous (all pass when they should fail).
    """
    if not summary.passed:
        return False

    if baseline.test_names:
        baseline_passed_now = summary.test_names_passed or []
        for baseline_test in baseline.test_names:
            if baseline_test not in baseline_passed_now and summary.test_names_failed:
                if baseline_test in summary.test_names_failed:
                    return False

    if baseline.pass_count is not None and baseline.fail_count is not None:
        if summary.failed is not None and summary.failed > 0:
            return False

    return True


def foreign_source_path(path: str, own_scopes: list[str], foreign_scopes: list[str]) -> bool:
    """Only another milestone's exclusive declared source ownership is enforced."""
    if not own_scopes or any(scope.strip() in {"", ".", "./"} for scope in own_scopes):
        return False

    def claimed(scopes: list[str]) -> bool:
        for scope in scopes:
            scope = scope.strip().removeprefix("./").rstrip("/")
            if scope and scope != "." and (path == scope or path.startswith(scope + "/")):
                return True
        return False

    return not is_test_path(path) and not claimed(own_scopes) and claimed(foreign_scopes)


def scope_violations(kind: str, changed_paths: dict[str, str], runner_kind: str, scope_paths: list[str] | None = None, docs_tree: str = "docs/", milestone_test_files: list[str] | None = None, foreign_scope_paths: list[str] | None = None) -> list[dict[str, str]]:
    """Detect out-of-scope edits (FR-008).

    Tests turns may only change test files. Implementation turns may not write
    tests at all -- not another milestone's, and not the ones that established
    this milestone's red (#364) -- nor source owned exclusively by another
    milestone. Documentation changes require explicit current-scope ownership.
    Records each reverted path and cause.

    Args:
        kind: Turn kind ("tests", "implement", "repair").
        changed_paths: Dict of {path -> 'added'|'modified'|'deleted'}.
        runner_kind: Stack kind.
        scope_paths: Optional scope paths to filter by.
        docs_tree: Documentation tree prefix (default "docs/").
        milestone_test_files: Tests admitted for the current milestone.
        foreign_scope_paths: Explicit source ownership of other planned milestones.

    Returns:
        List of dicts recording reverted paths and reasons.
    """
    violations = []

    def in_scope(path: str) -> bool:
        for sp in scope_paths or []:
            sp = sp.strip()
            if not sp or sp == ".":
                continue
            if path == sp or path.startswith(sp.rstrip("/") + "/"):
                return True
        return False

    for path in changed_paths.keys():
        # the plan naming a path exempts it from the documentation rule (a chore
        # on README.md is its own scope) but never from the tests-turn rule: a
        # live round implemented the function during the tests turn through an
        # in-scope source file, and red was never observed
        is_doc = path.startswith(docs_tree) or path.startswith("doc/") or path.startswith("README") or path.startswith("CONTRIBUTING") or path.startswith("CHANGELOG")
        if is_doc and not in_scope(path):
            violations.append({
                "path": path,
                "kind": "reverted_doc",
                "reason": "implementation turn must not edit documentation (spec 167 FR-008)"
            })
            continue

        if kind == "implement" and is_test_path(path):
            # #364: an implementation turn edits no tests at all, not even this
            # milestone's own. Writing tests for a LATER milestone pre-empts its
            # tests turn (a live round implemented and tested milestone two
            # during milestone one) -- that was the original rule. Editing THIS
            # milestone's tests is worse and was permitted: the red observation
            # is the evidence that the test tests something, and a turn that can
            # rewrite the test afterwards can weaken it until green passes. CI
            # then runs the weakened test and passes too, so no downstream gate
            # catches it. The test written at red is the specification; if it is
            # wrong, the honest move is to fail the milestone and re-plan, which
            # is what going back to red means here.
            foreign = milestone_test_files is not None and path not in milestone_test_files
            violations.append({
                "path": path,
                "kind": "reverted_foreign_test" if foreign else "reverted_own_test",
                "reason": (
                    "implementation turn must not write another milestone's tests (spec 167 FR-008)"
                    if foreign else
                    "implementation turn must not edit the tests that established red (#364)"
                ),
            })
            continue
        if kind == "implement" and foreign_source_path(path, scope_paths or [], foreign_scope_paths or []):
            violations.append({
                "path": path, "kind": "reverted_foreign_source",
                "reason": "source belongs exclusively to another planned milestone (#279)",
            })
            continue
        if kind == "tests":
            if not is_test_path(path):
                violations.append({
                    "path": path,
                    "kind": "reverted_source",
                    "reason": "tests turn must not edit source files (spec 167 FR-008)"
                })

    return violations


def no_progress_check(prior_failing: list[str], current_failing: list[str]) -> bool:
    """Check for no progress on CI: same checks failing twice (FR-013).

    Args:
        prior_failing: List of failing check names from previous poll.
        current_failing: List of failing check names from current poll.

    Returns:
        True if no progress detected (same checks failing twice).
    """
    if not prior_failing or not current_failing:
        return False

    prior_set = set(prior_failing)
    current_set = set(current_failing)

    return prior_set == current_set


def next_attempt_allowed(kind: str, attempts_so_far: int, budgets) -> bool:
    """Check if the next attempt is within budget (FR-006, FR-010, FR-013).

    Args:
        kind: Turn kind ("tests", "implement", "repair", "quality", "ci").
        attempts_so_far: Number of attempts completed so far.
        budgets: ImplementerBudgets with caps.

    Returns:
        True if another attempt is allowed within the budget.
    """
    if kind == "implement":
        return attempts_so_far < budgets.impl_attempts
    elif kind == "repair_quality":
        return attempts_so_far < budgets.quality_repairs
    elif kind == "repair_ci":
        return attempts_so_far < budgets.ci_repairs
    elif kind == "repair_tests":
        return attempts_so_far < budgets.tests_reprompts

    return True
