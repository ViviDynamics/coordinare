"""Resume rules for the implementer workflow (spec 171).

Spec 167 plans from the architect's brief and only from the brief, so every
dispatch of a card rebuilds the same milestone list and runs milestone zero
first, whatever is already on the branch. When an implementation turn spills
out of its milestone (it writes the NEXT milestone's source too, which nothing
reverts), the next milestone can never observe red, the run ends
``partial_progress``, and the following dispatch fails even earlier because
milestone zero's tests now pass at baseline. The card never finishes.

These rules give the stage a memory of its own branch. Its commits carry the
prefixes the driver puts on them (``test(#N):``, ``feat(#N):``, ``fix(#N):``,
``chore(#N):``, ``refactor(#N):``), so the paths those commits touched are the
only evidence needed: a milestone whose tests came from one of them and pass is
done, and a milestone whose SOURCE came from one of them but whose tests are
missing is owed coverage, not a red observation.

Every rule here is pure. Each has its own test file whose header names one
mutation that must make a test fail (FR-015). :func:`present_paths` is the one
filesystem read those rules consume, kept beside them so the plan step and the
red step judge presence the same way.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Literal

from performer.workflows.implementer.cycle import is_test_path
from performer.workflows.implementer.models import MilestonePlan

__all__ = [
    "ResumeState",
    "PRIOR_RUN_PREFIXES",
    "scope_segments",
    "present_paths",
    "_inside_repo",
    "prior_run_paths",
    "declared_test_paths",
    "declared_source_paths",
    "tests_cover",
    "resume_state",
    "apply_resume",
]

ResumeState = Literal["done", "tests_only", "open"]

#: The commit-subject prefixes ``driver._prefix`` and the commit messages use.
PRIOR_RUN_PREFIXES = ("test", "feat", "fix", "chore", "refactor")

_MAX_SCOPE_SEGMENTS = 20


def scope_segments(scope: str | None) -> list[str]:
    """The paths a milestone's scope string names, bounded.

    A scope of ``.`` or an empty scope names nothing: the milestone claims the
    whole tree, which no rule here can judge.
    """
    text = (scope or "").strip()
    if not text or text == ".":
        return []
    return [s.strip() for s in text.replace(";", ",").split(",") if s.strip()][:_MAX_SCOPE_SEGMENTS]


def _inside_repo(path: str) -> bool:
    """A scope segment that stays inside the repository, as ``plan._safe_repo_path``
    reads a repair group's path. A brief is prose: an absolute or traversing
    segment must never be resolved against the workspace."""
    return bool(path) and not path.startswith("/") and ".." not in path.split("/") and "\\" not in path


def present_paths(workspace: Path, paths: list[str]) -> frozenset[str]:
    """Which of *paths* exist in the workspace right now."""
    return frozenset(p for p in paths if _inside_repo(p) and (workspace / p).exists())


def prior_run_paths(entries: list[tuple[str, list[str]]], issue_number: int | None) -> frozenset[str]:
    """The paths THIS card's earlier runs committed on this branch (FR-002).

    ``entries`` are ``(subject, paths)`` per commit over the branch's base, as
    :func:`commits.branch_commit_entries` reads them. Only subjects carrying one
    of :data:`PRIOR_RUN_PREFIXES` for this card's issue number count: another
    card's commits on a shared branch, and a human's own commits, are not
    evidence that a previous implementer run did this milestone. A card with no
    issue number has no prefix to match and so no prior work.
    """
    if not issue_number:
        return frozenset()
    pattern = re.compile(rf"^({'|'.join(PRIOR_RUN_PREFIXES)})\(#{int(issue_number)}\):")
    paths: set[str] = set()
    for subject, entry_paths in entries or []:
        if pattern.match((subject or "").strip()):
            paths.update(p for p in entry_paths if p)
    return frozenset(paths)


def declared_test_paths(scope: str | None, extra_test_patterns: tuple[str, ...] | None = None) -> list[str]:
    """The test files a milestone's scope names (FR-003)."""
    return [p for p in scope_segments(scope) if is_test_path(p, extra_test_patterns)]


