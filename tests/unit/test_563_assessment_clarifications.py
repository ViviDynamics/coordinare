"""Retained issue comments reach Coordinare's initial assessment prompt."""
from __future__ import annotations

import pytest

from coordinare.services.conducting import _build_assess_prompt


def test_retained_comment_body_reaches_assessment_without_raw_comments() -> None:
    body = 'Please add test_score_word_nonletters_and_case: score_word("QiUx?! 42") == 20.'
    prompt = _build_assess_prompt({
        "title": "Sample", "body": "Implement scoring", "clarifications": [
            {"source": "issue", "comment_id": "6082656906", "author": "human", "body": body},
            {"questions": ["Retain tests?"], "answer": "Keep every existing test."},
        ],
    })
    assert body in prompt
    assert "6082656906" in prompt
    assert "human" in prompt
    assert "issue" in prompt
    assert "Retain tests?" in prompt
    assert "Keep every existing test." in prompt


@pytest.mark.parametrize("stage", ["assessing", "architecting", "implementing", "reviewing"])
def test_state_clarification_reaches_dispatch_after_card_snapshot(monkeypatch: pytest.MonkeyPatch, stage: str) -> None:
    from coordinare.config import PersonasConfig
    from coordinare.graph.nodes.dispatch_performer import _base_card_context

    monkeypatch.setattr("coordinare.graph.nodes.dispatch_performer.load_personas_hot", lambda *args: PersonasConfig())
    clarification = {"source": "issue", "comment_id": "9", "author": "human", "body": "Add the requested test."}
    state = {"card_clarifications": [clarification], "assessor_open_questions": [{"question": "Old question?"}]}
    context, _ = _base_card_context(state, {"id": "card", "title": "Sample"}, "card", stage)
    assert context["clarifications"] == [clarification]
    assert ("prior_clarifications" in context) == (stage == "assessing")
