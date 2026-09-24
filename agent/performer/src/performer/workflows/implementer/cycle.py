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
#
# 409: every pattern is anchored at a path boundary. Unanchored, "test_" and
# "_test." matched anywhere in the path, so greatest_common.py and
# latest_run.go were classed as tests: implementation turns had their real
# source reverted, and tests turns lost their own files. The set covers the
# conventions of the stacks the detectors can select; symphonies with their
# own convention extend it via local_test_gate.test_path_patterns.
_TEST_PATTERNS = (
    re.compile(r"(^|/)test_[^/]*\.(py|rb|js|go|java)$"),  # test_foo.py (filename only)
    re.compile(r"(^|/)[^/]*_test\.(py|rb|js|go)$"),  # foo_test.go
    re.compile(r"(^|/)[^/]*\.test\.(js|jsx|ts|tsx)$"),  # foo.test.tsx
    re.compile(r"(^|/)[^/]*\.spec\.(js|jsx|ts|tsx)$"),  # Button.spec.tsx
    re.compile(r"(^|/)spec/[^/]*_spec\.rb$"),  # spec/models/user_spec.rb
    re.compile(r"(^|/)[^/]*_spec\.rb$"),  # user_spec.rb
    re.compile(r"(^|/)src/test/java/[^/]*\.java$"),  # maven/gradle test tree
    re.compile(r"(^|/)[A-Z][^/]*Tests?\.java$"),  # ExampleTest.java, ExampleTests.java
    re.compile(r"(^|/)[A-Z][^/]*Tests\.cs$"),  # ExampleTests.cs
    re.compile(r"(^|/)tests/[^/]+\.rs$"),  # cargo integration tests
)


def is_test_path(path: str, extra_patterns: list[str] | tuple[str, ...] | None = None) -> bool:
    """Whether a repository path is a test file by convention (spec 171 FR-003).

    ``extra_patterns`` (409) are per-symphony regex sources compiled onto the
    built-in conventions: a symphony whose tests follow a convention the
    built-ins do not know declares them in
    ``local_test_gate.test_path_patterns``.
    """
    patterns = _TEST_PATTERNS
    if extra_patterns:
        extra = []
        for pattern in extra_patterns:
            if not pattern:
                continue
            try:
                extra.append(re.compile(pattern))
            except re.error:
                # Config validates these at load; a synthetic payload that
                # carries a malformed pattern must not abort the turn, so the
                # invalid extension is skipped and the built-ins still apply.
                log.warning("cycle.invalid_test_path_pattern", pattern=pattern)
        if extra:
            patterns = (*_TEST_PATTERNS, *extra)
    return any(pattern.search(path) for pattern in patterns)


def changed_test_files(
    changed: dict[str, str],
    runner_kind: str,
    scope_paths: list[str] | None = None,
    extra_test_patterns: list[str] | tuple[str, ...] | None = None,
) -> list[str]:
    """Identify changed test files (FR-008).

    Test files are identified by convention: **/test_*, **/*_test.*, etc.
    across any stack (pytest, rspec, jest, minitest).

    Args:
        changed: Dict of {path -> 'added'|'modified'|'deleted'}.
        runner_kind: Stack kind (pytest, rspec, jest, etc.).
        scope_paths: Optional scope paths to filter by.
        extra_test_patterns: Optional per-symphony regex sources (409).

    Returns:
        List of test file paths that changed.
    """
    return sorted(
        path for path in changed if is_test_path(path, extra_test_patterns)
    )




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


def foreign_source_path(
    path: str, own_scopes: list[str], foreign_scopes: list[str],
    extra_test_patterns: list[str] | tuple[str, ...] | None = None,
) -> bool:
    """Only another milestone's exclusive declared source ownership is enforced."""
    if not own_scopes or any(scope.strip() in {"", ".", "./"} for scope in own_scopes):
        return False

    def claimed(scopes: list[str]) -> bool:
        for scope in scopes:
            scope = scope.strip().removeprefix("./").rstrip("/")
            if scope and scope != "." and (path == scope or path.startswith(scope + "/")):
                return True
        return False

    return (
        not is_test_path(path, extra_test_patterns)
        and not claimed(own_scopes)
        and claimed(foreign_scopes)
    )


def scope_violations(
    kind: str, changed_paths: dict[str, str], runner_kind: str,
    scope_paths: list[str] | None = None, docs_tree: str = "docs/",
    milestone_test_files: list[str] | None = None,
    foreign_scope_paths: list[str] | None = None,
    extra_test_patterns: list[str] | tuple[str, ...] | None = None,
    allow_docs: bool = False,
) -> list[dict[str, str]]:
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
        extra_test_patterns: Optional per-symphony regex sources (409) extending
            the built-in test-file conventions.
        allow_docs: When True (the 410 docs lane), documentation paths are in
            scope for this turn; the doc-revert rule is suspended. The tests-turn
            rule is NOT suspended: a docs card still writes no tests.

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
        is_doc = path.startswith((docs_tree, "doc/", "README", "CONTRIBUTING", "CHANGELOG"))
        if is_doc and not allow_docs and not in_scope(path):
            violations.append({
                "path": path,
                "kind": "reverted_doc",
                "reason": "implementation turn must not edit documentation (spec 167 FR-008)",
            })
            continue

        if kind == "implement" and is_test_path(path, extra_test_patterns):
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
        if kind == "implement" and foreign_source_path(
            path, scope_paths or [], foreign_scope_paths or [], extra_test_patterns,
        ):
            violations.append({
                "path": path, "kind": "reverted_foreign_source",
                "reason": "source belongs exclusively to another planned milestone (#279)",
            })
            continue
        if kind == "tests":
            if not is_test_path(path, extra_test_patterns):
                violations.append({
                    "path": path,
                    "kind": "reverted_source",
                    "reason": "tests turn must not edit source files (spec 167 FR-008)",
                })

    return violations


def no_progress_check(prior_failing: list[str], current_failing: list[str]) -> bool:
    """Check for no progress on CI: the same failures twice (FR-013).

    Entries may carry a failure ground after the first colon ("name: ground");
    two polls failing with the same names but a different ground is progress.

    Args:
        prior_failing: Failing check signatures from the previous poll.
        current_failing: Failing check signatures from the current poll.

    Returns:
        True if no progress detected (same checks failing the same way twice).
    """
    if not prior_failing or not current_failing:
        return False

    def _grounded(names: list[str]) -> dict[str, str | None]:
        out: dict[str, str | None] = {}
        for entry in names:
            name, _, ground = str(entry).partition(":")
            out[name.strip()] = ground.strip() if ground else None
        return out

    return _grounded(prior_failing) == _grounded(current_failing)


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
    if kind == "repair_quality":
        return attempts_so_far < budgets.quality_repairs
    if kind == "repair_ci":
        return attempts_so_far < budgets.ci_repairs
    if kind == "repair_tests":
        return attempts_so_far < budgets.tests_reprompts

    return True
