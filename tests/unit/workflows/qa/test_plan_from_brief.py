"""165 FR-017: QA plans from the verification brief when present, unchanged
otherwise, and the judge reconciles against the same list."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from performer.workflows.base import WorkflowMetrics
from performer.workflows.budget import ModelReply
from performer.workflows.qa.plan import effective_criteria, run_plan_step
from performer.workflows.toolkit import Toolkit

_BRIEF = {
    "summary": "s",
    "criteria": [
        {"surface": "/time_entries/new", "action": "open the form", "expected": "a Category select is present", "kind": "visual"},
        {"surface": "TimeEntry", "action": "save without a category", "expected": "validation error", "kind": "functional"},
    ],
}


def _score(**over):
    base = {"acceptance_criteria": ["Card says something else"], "pr_diff": "", "description": "d", "verification_brief": {}}
    base.update(over)
    return SimpleNamespace(**base)


def test_brief_criteria_are_rendered_as_testable_sentences_and_marked():
    crit, source = effective_criteria(_score(verification_brief=_BRIEF))
    assert source == "blueprint"
    assert crit == [
        "/time_entries/new: open the form -> a Category select is present",
        "TimeEntry: save without a category -> validation error",
    ]


def test_without_a_brief_the_card_criteria_are_used_exactly():
    crit, source = effective_criteria(_score())
    assert (crit, source) == (["Card says something else"], "card")
    crit2, source2 = effective_criteria(_score(verification_brief={"criteria": []}))
    assert (crit2, source2) == (["Card says something else"], "card")


@pytest.mark.asyncio
async def test_the_plan_prompt_carries_the_brief_criteria_and_says_they_are_fixed():
    seen = {}

    async def model_call(persona, content, max_tokens):
        seen["text"] = content[0]["text"]
        crit = "/time_entries/new: open the form -> a Category select is present"
        return ModelReply(content=json.dumps({"checks": [{"id": "c1", "kind": "command", "criterion": crit, "command": "true"}], "surfaces": []}), finish_reason="stop")

    tk = Toolkit(metrics=WorkflowMetrics(), model_call=model_call, call_limit=12)
    plan = await run_plan_step(tk, _score(verification_brief=_BRIEF))
    assert "fixed by the architect's blueprint" in seen["text"]
    assert "Card says something else" not in seen["text"]
    assert plan.checks[0].criterion.startswith("/time_entries/new")


@pytest.mark.asyncio
async def test_without_a_brief_the_prompt_is_the_pre_165_one():
    seen = {}

    async def model_call(persona, content, max_tokens):
        seen["text"] = content[0]["text"]
        return ModelReply(content=json.dumps({"checks": [{"id": "c1", "kind": "command", "criterion": "Card says something else", "command": "true"}], "surfaces": []}), finish_reason="stop")

    tk = Toolkit(metrics=WorkflowMetrics(), model_call=model_call, call_limit=12)
    await run_plan_step(tk, _score())
    assert "Card says something else" in seen["text"]
    assert "blueprint" not in seen["text"]