def declared_source_paths(scope: str | None, extra_test_patterns: tuple[str, ...] | None = None) -> list[str]:
    """The non-test files a milestone's scope names (FR-003)."""
    return [p for p in scope_segments(scope) if not is_test_path(p, extra_test_patterns)]


def _anchored(name: str, path: str) -> bool:
    """Whether a reported test name belongs to *path*, as ``red_check`` reads names."""
    return name == path or name.startswith(path + "::") or name.startswith(path + " ")


def tests_cover(
    test_paths: list[str],
    present: frozenset[str] | set[str],
    passed: list[str] | None,
    failed: list[str] | None,
) -> bool:
    """Whether a result set shows every named test file present and passing (FR-004).

    Each path must exist, must carry at least one passing test, and must carry
    no failing one. A runner that reports bare names with no path anchors
    nothing, so this returns False and the caller keeps the strict path: the
    rule fails closed rather than assuming coverage it cannot see.
    """
    if not test_paths:
        return False
    passed_names = passed or []
    failed_names = failed or []
    for path in test_paths:
        if path not in present:
            return False
        if any(_anchored(name, path) for name in failed_names):
            return False
        if not any(_anchored(name, path) for name in passed_names):
            return False
    return True


def resume_state(
    milestone: MilestonePlan,
    prior_paths: frozenset[str] | set[str],
    present: frozenset[str] | set[str],
    passed: list[str] | None,
    failed: list[str] | None,
    extra_test_patterns: tuple[str, ...] | None = None,
) -> ResumeState:
    """What a previous run of this card already did for one milestone.

    ``done``: every test file the scope names came from this card's own earlier
    commits, exists, and passes. The milestone needs no turn.

    ``tests_only``: every source file the scope names came from this card's own
    earlier commits and exists, but at least one of its test files does not.
    The code is on the branch and the coverage is owed, so the milestone belongs
    in the tests lane rather than the feature lane, where its tests turn would
    write a test that passes at once and red could never be observed.

    ``open``: everything else, including a scope that names no test file. Only
    ``prior_paths`` membership distinguishes a resume from a vacuous test, so a
    milestone whose tests or source merely happen to exist on the base branch is
    always open and FR-005 applies to it in full.
    """
    test_paths = declared_test_paths(milestone.scope, extra_test_patterns)
    if not test_paths:
        return "open"

    if all(p in prior_paths for p in test_paths) and tests_cover(test_paths, present, passed, failed):
        return "done"

    source_paths = declared_source_paths(milestone.scope, extra_test_patterns)
    if (
        source_paths
        and all(p in prior_paths and p in present for p in source_paths)
        and any(p not in present for p in test_paths)
    ):
        return "tests_only"

    return "open"


def apply_resume(
    plans: list[MilestonePlan],
    states: list[ResumeState],
) -> tuple[list[MilestonePlan], list[MilestonePlan]]:
    """Split a plan into the milestones to skip and the milestones to run (FR-007, FR-009).

    Only a CONTIGUOUS PREFIX of ``done`` milestones is skipped: a done milestone
    that sits behind an open one still runs, because the open one before it may
    change the tree the done one was judged against. A ``tests_only`` milestone
    is re-laned to the tests lane; nothing else about the plan changes.
    """
    skipped: list[MilestonePlan] = []
    remaining: list[MilestonePlan] = []
    in_prefix = True
    for plan, state in zip(plans, states, strict=True):
        if in_prefix and state == "done":
            skipped.append(plan)
            continue
        in_prefix = False
        if state == "tests_only":
            remaining.append(plan.model_copy(update={"lane": "tests", "lane_source": "resume"}))
        else:
            remaining.append(plan)
    return skipped, remaining
