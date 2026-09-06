"""A deterministic model for the architect eval: answers the survey with the
fixture's proposed commands and the blueprint with the fixture's blueprint."""
from __future__ import annotations

import json

from performer.workflows.budget import ModelReply

from tests.eval.architect_scenarios.fixtures import Fixture


def stub_model_for(fixture: Fixture):
    answers = iter([
        json.dumps({"commands": [{"command": c, "reason": "orient"} for c in fixture.survey_commands]}),
        json.dumps(fixture.blueprint),
    ])

    async def model_call(persona: str, content: list[dict], max_tokens: int) -> ModelReply:
        return ModelReply(content=next(answers), finish_reason="stop")

    return model_call
