"""Deterministic stand-ins for the security eval: a model answering the fixture's
replies in call order, a fake scanner runner returning the fixture's tool output
(or a missing binary), a survey command runner, and a recording GitHub poster."""
from __future__ import annotations

import json
from pathlib import Path

from performer.workflows.budget import ModelReply

from tests.eval.security_scenarios.fixtures import Fixture


def stub_model_for(fixture: Fixture):
    state = {"i": 0}

    async def model_call(persona: str, content: list[dict], max_tokens: int) -> ModelReply:
        reply = fixture.replies[min(state["i"], len(fixture.replies) - 1)]
        state["i"] += 1
        return ModelReply(content=json.dumps(reply), finish_reason="stop")

    model_call.state = state
    return model_call


def fake_scanner_for(fixture: Fixture):
    async def runner(argv: list[str], cwd, timeout_s: int):
        tool = Path(argv[0]).name
        if tool == fixture.scanner_failure:
            raise FileNotFoundError(tool)
        results = fixture.semgrep if tool == "semgrep" else fixture.bandit
        return (1 if results else 0), json.dumps({"results": results, "errors": []}), ""

    return runner


def stub_command_runner():
    async def runner(cmd: str, cwd, timeout_s: int):
        if cmd.startswith("git status"):
            return 0, ""
        return 0, "abc123 change\n"

    return runner


class RecordingPoster:
    """Records the review the workflow would post. Live mode uses this too: the eval never writes to GitHub."""

    def __init__(self) -> None:
        self.reviews: list[dict] = []

    async def __call__(self, owner, repo, number, *, event, body, comments, token) -> dict:
        self.reviews.append({"event": event, "body": body, "comments": comments, "number": number})
        return {"html_url": f"https://example.invalid/{owner}/{repo}/pull/{number}#review-{len(self.reviews)}"}
