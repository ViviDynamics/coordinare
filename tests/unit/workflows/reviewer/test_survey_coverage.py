"""Spec 169 FR-003, FR-004: the survey reuses the 165 allow-list and records every
command and refusal; opened files are tracked; exactly one coverage pass runs
when the diff was truncated or a changed file is unread."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from performer.workflows.architect.survey import Survey, SurveyBudget, SurveyRecord
from performer.workflows.base import WorkflowMetrics
from performer.workflows.budget import ModelReply
from performer.workflows.reviewer.intake import build_intake
from performer.workflows.reviewer.models import ChangedFile
from performer.workflows.reviewer.survey import (
    mark_opened,
    run_reviewer_survey,
    survey_output_lines,
    unread_files,
)
from performer.workflows.toolkit import Toolkit

from tests.unit.workflows.reviewer._fixtures import DIFF, TRUNCATED_DIFF


def _toolkit(proposals: list[list[str]], outputs: dict[str, str] | None = None):
    calls = {"model": 0, "commands": []}

    async def model_call(persona, content, max_tokens):
        i = min(calls["model"], len(proposals) - 1)
        calls["model"] += 1
        return ModelReply(content=json.dumps({"commands": [{"command": c, "reason": "look"} for c in proposals[i]]}), finish_reason="stop")

    async def runner(cmd, cwd, timeout_s):
        calls["commands"].append(cmd)
        return 0, (outputs or {}).get(cmd, f"output of {cmd}")

    return Toolkit(metrics=WorkflowMetrics(), model_call=model_call, command_runner=runner, call_limit=12), calls


def _score(diff=DIFF):
    return SimpleNamespace(pr_diff=diff, relay_feedback=[], implementation_brief={}, title="t", description="d", pr_url="https://github.com/o/r/pull/1")


def test_mark_opened_needs_an_admitted_command_that_ran_and_names_the_path():
    files = [ChangedFile(path="src/a.py", hunks=[], fully_in_diff=False), ChangedFile(path="src/b.py", hunks=[], fully_in_diff=False)]
    survey = Survey(records=[
        SurveyRecord(command="sed -n '1,50p' src/a.py", allowed=True, exit_code=0, output="x"),
        SurveyRecord(command="cat src/b.py", allowed=False, refusal_reason="cat is not allowed"),
    ])
    out = mark_opened(files, survey)
    assert [f.opened_by_survey for f in out] == [True, False]
    assert unread_files(out) == ["src/b.py"]
    failed = Survey(records=[SurveyRecord(command="sed -n '1,5p' src/b.py", allowed=True, exit_code=1, output="")])
    assert unread_files(mark_opened(out, failed)) == ["src/b.py"], "a command that failed opened nothing"


def test_survey_output_lines_come_only_from_admitted_commands():
    recs = [SurveyRecord(command="a", allowed=True, exit_code=0, output="l1\nl2"), SurveyRecord(command="b", allowed=False, refusal_reason="r", output="never")]
    assert survey_output_lines(recs) == ["l1", "l2"]


@pytest.mark.asyncio
async def test_no_coverage_pass_when_every_file_is_fully_in_the_diff(tmp_path):
    tk, calls = _toolkit([["git log --oneline -5"]])
    outcome, files = await run_reviewer_survey(tk, build_intake(_score()), tmp_path, SurveyBudget(max_commands=4))
    assert calls["model"] == 1 and outcome.coverage_pass_ran is False
    assert [r["command"] if isinstance(r, dict) else r.command for r in outcome.records()] == ["git log --oneline -5"]
    assert unread_files(files) == []


@pytest.mark.asyncio
async def test_a_truncated_diff_runs_exactly_one_coverage_pass_naming_the_unread_file(tmp_path):
    tk, calls = _toolkit([["git log --oneline -3"], ["sed -n '1,80p' src/extra.py"]])
    outcome, files = await run_reviewer_survey(tk, build_intake(_score(TRUNCATED_DIFF)), tmp_path, SurveyBudget(max_commands=4))
    assert calls["model"] == 2, "the survey call and the single coverage call"
    assert outcome.coverage_pass_ran is True
    assert "src/extra.py" in outcome.coverage_pass_output
    assert unread_files(files) == []
    assert files[-1].opened_by_survey is True


@pytest.mark.asyncio
async def test_refused_commands_are_recorded_and_open_nothing(tmp_path):
    tk, calls = _toolkit([["rm -rf src", "sed -n '1,10p' src/extra.py"]])
    outcome, _files = await run_reviewer_survey(tk, build_intake(_score(TRUNCATED_DIFF)), tmp_path, SurveyBudget(max_commands=4))
    refusals = [r for r in outcome.records() if not r.allowed]
    assert [r.command for r in refusals] == ["rm -rf src", "rm -rf src"], "refused in the survey and again in the coverage pass; never executed"
    assert "rm -rf src" not in calls["commands"]


def test_a_file_counts_as_opened_only_when_it_is_a_whole_argument():
    """Review finding: `test.py` must not count as opened because `tests/test.py` was."""
    from performer.workflows.reviewer.survey import command_names_path

    assert command_names_path("sed -n '1,50p' tests/test.py", "tests/test.py")
    assert not command_names_path("sed -n '1,50p' tests/test.py", "test.py")
    assert command_names_path("cat ./src/a.py", "src/a.py")
    assert command_names_path("git show HEAD:src/a.py", "src/a.py")
    assert not command_names_path("grep -rn a.py src", "src/a.py")
    files = [ChangedFile(path="test.py", hunks=[], fully_in_diff=False), ChangedFile(path="tests/test.py", hunks=[], fully_in_diff=False)]
    out = mark_opened(files, Survey(records=[SurveyRecord(command="sed -n '1,50p' tests/test.py", allowed=True, exit_code=0, output="x")]))
    assert [f.opened_by_survey for f in out] == [False, True]
