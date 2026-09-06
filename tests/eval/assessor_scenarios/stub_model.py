"""A deterministic model for the assessor eval: returns a canned assessment."""
from __future__ import annotations

import json

from performer.workflows.budget import ModelReply

from tests.eval.assessor_scenarios.fixtures import Fixture


def stub_model_for(fixture: Fixture):
    """Return a model_call that answers with the fixture's canned assessment."""

    async def model_call(persona: str, content: list[dict], max_tokens: int) -> ModelReply:
        return ModelReply(content=json.dumps(fixture.model_assessment), finish_reason="stop")

    return model_call
