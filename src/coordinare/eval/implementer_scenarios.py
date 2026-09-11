"""Implementer workflow scenario eval (spec 167 SC-006).

Default mode runs the workflow against nine fixture cards with a scripted
fake harness: deterministic, also exercised by pytest. ``--live`` swaps in the
real performer agent_turn_runner from adapter.build_agent_turn_runner for the
configured backend, against the same temporary repo with real pytest test
files, keeping fake edges for push/PR/checks.

    PYTHONPATH=src:agent/performer/src python -m coordinare.eval.implementer_scenarios
    PYTHONPATH=src:agent/performer/src python -m coordinare.eval.implementer_scenarios --live --only single
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

from performer.workflows import get_workflow
from performer.workflows.base import WorkflowMetrics
from performer.workflows.toolkit import Toolkit

try:
    from tests.eval.implementer_scenarios.fakes import (
        Harness,
        _fake_test_runner,
        _repo,
        stub_model_call,
    )
    from tests.eval.implementer_scenarios.fixtures import FIXTURES
    from tests.eval.implementer_scenarios.scoring import score_run
except ImportError:
    pass


async def run_fixture(fixture, *, live: bool) -> tuple[dict, dict | None]:
    """Run the implementer workflow against a fixture.

    Args:
        fixture: The fixture definition.
        live: Whether to use the real gateway model instead of the stub.

    Returns:
        (report, score_notes) tuple. Notes are None in live mode.
    """
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        repo, _ = _repo(tmp_path)

        if live:
            from performer.config import Settings
            from performer.workflows.adapter import build_agent_turn_runner

            try:
                settings = Settings()
                agent_turn_runner = await build_agent_turn_runner(
                    settings.AGENT_BACKEND,
                    {},
                )
            except Exception as e:
                return {"error": str(e)}, None
        else:
            agent_turn_runner = Harness(repo, fixture.script)

        edges = fixture.edges_factory()

        score = SimpleNamespace(
            title="Add the thing",
            description="The thing should work.",
            acceptance_criteria=["it works"],
            implementation_brief=(
                {"milestones": fixture.milestones or [], "implementer_single_turn": fixture.single_turn}
                if fixture.milestones is not None or fixture.work_kind != "feature"
                else None
            ),
            workflow_env=fixture.env or {},
            test_command="pytest -rA",
            issue_number=7,
            local_test_gate={"enabled": True, "lint_command": "lint"},
            owner_repo=("o", "r"),
            effective_github_token="t",
            repo_url="https://example.invalid/o/r",
            pr_url="",
        )

        if fixture.work_kind != "feature":
            if score.implementation_brief is None:
                score.implementation_brief = {}
            score.implementation_brief["work_kind"] = fixture.work_kind

        toolkit = Toolkit(
            metrics=WorkflowMetrics(),
            model_call=stub_model_call() if not live else None,
            command_runner=_fake_test_runner(repo),
            agent_turn_runner=agent_turn_runner,
            call_limit=64,
        )

        from performer.models import Stand

        stand = Stand(path=repo, branch="feat/x")
        workflow = get_workflow("implementer")
        result = await workflow.run(stand, score, toolkit, ctx_overrides=edges.overrides())

        report = result.report
        score_result = None if live else score_run(fixture, report, repo, agent_turn_runner, edges, live=False)
        return report, score_result


async def run_all(*, live: bool, only: str | None) -> list[dict]:
    """Run all fixtures and return scores.

    Args:
        live: Whether to use the real gateway model.
        only: If set, only run this fixture by name.

    Returns:
        List of score dictionaries.
    """
    results = []
    for fixture in FIXTURES:
        if only and fixture.name != only:
            continue

        report, score_result = await run_fixture(fixture, live=live)

        run = report.get("implementer_run", {})
        status = run.get("status")
        turns = int(run.get("turn_count") or 0)
        phases = list(run.get("phase_durations_ms", {}).items())

        if live:
            print(
                f"{fixture.name:12s} {status:15s} turns={turns:2d} "
                f"phases={json.dumps({k: f'{v}ms' for k, v in phases[:3]})}"
            )
        else:
            passed = score_result.passed if score_result else False
            print(
                f"{fixture.name:12s} {'PASS' if passed else 'FAIL':4s} "
                f"status={status:15s} turns={turns:2d} "
                f"phases={json.dumps({k: v for k, v in phases[:3]})}"
            )
            if score_result and score_result.notes:
                for note in score_result.notes:
                    print(f"             - {note}")

        results.append(score_result if score_result else {"passed": False, "notes": ["live mode"]})

    return results


def main(argv: list[str] | None = None) -> int:
    """Run the implementer scenario eval.

    Args:
        argv: Command-line arguments.

    Returns:
        0 if all fixtures pass, 1 otherwise.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--live",
        action="store_true",
        help="use the real performer backend instead of the stub harness",
    )
    parser.add_argument(
        "--only",
        choices=[f.name for f in FIXTURES],
        help="run only this fixture by name",
    )
    args = parser.parse_args(argv)
    results = asyncio.run(run_all(live=args.live, only=args.only))
    passed = sum(1 for r in results if (r.passed if hasattr(r, "passed") else r.get("passed", True)))
    print(f"\n{passed}/{len(results)} fixtures passed")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
