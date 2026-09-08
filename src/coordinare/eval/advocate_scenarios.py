"""Advocate workflow scenario eval (spec 173 SC-005, SC-010).

Default mode runs the workflow against the fixtures with a stubbed model and a
fake GitHub: deterministic, and also exercised by pytest. ``--live`` swaps in
the gateway model and keeps the fake GitHub, so a live round exercises the
classification and the grounding gate without touching a real issue.

    PYTHONPATH=src:agent/performer/src .venv/bin/python -m coordinare.eval.advocate_scenarios
    PYTHONPATH=src:agent/performer/src .venv/bin/python -m coordinare.eval.advocate_scenarios --live --only answerable
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

from performer.workflows.advocate import AdvocateWorkflow
from performer.workflows.base import WorkflowMetrics
from performer.workflows.budget import ModelReply
from performer.workflows.toolkit import Toolkit
from tests.eval.advocate_scenarios.fixtures import FIXTURES, Fixture
from tests.eval.advocate_scenarios.scoring import Score, score_run
from tests.unit.workflows.advocate._fakes import FakeGitHub


async def run_fixture(fixture: Fixture, *, live: bool) -> tuple[Score, dict]:
    gh = FakeGitHub(fixture.issues)
    calls = {"n": 0}

    async def stub(persona, content, max_tokens: int = 0) -> ModelReply:
        # The real Toolkit unwraps ModelReply.content; returning a bare string
        # makes every call fail with "'str' object has no attribute 'content'"
        # and every issue escalate as unclassified.
        calls["n"] += 1
        return ModelReply(
            content=json.dumps(fixture.stub_reply or {"classifications": []}),
            finish_reason="stop",
        )

    model_call = stub
    if live:
        # The real gateway helper. An earlier draft imported a module that does
        # not exist, so --live would have failed on import at the first live
        # round rather than in any stubbed test.
        from coordinare.eval.gateway import _call_model

        async def counted(persona, content, max_tokens: int = 0) -> ModelReply:
            calls["n"] += 1
            return await _call_model(persona, content, max_tokens)

        model_call = counted

    toolkit = Toolkit(metrics=WorkflowMetrics(), model_call=model_call, command_runner=None, call_limit=8)

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        for rel, body in fixture.docs.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(body)
        stand = SimpleNamespace(path=root, branch="advocate/eval")
        score_obj = SimpleNamespace(
            workflow_env={}, owner_repo="o/r", github_token="t", project_id="",
        )
        started = time.monotonic()
        result = await AdvocateWorkflow(lister=gh.lister, poster=gh).run(stand, score_obj, toolkit)

    record = result.report.get("advocate", {})
    scored = score_run(fixture, record, gh, calls["n"])
    return scored, {"wall_ms": int((time.monotonic() - started) * 1000),
                    "model_calls": calls["n"], "record": record}


async def run_all(*, live: bool, only: str | None) -> list[Score]:
    scores: list[Score] = []
    for fixture in FIXTURES:
        if only and fixture.name != only:
            continue
        score, detail = await run_fixture(fixture, live=live)
        scores.append(score)
        print(f"{fixture.name:22s} {'PASS' if score.passed else 'FAIL'}  "
              f"{detail['wall_ms']}ms  model_calls={detail['model_calls']}")
        for note in score.notes:
            print(f"         - {note}")
    return scores


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true",
                        help="use the gateway model instead of the stub")
    parser.add_argument("--only", choices=[f.name for f in FIXTURES])
    args = parser.parse_args(argv)
    scores = asyncio.run(run_all(live=args.live, only=args.only))
    passed = sum(1 for s in scores if s.passed)
    print(f"\n{passed}/{len(scores)} fixtures passed")
    return 0 if passed == len(scores) else 1


if __name__ == "__main__":
    sys.exit(main())
