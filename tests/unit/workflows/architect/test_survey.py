"""165 FR-004: refused commands never run, budget and truncation hold, env knobs work."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from performer.workflows.architect.survey import (
    DEFAULT_MAX_COMMANDS,
    SurveyBudget,
    SurveyProposal,
    run_survey_step,
)


class _TK:
    def __init__(self, commands: list[str], output: str = "out"):
        self.proposal = SurveyProposal.model_validate(
            {"commands": [{"command": c, "reason": "r"} for c in commands]},
        )
        self.ran: list[str] = []
        self.output = output
        self.model_calls = 0

    async def call_model(self, *, persona, schema, content, budget):
        self.model_calls += 1
        assert schema is SurveyProposal
        return self.proposal

    async def run_command(self, cmd, *, cwd=None, timeout_s=300, plan_check_id=""):
        self.ran.append(cmd)
        return SimpleNamespace(exit_code=0, output_excerpt=self.output, passed=True, command=cmd)


@pytest.mark.asyncio
async def test_refused_commands_are_recorded_and_never_executed():
    tk = _TK(["ls app", "bundle install", "git log --oneline -3", "rm -rf ."])
    survey = await run_survey_step(tk, "card", Path("/w"), SurveyBudget())
    assert tk.ran == ["ls app", "git log --oneline -3"]
    assert survey.refused == 2
    refused = [r for r in survey.records if not r.allowed]
    assert all(r.refusal_reason for r in refused)
    assert "refused" in survey.as_text()


@pytest.mark.asyncio
async def test_the_command_budget_caps_execution_and_refusals_consume_it():
    cmds = ["bundle install"] + [f"ls dir{i}" for i in range(20)]
    tk = _TK(cmds)
    survey = await run_survey_step(tk, "card", Path("/w"), SurveyBudget(max_commands=5))
    assert len(survey.records) == 5
    assert len(tk.ran) == 4, "the refused command consumed one unit of budget"


@pytest.mark.asyncio
async def test_output_is_truncated_per_command():
    tk = _TK(["cat big"], output="x" * 10_000)
    survey = await run_survey_step(tk, "card", Path("/w"), SurveyBudget(max_output_chars=4000))
    rec = survey.records[0]
    assert len(rec.output) == 4000 and rec.truncated is True
    assert "[truncated]" in survey.as_text()


def test_env_knobs_override_defaults_and_bad_values_fall_back():
    b = SurveyBudget.from_env({"ARCHITECT_SURVEY_MAX_COMMANDS": "6", "ARCHITECT_SURVEY_MAX_OUTPUT_CHARS": "1000"})
    assert (b.max_commands, b.max_output_chars) == (6, 1000)
    b2 = SurveyBudget.from_env({"ARCHITECT_SURVEY_MAX_COMMANDS": "zero", "ARCHITECT_SURVEY_MAX_OUTPUT_CHARS": "-5"})
    assert (b2.max_commands, b2.max_output_chars) == (DEFAULT_MAX_COMMANDS, 4000)
    assert SurveyBudget.from_env(None).max_commands == DEFAULT_MAX_COMMANDS


@pytest.mark.asyncio
async def test_survey_makes_exactly_one_model_call():
    tk = _TK(["ls"])
    await run_survey_step(tk, "card", Path("/w"), SurveyBudget())
    assert tk.model_calls == 1
