"""Scoring logic for implementer scenario eval (spec 167 SC-006).

Checks: status, commit prefixes in order, persona sequence, turn caps,
nothing red pushed, no documentation changes, phase durations present.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from tests.eval.implementer_scenarios.fakes import Edges, Harness, _log


@dataclass
class Score:
    """Result of scoring a fixture run."""

    passed: bool
    notes: list[str]


def score_run(
    fixture,
    report: dict,
    repo: Path,
    harness: Harness,
    edges: Edges,
    *,
    live: bool = False,
) -> Score:
    """Score a workflow run against fixture expectations.

    Args:
        fixture: The Fixture definition with expectations.
        report: The ImplementerWorkflow result report.
        repo: The temporary repository path.
        harness: The fake or real turn runner with briefs log.
        edges: The fake outside world with counters.
        live: Whether this is a live run (skips stubbed-persona check).

    Returns:
        Score with passed flag and notes explaining any failures.
    """
    notes = []
    run = report.get("implementer_run", {})

    status = run.get("status")
    if status != fixture.expect.status:
        notes.append(f"status: expected {fixture.expect.status}, got {status}")

    log_msgs = _log(repo)
    for i, prefix in enumerate(fixture.expect.commit_prefixes_in_order):
        if i >= len(log_msgs):
            notes.append(f"commit {i}: expected prefix {prefix}, got nothing (log has {len(log_msgs)} commits)")
            break
        if not log_msgs[i].startswith(prefix):
            notes.append(
                f"commit {i}: expected prefix {prefix}, got {log_msgs[i]}",
            )

    personas = [b["persona_kind"] for b in harness.briefs]
    if not live:
        expected_personas = fixture.expect.persona_sequence
        if personas != expected_personas:
            notes.append(
                f"persona sequence: expected {expected_personas}, got {personas}",
            )

    turn_count = len(harness.briefs)
    if not (fixture.expect.min_turns <= turn_count <= fixture.expect.max_turns):
        notes.append(
            f"turn count: expected {fixture.expect.min_turns}-{fixture.expect.max_turns}, got {turn_count}",
        )

    if fixture.expect.pushed:
        if edges.pushes == 0:
            notes.append("pushed: expected at least one push, got none")
    else:
        if edges.pushes != 0:
            notes.append(f"pushed: expected no pushes, got {edges.pushes}")

    if repo.exists() and (repo / "docs").exists():
        notes.append("documentation: docs/ directory exists, but implementer should not write docs")

    phases = list(run.get("phase_durations_ms", {}).keys())
    if not phases:
        notes.append("phase_durations_ms: expected at least one phase, got none")

    passed = len(notes) == 0
    return Score(passed=passed, notes=notes)
