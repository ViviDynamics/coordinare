"""Parametrized tests for implementer scenario eval (spec 167 SC-006).

Run all nine fixtures with the fake harness and score the results.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from performer.models import Stand
from performer.workflows.base import WorkflowMetrics
from performer.workflows.implementer import ImplementerWorkflow
from performer.workflows.toolkit import Toolkit

from tests.eval.implementer_scenarios.fakes import (
    Edges,
    Harness,
    _fake_test_runner,
    _repo,
    stub_model_call,
)
from tests.eval.implementer_scenarios.fixtures import FIXTURES
from tests.eval.implementer_scenarios.scoring import score_run


async def _run(repo: Path, score, harness: Harness, edges: Edges, **overrides):
    """Run the implementer workflow with fake infrastructure."""

    toolkit = Toolkit(
        metrics=WorkflowMetrics(),
        model_call=stub_model_call(),
        command_runner=_fake_test_runner(repo),
        agent_turn_runner=harness,
        call_limit=64,
    )
    stand = Stand(path=repo, branch="feat/x")
    result = await ImplementerWorkflow().run(
        stand, score, toolkit, ctx_overrides=edges.overrides(**overrides)
    )
    return result.report, toolkit


def _score(
    *,
    milestones=None,
    work_kind=None,
    single_turn=False,
    test_command="pytest -rA",
    env=None,
):
    brief = None
    if milestones is not None or work_kind is not None:
        brief = {"milestones": milestones or [], "implementer_single_turn": single_turn}
        if work_kind:
            brief["work_kind"] = work_kind
    return SimpleNamespace(
        title="Add the thing",
        description="The thing should work.",
        acceptance_criteria=["it works"],
        implementation_brief=brief,
        workflow_env=env or {},
        test_command=test_command,
        issue_number=7,
        local_test_gate={"enabled": True, "lint_command": "lint"},
        owner_repo=("o", "r"),
        effective_github_token="t",
        repo_url="https://example.invalid/o/r",
        pr_url="",
    )


@pytest.mark.parametrize("fixture", FIXTURES, ids=lambda f: f.name)
@pytest.mark.asyncio
async def test_fixture(tmp_path, fixture):
    """Run a fixture and assert it passes scoring."""
    repo, _ = _repo(tmp_path)
    harness = Harness(repo, fixture.script)
    edges = fixture.edges_factory()

    score = _score(
        milestones=fixture.milestones,
        work_kind=fixture.work_kind if fixture.work_kind != "feature" else None,
        single_turn=fixture.single_turn,
        env=fixture.env or None,
    )

    report, _ = await _run(repo, score, harness, edges)
    result = score_run(fixture, report, repo, harness, edges, live=False)

    assert result.passed, f"{fixture.name} failed: {', '.join(result.notes)}"


@pytest.mark.asyncio
async def test_scorer_can_fail(tmp_path):
    """Verify the scorer catches mismatches."""
    repo, _ = _repo(tmp_path)

    def always_fail(repo, brief):
        pass

    harness = Harness(repo, {"TESTS": always_fail, "IMPLEMENT": always_fail})
    edges = Edges()
    score = _score(milestones=[{"goal": "m0", "scope": "src/m0.py", "done_when": "m0 tests"}])

    report, _ = await _run(repo, score, harness, edges)
    result = score_run(FIXTURES[0], report, repo, harness, edges, live=False)

    assert not result.passed, "scorer should detect fixture mismatch"
    assert any("status" in note or "persona" in note for note in result.notes), "notes should explain the mismatch"
