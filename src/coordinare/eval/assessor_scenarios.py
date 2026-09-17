"""Assessor workflow scenario eval (spec 166 SC-006).

Default mode runs the workflow against the three fixture cards with a stubbed
model: deterministic, also exercised by pytest. ``--live`` swaps in the LiteLLM
gateway toolkit (real model, real minutes) and keeps the same scoring; it is a
rate to read, not a CI gate.

    .venv/bin/python -m coordinare.eval.assessor_scenarios
    .venv/bin/python -m coordinare.eval.assessor_scenarios --live --only clear
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from types import SimpleNamespace

from performer.workflows import get_workflow
from performer.workflows.base import WorkflowMetrics
from performer.workflows.toolkit import Toolkit
from tests.eval.assessor_scenarios.fixtures import FIXTURES, Fixture
from tests.eval.assessor_scenarios.scoring import Score, score_run
from tests.eval.assessor_scenarios.stub_model import stub_model_for


async def run_fixture(fixture: Fixture, *, live: bool) -> tuple[Score, dict]:
    """Run the assessor workflow against a fixture.

    Args:
        fixture: The fixture card.
        live: Whether to use the live gateway model instead of the stub.

    Returns:
        (Score, report) tuple.
    """
    if live:
        from coordinare.eval.gateway import _call_model

        model_call = _call_model
    else:
        model_call = stub_model_for(fixture)

    toolkit = Toolkit(
        metrics=WorkflowMetrics(),
        model_call=model_call,
        command_runner=None,
        call_limit=12,
    )

    score = SimpleNamespace(
        title=fixture.title,
        description=fixture.description,
        acceptance_criteria=list(fixture.criteria),
        clarifications=list(fixture.clarifications),
        prior_clarifications=[],
        issue_number=None,
        workflow_env={},
    )

    workflow = get_workflow("assessor")
    result = await workflow.run(
        SimpleNamespace(path="."),
        score,
        toolkit,
    )
    report = result.report
    return score_run(fixture, report, live=live), report


async def run_all(*, live: bool, only: str | None) -> list[Score]:
    """Run all fixtures and return scores.

    Args:
        live: Whether to use the live gateway model.
        only: If set, only run this fixture by name.

    Returns:
        List of Score objects.
    """
    scores: list[Score] = []
    for fixture in FIXTURES:
        if only and fixture.name != only:
            continue
        score, report = await run_fixture(fixture, live=live)
        scores.append(score)

        assessment = report.get("assessment", {})
        metrics = report.get("workflow_metrics", {})
        durations = metrics.get("step_durations_ms", {})

        print(
            f"{fixture.name:8s} {'PASS' if score.passed else 'FAIL'}  "
            f"ready={assessment.get('ready')}  "
            f"questions={len(assessment.get('questions', []))}  "
            f"criteria_source={assessment.get('criteria_source')}  "
            f"steps_ms={json.dumps(durations)}",
        )
        for note in score.notes:
            print(f"         - {note}")

    return scores


def main(argv: list[str] | None = None) -> int:
    """Run the assessor scenario eval.

    Args:
        argv: Command-line arguments.

    Returns:
        0 if all fixtures pass, 1 otherwise.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="use the LiteLLM gateway instead of the stub model")
    parser.add_argument("--only", choices=[f.name for f in FIXTURES])
    args = parser.parse_args(argv)
    scores = asyncio.run(run_all(live=args.live, only=args.only))
    passed = sum(1 for s in scores if s.passed)
    print(f"\n{passed}/{len(scores)} fixtures passed")
    return 0 if passed == len(scores) else 1


if __name__ == "__main__":
    sys.exit(main())
