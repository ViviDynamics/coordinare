"""Deterministic stand-ins for the reviewer eval: a model that answers the
fixture's replies in call order, a runner for the survey commands, and a
GitHub poster that records the one review instead of posting it."""
from __future__ import annotations

import json

from performer.workflows.budget import ModelReply

from tests.eval.reviewer_scenarios.fixtures import Fixture


def stub_model_for(fixture: Fixture):
    state = {"i": 0}

    async def model_call(persona: str, content: list[dict], max_tokens: int) -> ModelReply:
        reply = fixture.replies[min(state["i"], len(fixture.replies) - 1)]
        state["i"] += 1
        return ModelReply(content=json.dumps(reply), finish_reason="stop")

    return model_call


def stub_runner_for(fixture: Fixture):
    async def runner(cmd: str, cwd, timeout_s: int) -> tuple[int, str]:
        if cmd.startswith("git status"):
            return 0, ""
        if cmd in fixture.survey_output:
            return 0, fixture.survey_output[cmd]
        return 0, "abc123 add div\n"

    return runner


class RecordingPoster:
    """Records the review the workflow would post. Live mode uses this too: the
    eval never writes to GitHub."""

    def __init__(self) -> None:
        self.reviews: list[dict] = []

    async def __call__(self, owner, repo, number, *, event, body, comments, token) -> dict:
        self.reviews.append({"event": event, "body": body, "comments": comments, "number": number})
        return {"html_url": f"https://example.invalid/{owner}/{repo}/pull/{number}#review-{len(self.reviews)}"}
