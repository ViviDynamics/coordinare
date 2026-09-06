"""165 FR-011 / FR-012: the implementer persona defers to a brief when one is
present and never writes documentation."""
from __future__ import annotations

from coordinare.services.persona_service import DEFAULT_INSTRUCTIONS


def _implementer() -> str:
    return DEFAULT_INSTRUCTIONS["implementer"]


def test_the_brief_is_declared_authoritative_over_plan_files():
    text = _implementer()
    assert "Implementation Brief" in text
    assert "milestones replace" in text and "plan.md/tasks.md" in text


def test_single_turn_is_explained():
    assert "SINGLE TURN" in _implementer()


def test_the_implementer_never_writes_documentation():
    text = _implementer()
    assert "Never create or edit documentation" in text
    assert "the documenter owns every document" in text


def test_the_prose_path_instructions_are_still_present():
    """164 FR-005: a card without a brief still reads plan.md and tasks.md."""
    text = _implementer()
    assert "Read plan.md + tasks.md" in text
    assert "PARTIAL_PROGRESS" in text


def test_reviewer_is_told_an_implementer_documentation_change_is_a_finding():
    """FR-012 second half: the review stage reports the change. The reviewer
    persona is where the review stage's judgement lives."""
    from coordinare.services.persona_service import DEFAULT_INSTRUCTIONS

    text = DEFAULT_INSTRUCTIONS["reviewer"]
    assert "implementation_brief" in text
    assert "documentation tree" in text and "finding" in text
    assert "documenter owns" in text
