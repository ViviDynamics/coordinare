"""Reviewer workflow scenario eval (spec 169 SC-001).

Default mode runs the workflow against the fixture pull requests with a
stubbed model and a fake command runner: deterministic, also exercised by
pytest. ``--live`` swaps in the LiteLLM gateway model (real model, real
minutes) and keeps the same scoring with the fixture's accepted verdicts; it
is a rate to read, not a CI gate. Neither mode posts to GitHub.

    PYTHONPATH=src:agent/performer/src .venv/bin/python -m coordinare.eval.reviewer_scenarios
    PYTHONPATH=src:agent/performer/src .venv/bin/python -m coordinare.eval.reviewer_scenarios --live --only findings
"""
from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

from performer.workflows.base import WorkflowMetrics
from performer.workflows.reviewer import ReviewerWorkflow
from performer.workflows.toolkit import Toolkit
from tests.eval.reviewer_scenarios.fixtures import FIXTURES, Fixture
from tests.eval.reviewer_scenarios.scoring import Score, score_run
from tests.eval.reviewer_scenarios.stub_model import (
    RecordingPoster,
    stub_model_for,
    stub_runner_for,
)

_GIT = shutil.which("git") or "git"  # 440: resolve the real git path once; fallback preserves prior behavior


def _live_workspace(fixture: Fixture) -> Path:
    """A real git repo holding the fixture's files, so live survey commands have something to read."""
    root = Path(tempfile.mkdtemp(prefix="reviewer-eval-"))
    (root / "src").mkdir()
    (root / "tests").mkdir()
    (root / "src" / "calc.py").write_text("def add(a, b):\n    return a + b\n\n\ndef div(a, b):\n    return a / b\n\n\n")
    (root / "tests" / "test_calc.py").write_text("from src.calc import add, div\n\n\ndef test_add():\n    assert add(1, 2) == 3\n\n\ndef test_div():\n    assert div(4, 2) == 2\n")
    if "extra.py" in fixture.diff:
        (root / "src" / "extra.py").write_text("import os\nX = 1\n")
    if "docs/usage.md" in fixture.diff:
        (root / "docs").mkdir()
        (root / "docs" / "usage.md").write_text("# Usage\nCall div.\n")
    subprocess.run([_GIT, "init", "-q"], cwd=root, check=True, timeout=120)
    subprocess.run([_GIT, "-c", "user.email=e@x", "-c", "user.name=eval", "add", "-A"], cwd=root, check=True, timeout=120)
    subprocess.run([_GIT, "-c", "user.email=e@x", "-c", "user.name=eval", "commit", "-qm", "fixture"], cwd=root, check=True, timeout=120)
    return root


async def run_fixture(fixture: Fixture, *, live: bool, workspace: Path | None = None) -> tuple[Score, dict]:
    poster = RecordingPoster()
    if live:
        from coordinare.eval.gateway import _call_model, _run_command

        model_call, runner = _call_model, _run_command
        workspace = workspace or _live_workspace(fixture)
    else:
        model_call, runner = stub_model_for(fixture), stub_runner_for(fixture)
        workspace = workspace or Path(".")
    toolkit = Toolkit(metrics=WorkflowMetrics(), model_call=model_call, command_runner=runner, call_limit=12)
    score = SimpleNamespace(
        title="Add div", description="Divide two numbers", pr_diff=fixture.diff, relay_feedback=list(fixture.relay_feedback),
        implementation_brief=dict(fixture.brief), pr_url="https://github.com/eval/repo/pull/1", owner_repo=("eval", "repo"),
        effective_github_token="unused", backend="eval", model="stub" if not live else "gateway", workflow_env={},
    )
    result = await ReviewerWorkflow(poster=poster).run(SimpleNamespace(path=workspace), score, toolkit)
    return score_run(fixture, result.report, poster.reviews, live=live), result.report


async def run_all(*, live: bool, only: str | None) -> list[Score]:
    scores: list[Score] = []
    for fixture in FIXTURES:
        if only and fixture.name != only:
            continue
        score, report = await run_fixture(fixture, live=live)
        scores.append(score)
        review = report.get("review", {})
        durations = (report.get("workflow_metrics") or {}).get("step_durations_ms", {})
        print(
            f"{fixture.name:20s} {'PASS' if score.passed else 'FAIL'}  verdict={review.get('verdict')}  "
            f"findings={len(review.get('findings') or [])}  dropped={len(review.get('findings_dropped') or [])}  "
            f"coverage_pass={review.get('coverage_pass_ran')}  steps_ms={json.dumps(durations)}",
        )
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
