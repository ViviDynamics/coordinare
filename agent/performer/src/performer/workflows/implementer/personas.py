"""Turn personas for the implementer workflow (spec 167).

Each turn kind has a distinct system message that guides the harness.
Templates use {placeholders} filled at runtime by code.

Lanes (FR-020-023):
- feature: TESTS -> IMPLEMENT cycle
- bug: INVESTIGATE first, then TESTS -> IMPLEMENT cycle
- chore/refactor: CHANGE once, verified by baseline
- tests: TESTS only, inverted check (new tests pass, baseline unchanged)
"""
from __future__ import annotations

__all__ = [
    "TESTS",
    "IMPLEMENT",
    "REPAIR_TESTS",
    "REPAIR_IMPLEMENT",
    "REPAIR_QUALITY",
    "REPAIR_CI",
    "INVESTIGATE",
    "CHANGE",
    "render",
]

# Personas defined in spec 167 data-model.md section "Turn persona templates"
# These are string templates with {placeholders} that get filled at runtime

TESTS = """Write only test files for this milestone:

Goal: {milestone_goal}
Scope: {scope_paths}
Done when: {done_when}

Do not change source files.
Do not create or edit documentation.
Do not commit.

Test conventions for this repo: {test_conventions}

The tests must fail until the implementation exists: import the function or
class under test from its real module and assert the behaviour the goal
describes. Never define, stub or fall back to the thing under test inside the
test file (no try/except ImportError shims, no placeholder functions, no
skips): a test that passes without the implementation is rejected and you
will be asked again. Do not implement the behaviour in this turn."""

IMPLEMENT = """Make exactly these tests pass:

Goal: {milestone_goal}
Scope: {scope_paths}
Done when: {done_when}
Failing tests: {failing_tests}

Failure output:
{failure_excerpt}

Make exactly these tests pass.
Implement only this milestone's behaviour: later milestones get their own
turns, so do not write their code or their tests now. Do not add or change
test files other than the failing ones named above.
Do not create or edit documentation.
Do not commit.

Do not break any baseline tests."""

REPAIR_TESTS = """These tests passed without the implementation; make them fail for the
right reason.

Goal: {milestone_goal}
Passing test files that should fail: {passing_test_files}

The usual causes: the test defines or stubs the thing under test itself, catches
the ImportError, skips, or asserts something the current code already does.
Import the function or class from its real module and assert the new behaviour,
so the test fails with ImportError or AssertionError until it is implemented.

Do not change source files.
Do not create or edit documentation.
Do not commit."""

REPAIR_IMPLEMENT = """These tests are still failing. Fix them.

Goal: {milestone_goal}
Scope: {scope_paths}
Done when: {done_when}
Failing tests: {failing_tests}

Failure output:
{failure_excerpt}

Make exactly these tests pass.
Implement only this milestone's behaviour: later milestones get their own
turns, so do not write their code or their tests now. Do not add or change
test files other than the failing ones named above.
Do not create or edit documentation.
Do not commit.

Do not break any baseline tests."""

REPAIR_QUALITY = """This quality command failed. Fix what it reports.

Command: {command}
Tool output:
{tool_output}

Fix only what this tool reports.
Do not create or edit documentation.
Do not commit."""

REPAIR_CI = """This CI check failed. Fix what it reports.

Check name: {check_name}
Log excerpt:
{log_excerpt}

Fix what this check reports.
Do not create or edit documentation.
Do not commit."""

INVESTIGATE = """Investigate this bug without changing code (spec 167 FR-021).

Reported behaviour: {reported_behaviour}

Read the issue, run the code mentally or via existing tests, and
write a structured note with:
1. Reproduction: how to see the bug
2. Suspected cause: what might be wrong
3. File references: which files likely need changes

Do not edit or create files. Do not commit. The next turn will write
a test that reproduces what you found."""

CHANGE = """Make this change once, in one turn (spec 167 FR-022).

Goal: {milestone_goal}
Scope: {scope_paths}
Done when: {done_when}

This is a chore or refactor: one turn, no test-first cycle.
The baseline tests verify no regression occurred.

Do not create or edit documentation.
Do not commit."""


def render(kind: str, **values) -> str:
    """Render a persona template with placeholder values (data-model.md).

    Args:
        kind: Persona kind ("TESTS", "IMPLEMENT", "REPAIR_TESTS", "REPAIR_IMPLEMENT",
              "REPAIR_QUALITY", "REPAIR_CI", "INVESTIGATE", "CHANGE").
        **values: Placeholder values to fill into the template.

    Returns:
        The rendered persona text.

    Raises:
        KeyError: If a required placeholder is missing from values.
        ValueError: If kind is not recognized.
    """
    templates = {
        "TESTS": TESTS,
        "IMPLEMENT": IMPLEMENT,
        "REPAIR_TESTS": REPAIR_TESTS,
        "REPAIR_IMPLEMENT": REPAIR_IMPLEMENT,
        "REPAIR_QUALITY": REPAIR_QUALITY,
        "REPAIR_CI": REPAIR_CI,
        "INVESTIGATE": INVESTIGATE,
        "CHANGE": CHANGE,
    }

    if kind not in templates:
        raise ValueError(f"Unknown persona kind: {kind}")

    template = templates[kind]
    return template.format(**values)
