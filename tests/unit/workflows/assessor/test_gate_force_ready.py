"""T021 - Gate rule: force_ready (spec 166 FR-008).

After two answered rounds or no usable questions, force ready=True
and move remaining questions to assumptions.

Mutation: remove the >= check or change limit -> test fails.
"""
from __future__ import annotations

from performer.workflows.assessor.gate import force_ready


def test_force_ready_first_round_with_question():
    """First answered round with questions does not force ready."""
    ready, qs, assumptions = force_ready(False, ["Q1"], answered_rounds=1, limit=2)
    assert ready is False
    assert qs == ["Q1"]
    assert assumptions == []


def test_force_ready_two_rounds_no_questions():
    """Two answered rounds with no questions forces ready."""
    ready, _qs, _assumptions = force_ready(False, [], answered_rounds=2, limit=2)
    assert ready is True


def test_force_ready_two_rounds_with_questions():
    """Two answered rounds with questions forces ready and moves them."""
    ready, qs, assumptions = force_ready(False, ["Q1", "Q2"], answered_rounds=2, limit=2)
    assert ready is True
    assert qs == []
    assert len(assumptions) == 2
    assert "Q1" in assumptions[0]
    assert "Q2" in assumptions[1]


def test_force_ready_already_ready():
    """Already ready assessment stays ready."""
    ready, qs, _assumptions = force_ready(True, ["Q1"], answered_rounds=0, limit=2)
    assert ready is True
    assert qs == ["Q1"]


def test_not_ready_with_no_usable_question_becomes_ready():
    """FR-008 second clause: when nothing usable is left to ask (every
    question capped or already answered), a not-ready assessment would block
    the card for nothing, so the gate makes it ready and says why."""
    ready, qs, assumptions = force_ready(False, [], answered_rounds=1, limit=2)
    assert ready is True
    assert qs == []
    assert len(assumptions) == 1 and assumptions[0].startswith("assumed:")


def test_not_ready_with_a_question_and_one_round_still_asks():
    ready, qs, assumptions = force_ready(False, ["Which audience?"], answered_rounds=1, limit=2)
    assert (ready, qs, assumptions) == (False, ["Which audience?"], [])


def test_force_ready_custom_limit():
    """Custom limit is respected."""
    ready, qs, assumptions = force_ready(False, ["Q1"], answered_rounds=1, limit=1)
    assert ready is True
    assert qs == []
    assert "Q1" in assumptions[0]
