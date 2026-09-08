"""Advocate throughput (spec 007 SC-006, rewritten for spec 173).

The original measured ``AdvocateService.scan_and_respond`` over 20 issues. That
service is gone: the advocate runs in a performer now. The property it guarded
is still worth guarding, so this measures the equivalent on the new path, with
every external call mocked to return instantly. It validates the run's own
overhead, not network latency, and it fails if issue handling ever becomes
sequential where it used to be bounded.
"""
from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from performer.workflows.advocate import AdvocateWorkflow

from tests.unit.workflows.advocate._fakes import FakeGitHub, FakeToolkit, issue


def _issues(n: int) -> list[dict]:
    return [issue(i, title=f"How do I use feature {i}?", body="I need help.") for i in range(1, n + 1)]


@pytest.mark.asyncio
async def test_an_advocate_run_handles_20_issues_within_10_seconds(tmp_path: Path) -> None:
    """SC-006 and spec-173 SC-009: 20 issues in one run, well inside the budget."""
    (tmp_path / "README.md").write_text("Run `make start`.")
    gh = FakeGitHub(_issues(20))
    replies = [{"classifications": [
        {"issue_id": f"I_{i}", "classification": "question", "confidence": 0.9,
         "reasoning": "covered", "answer": "Based on `README.md`: run make start.",
         "cited_documents": ["README.md"]}
        for i in range(1, 21)
    ]}]
    tk = FakeToolkit(replies)
    stand = SimpleNamespace(path=tmp_path, branch="advocate/perf")

    from tests.unit.workflows.advocate._fakes import score

    started = time.monotonic()
    result = await AdvocateWorkflow(lister=gh.lister, poster=gh).run(
        stand, score(workflow_env={"ADVOCATE_MAX_ISSUES_PER_CALL": "20"}), tk
    )
    elapsed = time.monotonic() - started

    record = result.report["advocate"]
    assert record["issues_seen"] == 20
    assert len(record["outcomes"]) == 20
    assert elapsed <= 10.0, f"20 issues took {elapsed:.2f}s"


@pytest.mark.asyncio
async def test_issues_are_batched_rather_than_one_call_each(tmp_path: Path) -> None:
    """The bound that makes the budget hold: a repository with many unhandled
    issues must not cost one model call per issue."""
    (tmp_path / "README.md").write_text("Run `make start`.")
    gh = FakeGitHub(_issues(20))
    tk = FakeToolkit([{"classifications": []}, {"classifications": []}])
    stand = SimpleNamespace(path=tmp_path, branch="advocate/perf")

    from tests.unit.workflows.advocate._fakes import score

    await AdvocateWorkflow(lister=gh.lister, poster=gh).run(
        stand, score(workflow_env={"ADVOCATE_MAX_ISSUES_PER_CALL": "10"}), tk
    )
    assert tk.metrics.model_calls == 2, "20 issues at 10 per call is 2 calls, not 20"
