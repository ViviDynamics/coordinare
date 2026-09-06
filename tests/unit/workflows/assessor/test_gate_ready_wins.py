"""T022 - Gate rule: ready_wins (spec 166).

When ready=True, move any remaining questions to assumptions and return empty questions.

Mutation: remove the if ready check or change the logic -> test fails.
"""
from __future__ import annotations

from performer.workflows.assessor.gate import ready_wins


def test_ready_wins_no_questions():
    """Ready with no questions returns empty questions."""
    qs, assumptions = ready_wins(True, [])
    assert qs == []
    assert assumptions == []


def test_ready_wins_with_questions():
    """Ready with questions moves them to assumptions."""
    qs, assumptions = ready_wins(True, ["Q1", "Q2"])
    assert qs == []
    assert len(assumptions) == 2
    assert "Q1" in assumptions[0]
    assert "Q2" in assumptions[1]


def test_ready_wins_not_ready():
    """Not ready keeps questions."""
    qs, assumptions = ready_wins(False, ["Q1"])
    assert qs == ["Q1"]
    assert assumptions == []


def test_ready_wins_phrasing():
    """Questions are phrased as 'not asked' assumptions."""
    _qs, assumptions = ready_wins(True, ["Why is this needed?"])
    assert "not asked" in assumptions[0]
