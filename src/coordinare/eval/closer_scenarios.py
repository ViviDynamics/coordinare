"""Closer workflow scenario eval (spec 172 SC-005).

Default mode runs the workflow against the fixtures with a stubbed model and a
fake GitHub: deterministic, also exercised by pytest. ``--live`` swaps in the
LiteLLM gateway model and keeps the fake GitHub, so a live round exercises the
judgement without touching a real pull request.

    PYTHONPATH=src:agent/performer/src .venv/bin/python -m coordinare.eval.closer_scenarios
    PYTHONPATH=src:agent/performer/src .venv/bin/python -m coordinare.eval.closer_scenarios --live --only answered
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from types import SimpleNamespace
from typing import Any

from performer.workflows.base import WorkflowMetrics
from performer.workflows.budget import ModelReply
from performer.workflows.closer import CloserWorkflow
from performer.workflows.toolkit import Toolkit
from tests.eval.closer_scenarios.fixtures import FIXTURES, Fixture
from tests.eval.closer_scenarios.scoring import Score, score_run
from tests.unit.workflows.closer._fakes import FakeGitHub
from tests.unit.workflows.closer._fakes import score as fixture_score


async def run_fixture(fixture: Fixture, *, live: bool) -> tuple[Score, dict[str, Any]]:
    gh = FakeGitHub(fixture.threads, resolve_failures=set(fixture.resolve_failures))
    calls = {"n": 0}
    if live:
        from coordinare.eval.gateway import _call_model

        async def model_call(persona: str, content: list[dict[str, Any]], max_tokens: int) -> ModelReply:
            calls["n"] += 1
            return await _call_model(persona, content, max_tokens)
    else:
        async def model_call(persona: str, content: list[dict[str, Any]], max_tokens: int) -> ModelReply:
            calls["n"] += 1
            return ModelReply(content=json.dumps({"judgements": fixture.judgements}), finish_reason="stop")

    toolkit = Toolkit(metrics=WorkflowMetrics(), model_call=model_call, command_runner=None, call_limit=8)
    result = await CloserWorkflow(fetcher=gh.fetcher, resolver=gh.resolver, poster=gh.poster).run(
        SimpleNamespace(path="."), fixture_score(), toolkit,
    )
    return score_run(fixture, result.report, gh, calls["n"], live=live), result.report


async def run_all(*, live: bool, only: str | None) -> list[Score]:
    scores: list[Score] = []
    for fixture in FIXTURES:
        if only and fixture.name != only:
            continue
        score, report = await run_fixture(fixture, live=live)
        scores.append(score)
        closing = report.get("closing", {})
        durations = (report.get("workflow_metrics") or {}).get("step_durations_ms", {})
        print(f"{fixture.name:20s} {'PASS' if score.passed else 'FAIL'}  verdict={closing.get('verdict')}  "
              f"threads={closing.get('threads_read')}  resolved={len(closing.get('resolved') or [])}  "
              f"open={len(closing.get('open_threads') or [])}  model_calls={(closing.get('workflow_metrics') or {}).get('model_calls')}  steps_ms={json.dumps(durations)}")
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
