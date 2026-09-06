"""T020 - Gate rule: drop_answered (spec 166 FR-007).

Drop questions matching answered clarifications (60% token overlap).
Record dropped questions and matched answers in assumptions.

Mutation: remove the matches_answered call or change threshold -> test fails.
"""
from __future__ import annotations

from performer.workflows.assessor.gate import drop_answered
from performer.workflows.assessor.models import ClarificationRound


def test_drop_answered_no_answered():
    """No answered clarifications means no questions dropped."""
    qs = ["What's the goal?", "Why?"]
    answered = []
    kept, dropped, assumptions = drop_answered(qs, answered)
    assert kept == qs
    assert dropped == []
    assert assumptions == []


def test_drop_answered_exact_match():
    """Question matching answered clarification is dropped."""
    qs = ["What's the goal?"]
    answered = [ClarificationRound(question="What's the goal?", answer="To fix it")]
    kept, dropped, assumptions = drop_answered(qs, answered)
    assert kept == []
    assert dropped == ["What's the goal?"]
    assert "already answered" in assumptions[0]
    assert "To fix it" in assumptions[0]


def test_drop_answered_token_overlap():
    """Question with 60% token overlap is dropped."""
    qs = ["What is the goal?"]
    answered = [ClarificationRound(question="What's the goal?", answer="To fix it")]
    kept, dropped, _assumptions = drop_answered(qs, answered, threshold=0.6)
    assert kept == []
    assert dropped == ["What is the goal?"]


def test_drop_answered_low_overlap():
    """Question with low overlap is kept."""
    qs = ["What else?"]
    answered = [ClarificationRound(question="What's the goal?", answer="To fix it")]
    kept, dropped, _assumptions = drop_answered(qs, answered, threshold=0.6)
    assert kept == ["What else?"]
    assert dropped == []


def test_drop_answered_blank_answer_ignored():
    """Blank answers do not cause questions to be dropped."""
    qs = ["What's the goal?"]
    answered = [ClarificationRound(question="What's the goal?", answer="")]
    kept, dropped, _assumptions = drop_answered(qs, answered)
    assert kept == ["What's the goal?"]
    assert dropped == []


def test_drop_answered_multiple():
    """Multiple questions, some dropped."""
    qs = ["What's the goal?", "Why?", "How?"]
    answered = [
        ClarificationRound(question="What's the goal?", answer="To fix it"),
        ClarificationRound(question="How?", answer="By coding"),
    ]
    kept, dropped, assumptions = drop_answered(qs, answered)
    assert kept == ["Why?"]
    assert len(dropped) == 2
    assert len(assumptions) == 2
