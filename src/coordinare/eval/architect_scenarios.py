"""Architect workflow scenario eval (spec 165 SC-004).

Default mode runs the workflow against the three fixture cards with a stubbed
model in a temporary repository: deterministic, also exercised by pytest.
``--live`` swaps in the LiteLLM gateway toolkit (real model, real minutes) and
keeps the same scoring; it is a rate to read, not a CI gate.

    .venv/bin/python -m coordinare.eval.architect_scenarios
    .venv/bin/python -m coordinare.eval.architect_scenarios --live --only schema
"""
from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

from performer.workflows import get_workflow
from performer.workflows.base import WorkflowMetrics
from performer.workflows.toolkit import Toolkit
from tests.eval.architect_scenarios.fixtures import FIXTURES, Fixture
from tests.eval.architect_scenarios.scoring import Score, score_run
from tests.eval.architect_scenarios.stub_model import stub_model_for


def materialise_repo(fixture: Fixture, root: Path) -> Path:
    repo = root / fixture.name
    for rel, content in fixture.repo_files.items():
        path = repo / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=e@x", "-c", "user.name=eval", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=e@x", "-c", "user.name=eval", "commit", "-q", "-m", "fixture"], cwd=repo, check=True)
    return repo


async def _local_runner(cmd: str, cwd, timeout_s: int) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_shell(
        cmd, cwd=str(cwd), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except TimeoutError:
        proc.kill()
        return 124, "timed out"
    return proc.returncode or 0, out.decode(errors="replace")


def _score_for(fixture: Fixture):
    return SimpleNamespace(
        title=fixture.title, description=fixture.description, acceptance_criteria=list(fixture.criteria),
        clarifications=[], issue_number=None, workflow_env={},
    )


async def run_fixture(fixture: Fixture, *, live: bool, root: Path) -> tuple[Score, dict]:
    repo = materialise_repo(fixture, root)
    ran: list[str] = []

    async def recording_runner(cmd, cwd, timeout_s):
        ran.append(cmd)
        return await _local_runner(cmd, cwd, timeout_s)

    if live:
        from coordinare.eval.gateway import _call_model  # the same gateway path the QA eval uses

        model_call = _call_model
    else:
        model_call = stub_model_for(fixture)
    toolkit = Toolkit(metrics=WorkflowMetrics(), model_call=model_call, command_runner=recording_runner, call_limit=12)
    workflow = get_workflow("architect")
    result = await workflow.run(SimpleNamespace(path=repo), _score_for(fixture), toolkit)
    return score_run(fixture, result.report, ran, live=live), result.report


async def run_all(*, live: bool, only: str | None) -> list[Score]:
    scores: list[Score] = []
    with tempfile.TemporaryDirectory(prefix="architect-eval-") as tmp:
        for fixture in FIXTURES:
            if only and fixture.name != only:
                continue
            score, report = await run_fixture(fixture, live=live, root=Path(tmp))
            scores.append(score)
            durations = (report.get("workflow_metrics") or {}).get("step_durations_ms", {})
            print(f"{fixture.name:8s} {'PASS' if score.passed else 'FAIL'}  size={report.get('size')}  "
                  f"milestones={len(report['blueprint'].get('milestones', []))}  docs={len(report['blueprint'].get('docs', []))}  "
                  f"steps_ms={json.dumps(durations)}")
            for note in score.notes:
                print(f"         - {note}")
    return scores


def main(argv: list[str] | None = None) -> int:
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
