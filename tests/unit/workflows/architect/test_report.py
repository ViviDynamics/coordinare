"""165 FR-007/FR-008: the report proves write-freedom by execution and carries
the blueprint, its size and hash, and the metrics."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from performer.workflows.architect.models import Blueprint
from performer.workflows.architect.report import (
    ArchitectWroteToTree,
    blueprint_hash,
    build_report,
    write_free_check,
)
from performer.workflows.architect.survey import Survey, SurveyRecord
from performer.workflows.base import WorkflowMetrics

_BP = Blueprint.model_validate({
    "summary": "s", "milestones": [{"goal": "g", "scope": ["a"], "done_when": "d"}],
    "modules": [], "data_model": {"changes": []}, "interfaces": [], "risks": [],
    "criteria": [{"surface": "/", "action": "open", "expected": "ok", "kind": "functional"}], "docs": [],
})


class _TK:
    def __init__(self, exit_code=0, output=""):
        self.exit_code, self.output, self.ran = exit_code, output, []

    async def run_command(self, cmd, *, cwd=None, timeout_s=300, plan_check_id=""):
        self.ran.append(cmd)
        return SimpleNamespace(exit_code=self.exit_code, output_excerpt=self.output)


@pytest.mark.asyncio
async def test_write_free_check_is_an_executed_git_status():
    tk = _TK()
    check = await write_free_check(tk, Path("/w"))
    assert tk.ran == ["git status --porcelain"]
    assert check["passed"] is True and check["exit_code"] == 0


@pytest.mark.asyncio
async def test_a_dirty_tree_fails_the_check_and_names_the_paths():
    tk = _TK(output=" M app/models/user.rb\n?? db/migrate/x.rb\n")
    check = await write_free_check(tk, Path("/w"))
    assert check["passed"] is False
    assert check["dirty_paths"] == ["app/models/user.rb", "db/migrate/x.rb"]


def test_report_refuses_to_exist_when_the_tree_is_dirty():
    survey = Survey()
    with pytest.raises(ArchitectWroteToTree, match=r"app/models/user\.rb"):
        build_report(_BP, "small", survey, {"passed": False, "dirty_paths": ["app/models/user.rb"]}, WorkflowMetrics())


def test_report_shape():
    m = WorkflowMetrics()
    m.model_calls = 2
    m.step_durations_ms["survey"] = 5
    survey = Survey(records=[SurveyRecord(command="rm -rf .", allowed=False, refusal_reason="no")])
    report = build_report(_BP, "small", survey, {"command": "git status --porcelain", "exit_code": 0, "passed": True, "dirty_paths": []}, m)
    assert report["size"] == "small" and report["blueprint"]["size"] == "small"
    assert report["blueprint"]["blueprint_hash"] == blueprint_hash(_BP)
    assert report["write_free_check"]["passed"] is True and report["write_free_check"]["refused_commands"] == 1
    assert report["workflow_metrics"]["model_calls"] == 2 and report["workflow_metrics"]["step_durations_ms"] == {"survey": 5}
    assert report["blueprint"]["milestones"][0]["goal"] == "g"


def test_hash_is_stable_and_content_sensitive():
    assert blueprint_hash(_BP) == blueprint_hash(_BP)
    other = _BP.model_copy(update={"summary": "different"})
    assert blueprint_hash(other) != blueprint_hash(_BP)
