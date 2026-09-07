"""Nine fixture scenarios for implementer workflow eval (spec 167 SC-006).

Each fixture defines: a work kind and milestones, a script of fake harness actions,
expected outcomes (status, turn sequence, commits, phases), and edge conditions
(CI failures, quality failures, pending checks).

Deterministic by construction; the live mode swaps the stub for the gateway
and keeps the expectations.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from tests.eval.implementer_scenarios.fakes import Edges, _write


@dataclass(frozen=True)
class Expectation:
    """Scoring expectations for the fixture."""

    status: str
    commit_prefixes_in_order: list[str]
    persona_sequence: list[str]
    min_turns: int
    max_turns: int
    pushed: bool


@dataclass(frozen=True)
class Fixture:
    """A fixture for implementer eval."""

    name: str
    work_kind: str
    milestones: list[dict] | None
    single_turn: bool
    script: dict[str, Callable[[Path, dict], str | None]]
    edges_factory: Callable[[], Edges]
    env: dict
    expect: Expectation


def _milestones(n: int):
    return [
        {
            "goal": f"milestone {i}",
            "scope": f"src/m{i}.py, tests/test_m{i}.py",
            "done_when": f"m{i} tests pass",
        }
        for i in range(n)
    ]


def _tests_turn(repo: Path, brief: dict) -> None:
    i = brief["milestone_index"]
    _write(repo, f"tests/test_m{i}.py", f"EXPECTS src/m{i}.py\n")


def _impl_turn(repo: Path, brief: dict) -> None:
    i = brief["milestone_index"]
    _write(repo, f"src/m{i}.py", f"M{i} = True\n")


def _single_tests_turn(repo: Path, brief: dict) -> None:
    _write(repo, "tests/test_feature.py", "EXPECTS src/feature.py\n")


def _single_impl_turn(repo: Path, brief: dict) -> None:
    _write(repo, "src/feature.py", "FEATURE = 1\n")


SINGLE = Fixture(
    name="single",
    work_kind="feature",
    milestones=[{"goal": "single", "scope": "src/feature.py, tests/test_feature.py", "done_when": "feature works"}],
    single_turn=False,
    script={
        "TESTS": _single_tests_turn,
        "IMPLEMENT": _single_impl_turn,
    },
    edges_factory=lambda: Edges(),
    env={},
    expect=Expectation(
        status="pr_opened",
        commit_prefixes_in_order=["feat(#", "test(#"],
        persona_sequence=["TESTS", "IMPLEMENT"],
        min_turns=2,
        max_turns=4,
        pushed=True,
    ),
)


TWO_MILESTONES = Fixture(
    name="two_milestones",
    work_kind="feature",
    milestones=_milestones(2),
    single_turn=False,
    script={
        "TESTS": _tests_turn,
        "IMPLEMENT": _impl_turn,
    },
    edges_factory=lambda: Edges(),
    env={},
    expect=Expectation(
        status="pr_opened",
        commit_prefixes_in_order=["feat(#", "test(#", "feat(#", "test(#"],
        persona_sequence=["TESTS", "IMPLEMENT", "TESTS", "IMPLEMENT"],
        min_turns=4,
        max_turns=6,
        pushed=True,
    ),
)


VACUOUS = Fixture(
    name="vacuous",
    work_kind="feature",
    milestones=_milestones(2),
    single_turn=False,
    script={
        "TESTS": lambda repo, brief: _write(repo, f"tests/test_m{brief['milestone_index']}.py", "EXPECTS src/base.py\n"),
        "REPAIR_TESTS": lambda repo, brief: _write(repo, f"tests/test_m{brief['milestone_index']}.py", "EXPECTS src/base.py\n"),
    },
    edges_factory=lambda: Edges(),
    env={},
    expect=Expectation(
        status="partial_progress",
        commit_prefixes_in_order=[],
        persona_sequence=["TESTS", "REPAIR_TESTS"],
        min_turns=2,
        max_turns=2,
        pushed=False,
    ),
)


STUCK = Fixture(
    name="stuck",
    work_kind="feature",
    milestones=_milestones(1),
    single_turn=False,
    script={
        "TESTS": _tests_turn,
        "IMPLEMENT": lambda repo, brief: _write(repo, "src/wrong.py", "WRONG = 1\n"),
        "REPAIR_IMPLEMENT": lambda repo, brief: _write(repo, "src/wrong.py", "WRONG = 1\n"),
    },
    edges_factory=lambda: Edges(),
    env={},
    expect=Expectation(
        status="partial_progress",
        commit_prefixes_in_order=[],
        persona_sequence=["TESTS", "IMPLEMENT", "REPAIR_IMPLEMENT", "REPAIR_IMPLEMENT"],
        min_turns=4,
        max_turns=4,
        pushed=False,
    ),
)


CI_PENDING = Fixture(
    name="ci_pending",
    work_kind="feature",
    milestones=_milestones(1),
    single_turn=False,
    script={
        "TESTS": _tests_turn,
        "IMPLEMENT": _impl_turn,
    },
    edges_factory=lambda: Edges(checks=lambda n: [{"name": "Slow", "status": "queued", "conclusion": None}]),
    env={"IMPL_CI_WAIT_S": "0"},
    expect=Expectation(
        status="env_blocked",
        commit_prefixes_in_order=["feat(#", "test(#"],
        persona_sequence=["TESTS", "IMPLEMENT"],
        min_turns=2,
        max_turns=2,
        pushed=True,
    ),
)


BUG = Fixture(
    name="bug",
    work_kind="bug",
    milestones=_milestones(1),
    single_turn=False,
    script={
        "INVESTIGATE": lambda repo, brief: "Suspected cause: base.py returns 1 where 2 is expected (src/base.py:1).",
        "TESTS": _tests_turn,
        "IMPLEMENT": _impl_turn,
    },
    edges_factory=lambda: Edges(),
    env={},
    expect=Expectation(
        status="pr_opened",
        commit_prefixes_in_order=["feat(#", "test(#"],
        persona_sequence=["INVESTIGATE", "TESTS", "IMPLEMENT"],
        min_turns=3,
        max_turns=4,
        pushed=True,
    ),
)


CHORE = Fixture(
    name="chore",
    work_kind="chore",
    milestones=_milestones(1),
    single_turn=False,
    script={
        "CHANGE": lambda repo, brief: _write(repo, "src/config.txt", "changed\n"),
    },
    edges_factory=lambda: Edges(),
    env={},
    expect=Expectation(
        status="pr_opened",
        commit_prefixes_in_order=["chore(#"],
        persona_sequence=["CHANGE"],
        min_turns=1,
        max_turns=1,
        pushed=True,
    ),
)


REFACTOR = Fixture(
    name="refactor",
    work_kind="refactor",
    milestones=_milestones(1),
    single_turn=False,
    script={
        "CHANGE": lambda repo, brief: _write(repo, "src/refactored.py", "REFACTORED = 1\n"),
    },
    edges_factory=lambda: Edges(),
    env={},
    expect=Expectation(
        status="pr_opened",
        commit_prefixes_in_order=["refactor(#"],
        persona_sequence=["CHANGE"],
        min_turns=1,
        max_turns=1,
        pushed=True,
    ),
)


TESTS = Fixture(
    name="tests",
    work_kind="tests",
    milestones=_milestones(1),
    single_turn=False,
    script={
        "TESTS": lambda repo, brief: _write(repo, "tests/test_cover.py", "EXPECTS src/base.py\n"),
    },
    edges_factory=lambda: Edges(),
    env={},
    expect=Expectation(
        status="pr_opened",
        commit_prefixes_in_order=["test(#"],
        persona_sequence=["TESTS"],
        min_turns=1,
        max_turns=1,
        pushed=True,
    ),
)


FIXTURES = [SINGLE, TWO_MILESTONES, VACUOUS, STUCK, CI_PENDING, BUG, CHORE, REFACTOR, TESTS]
