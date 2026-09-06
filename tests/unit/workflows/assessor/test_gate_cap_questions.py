"""T019 - Gate rule: cap_questions (spec 166 FR-006).

Keep at most two questions, record dropped ones.

Mutation: remove the min(..., 2) truncation or change 2 to 3 -> test fails.
"""
from __future__ import annotations

from performer.workflows.assessor.gate import cap_questions


def test_cap_questions_zero():
    """Empty questions list returns empty kept and dropped."""
    kept, dropped = cap_questions([])
    assert kept == []
    assert dropped == []


def test_cap_questions_one():
    """One question returns it as kept."""
    kept, dropped = cap_questions(["Q1"])
    assert kept == ["Q1"]
    assert dropped == []


def test_cap_questions_two():
    """Exactly two questions returns both as kept."""
    kept, dropped = cap_questions(["Q1", "Q2"])
    assert kept == ["Q1", "Q2"]
    assert dropped == []


def test_cap_questions_three():
    """Three questions keeps first two, drops the third."""
    kept, dropped = cap_questions(["Q1", "Q2", "Q3"])
    assert kept == ["Q1", "Q2"]
    assert dropped == ["Q3"]


def test_cap_questions_many():
    """Four or more questions keeps first two."""
    kept, dropped = cap_questions(["Q1", "Q2", "Q3", "Q4", "Q5"])
    assert kept == ["Q1", "Q2"]
    assert dropped == ["Q3", "Q4", "Q5"]


def test_cap_questions_custom_limit():
    """Custom limit is respected."""
    kept, dropped = cap_questions(["Q1", "Q2", "Q3"], limit=1)
    assert kept == ["Q1"]
    assert dropped == ["Q2", "Q3"]


def test_cap_questions_preserves_order():
    """Question order is preserved."""
    qs = ["First question?", "Second question?", "Third question?", "Fourth question?"]
    kept, dropped = cap_questions(qs)
    assert kept[0] == "First question?"
    assert kept[1] == "Second question?"
    assert dropped[0] == "Third question?"
