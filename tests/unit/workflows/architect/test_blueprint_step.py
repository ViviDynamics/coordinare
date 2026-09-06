"""165 FR-005: the blueprint call goes through the 164 guards and a hollow
blueprint is a schema violation naming the field, never an accepted plan."""
from __future__ import annotations

import json

import pytest
from performer.workflows.architect.blueprint import run_blueprint_step
from performer.workflows.architect.models import Blueprint
from performer.workflows.base import SchemaViolation, WorkflowMetrics
from performer.workflows.budget import Budget, ModelReply
from performer.workflows.toolkit import Toolkit

_VALID = {
    "summary": "s", "milestones": [{"goal": "g", "scope": ["a"], "done_when": "d"}],
    "modules": [], "data_model": {"changes": []}, "interfaces": [], "risks": [],
    "criteria": [{"surface": "/", "action": "open", "expected": "ok", "kind": "functional"}], "docs": [],
}


def _toolkit(replies: list[str]) -> tuple[Toolkit, list[int]]:
    budgets: list[int] = []
    it = iter(replies)

    async def model_call(persona, content, max_tokens):
        budgets.append(max_tokens)
        return ModelReply(content=next(it), finish_reason="stop")

    tk = Toolkit(metrics=WorkflowMetrics(), model_call=model_call, command_runner=None,
                 screenshot_capture=None, dom_reader=None, event_sink=None, call_limit=12)
    return tk, budgets


@pytest.mark.asyncio
async def test_valid_blueprint_is_returned_with_the_plan_sized_budget():
    tk, budgets = _toolkit([json.dumps(_VALID)])
    bp = await run_blueprint_step(tk, "card", "survey")
    assert isinstance(bp, Blueprint) and bp.milestones[0].goal == "g"
    assert budgets == [Budget.for_step("blueprint").max_tokens] and budgets[0] >= 8000


@pytest.mark.asyncio
async def test_a_hollow_blueprint_is_reprompted_once_then_raises_naming_the_field():
    hollow = json.dumps({**_VALID, "milestones": []})
    tk, _ = _toolkit([hollow, hollow])
    with pytest.raises(SchemaViolation) as exc:
        await run_blueprint_step(tk, "card", "survey")
    assert "milestones" in str(exc.value)
    assert tk.metrics.schema_reprompts == 1


@pytest.mark.asyncio
async def test_the_reprompt_can_recover():
    tk, _ = _toolkit(["not json", json.dumps(_VALID)])
    bp = await run_blueprint_step(tk, "card", "survey")
    assert bp.criteria[0].surface == "/"
    assert tk.metrics.schema_reprompts == 1 and tk.metrics.model_calls == 2


@pytest.mark.asyncio
async def test_the_model_never_sets_size():
    tk, _ = _toolkit([json.dumps({**_VALID, "size": "small"}), json.dumps(_VALID)])
    bp = await run_blueprint_step(tk, "card", "survey")
    assert not hasattr(bp, "size")
    assert tk.metrics.schema_reprompts == 1, "an extra field is a schema violation, reprompted once"
