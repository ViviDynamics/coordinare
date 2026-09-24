"""417 at the workflow seam: the tree snapshot is taken before any step runs and
the write-free check judges only NEW dirty paths; blueprint guards run before the
report is emitted."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from performer.workflows.architect import ArchitectWorkflow
from performer.workflows.architect.models import (
    Blueprint,
    Criterion,
    DataModel,
    Milestone,
    Module,
)
from performer.workflows.architect.report import BlueprintPathError
from performer.workflows.architect.survey import ProposedCommand, SurveyProposal
from performer.workflows.base import WorkflowMetrics


class _Toolkit:
    def __init__(self, blueprint, status_output: str):
        self.blueprint = blueprint
        self._status_output = status_output
        self.status_calls = 0

    async def call_model(self, persona, schema, content, budget):
        if schema is SurveyProposal:
            return SurveyProposal(commands=[ProposedCommand(command="ls", reason="r")])
        return self.blueprint

    async def run_command(self, command: str, cwd: Path, timeout_s: int):
        if command == "git status --porcelain":
            self.status_calls += 1
            return SimpleNamespace(exit_code=0, output_excerpt=self._status_output)
        return SimpleNamespace(exit_code=0, output_excerpt="app.py\n")

    def emit(self, *args, **kwargs):
        pass

    @property
    def metrics(self):
        return WorkflowMetrics()

    def events(self):
        return []


def _blueprint_ok(tmp_path: Path) -> Blueprint:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("x", encoding="utf-8")
    return Blueprint(
        summary="s",
        milestones=[Milestone(goal="g", scope=["src/app.py"], done_when="d")],
        modules=[Module(path="src/app.py", note="n")],
        data_model=DataModel(changes=[]),
        interfaces=[],
        risks=[],
        criteria=[Criterion(surface="s", action="a", expected="e", kind="functional")],
        docs=[],
    )


def _run(toolkit, workspace):
    workflow = ArchitectWorkflow()
    score = SimpleNamespace(workflow_env={})
    return workflow.run(SimpleNamespace(path=workspace), score, toolkit)


@pytest.mark.asyncio
async def test_pre_existing_dirt_does_not_fail_the_workflow(tmp_path):
    blueprint = _blueprint_ok(tmp_path)
    toolkit = _Toolkit(blueprint, "?? docs/old.md")
    result = await _run(toolkit, tmp_path)
    assert result.report["write_free_check"]["passed"] is True
    assert result.report["write_free_check"]["pre_existing"] == ["docs/old.md"]
    baseline_plus_check = 2
    assert toolkit.status_calls == baseline_plus_check


@pytest.mark.asyncio
async def test_a_hallucinated_blueprint_path_fails_the_workflow(tmp_path):
    blueprint = _blueprint_ok(tmp_path)
    blueprint.modules = [Module(path="src/ghost.py", note="n")]
    toolkit = _Toolkit(blueprint, "")
    with pytest.raises(BlueprintPathError):
        await _run(toolkit, tmp_path)
