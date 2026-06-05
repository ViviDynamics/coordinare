"""080 — DifficultyClassifier (ModelClassifier) — T027/T028.

Conditional-escalation routing is covered in test_strategies.py with a fake
classifier; here we pin the model-backed classifier's score parsing and the
FR-015 failure→think (1.0) contract.
"""

from __future__ import annotations

import pytest

from performer.proxy.classifier import ModelClassifier, _parse_score
from performer.proxy.llm_turn import LLMRequest, LLMResponse, Message
from performer.proxy.upstreams import UpstreamError


@pytest.mark.parametrize(
    ("text", "expected"),
    [("0.0", 0.0), ("1", 1.0), ("0.73", 0.73), ("difficulty: 0.4", 0.4),
     ("", 1.0), ("no number here", 1.0), ("2.5", 1.0), ("-3", 1.0)],
)
def test_parse_score_clamps_and_defaults(text, expected):
    assert _parse_score(text) == expected


class _Up:
    name = "cls"

    def __init__(self, *, content=None, raises=False):
        self._content = content
        self._raises = raises
        self.calls = []

    async def complete(self, request, *, tools_enabled=True):
        self.calls.append((request, tools_enabled))
        if self._raises:
            raise UpstreamError("down")
        return LLMResponse(content=self._content)


@pytest.mark.asyncio
async def test_model_classifier_parses_score_and_hides_tools():
    up = _Up(content="0.8")
    score = await ModelClassifier(up).score(LLMRequest(messages=(Message.user("hard task"),)))
    assert score == 0.8
    # classifier probe runs with tools hidden
    assert up.calls[0][1] is False


@pytest.mark.asyncio
async def test_model_classifier_failure_escalates_to_think():
    up = _Up(raises=True)
    score = await ModelClassifier(up).score(LLMRequest(messages=(Message.user("x"),)))
    assert score == 1.0  # FR-015: failure defaults to think
