"""Security workflow scenario eval (spec 170 SC-006).

Default mode runs the workflow against the fixture pull requests with a stubbed
model, a fake scanner runner and a fake command runner: deterministic, also
exercised by pytest. ``--live`` swaps in the LiteLLM gateway model and the real
semgrep and bandit over a temporary repository holding the fixture's files, and
keeps the same scoring with the fixture's accepted verdicts; it is a rate to
read, not a CI gate. Neither mode posts to GitHub.

    PYTHONPATH=src:agent/performer/src .venv/bin/python -m coordinare.eval.security_scenarios
    PYTHONPATH=src:agent/performer/src .venv/bin/python -m coordinare.eval.security_scenarios --live --only injection
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

from performer.workflows.base import WorkflowMetrics
from performer.workflows.security import SecurityWorkflow
from performer.workflows.security.scanner import default_runner
from performer.workflows.toolkit import Toolkit
from tests.eval.security_scenarios.fixtures import FIXTURES, Fixture
from tests.eval.security_scenarios.scoring import Score, score_run
from tests.eval.security_scenarios.stub_model import (
    RecordingPoster,
    fake_scanner_for,
    stub_command_runner,
    stub_model_for,
)


def _live_workspace(fixture: Fixture) -> Path:
    """A real git repo holding the fixture's files so the tools and the survey have something to read."""
    root = Path(tempfile.mkdtemp(prefix="security-eval-"))
    for rel, text in fixture.files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)
    git = ["git", "-c", "user.email=e@x", "-c", "user.name=eval"]
    subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    subprocess.run([*git, "add", "-A"], cwd=root, check=True)
    subprocess.run([*git, "commit", "-qm", "fixture"], cwd=root, check=True)
    return root


async def run_fixture(fixture: Fixture, *, live: bool, workspace: Path | None = None) -> tuple[Score, dict]:
    poster = RecordingPoster()
    if live:
        from coordinare.eval.gateway import _call_model, _run_command

        model_call, runner, scan_runner = _call_model, _run_command, default_runner
        workspace = workspace or _live_workspace(fixture)
        model_calls = {"i": 0}

        async def counting(persona, content, max_tokens):
            model_calls["i"] += 1
            return await model_call(persona, content, max_tokens)

        model_call = counting
    else:
        stub = stub_model_for(fixture)
        model_call, runner, scan_runner, model_calls = stub, stub_command_runner(), fake_scanner_for(fixture), stub.state
        workspace = workspace or Path(".")
    toolkit = Toolkit(metrics=WorkflowMetrics(), model_call=model_call, command_runner=runner, call_limit=12)
    score = SimpleNamespace(
        title=f"eval {fixture.name}", description="Security eval fixture", pr_diff=fixture.diff, implementation_brief=dict(fixture.brief),
        pr_url="https://github.com/eval/repo/pull/1", owner_repo=("eval", "repo"), effective_github_token="unused",
        backend="eval", model="gateway" if live else "stub", workflow_env={},
    )
    result = await SecurityWorkflow(poster=poster, scan_runner=scan_runner).run(SimpleNamespace(path=workspace), score, toolkit)
    return score_run(fixture, result.report, poster.reviews, model_calls["i"], live=live), result.report


async def run_all(*, live: bool, only: str | None) -> list[Score]:
    scores: list[Score] = []
    for fixture in FIXTURES:
        if only and fixture.name != only:
            continue
        score, report = await run_fixture(fixture, live=live)
        scores.append(score)
        sec = report.get("security", {})
        durations = (report.get("workflow_metrics") or {}).get("step_durations_ms", {})
        print(
            f"{fixture.name:20s} {'PASS' if score.passed else 'FAIL'}  verdict={sec.get('verdict')}  blocking={len(sec.get('blocking') or [])}  "
            f"advisory={len(sec.get('advisory') or [])}  dropped={len(sec.get('findings_dropped') or [])}  "
            f"scan={[(r.get('tool'), r.get('exit_code'), r.get('finding_count')) for r in (sec.get('scan') or [])]}  steps_ms={json.dumps(durations)}",
        )
        for note in score.notes:
            print(f"         - {note}")
    return scores


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true", help="use the LiteLLM gateway and the real scanners instead of the stubs")
    parser.add_argument("--only", choices=[f.name for f in FIXTURES])
    args = parser.parse_args(argv)
    scores = asyncio.run(run_all(live=args.live, only=args.only))
    passed = sum(1 for s in scores if s.passed)
    print(f"\n{passed}/{len(scores)} fixtures passed")
    return 0 if passed == len(scores) else 1


if __name__ == "__main__":
    sys.exit(main())
