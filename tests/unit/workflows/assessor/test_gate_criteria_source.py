"""T023 - Gate rule: criteria_source (spec 166 FR-005, amended by 410).

If card has criteria, return empty list with source "card".
If card has no criteria, return drafted criteria with source "assessor".
If card has no criteria and draft is empty, degrade to an empty assessor
criteria set with a warning (410): a ready assessment must never crash the
gate, it ships with no criteria and the run is still recordable.
"""
from __future__ import annotations

from performer.workflows.architect.models import Criterion
from performer.workflows.assessor.gate import criteria_source


def test_criteria_source_card_has_criteria():
    """Card with criteria returns empty and source 'card'."""
    card_crit = [Criterion(surface="/", action="click", expected="loaded", kind="functional")]
    drafted = [Criterion(surface="/home", action="view", expected="seen", kind="visual")]
    crit, source = criteria_source(card_crit, drafted)
    assert crit == []
    assert source == "card"


def test_criteria_source_no_card_with_draft():
    """No card criteria with draft returns draft and source 'assessor'."""
    card_crit = []
    drafted = [Criterion(surface="/", action="click", expected="loaded", kind="functional")]
    crit, source = criteria_source(card_crit, drafted)
    assert crit == drafted
    assert source == "assessor"


def test_criteria_source_no_card_no_draft_degrades():
    """No card criteria and no draft: degrade instead of raising (410)."""
    crit, source = criteria_source([], [])
    assert crit == []
    assert source == "assessor"


def test_criteria_source_card_empty_list():
    """Empty card criteria list is treated as no criteria."""
    card_crit = []
    drafted = [Criterion(surface="/", action="click", expected="loaded", kind="functional")]
    crit, source = criteria_source(card_crit, drafted)
    assert crit == drafted
    assert source == "assessor"


# live round 2026-09-06: an ambiguous card (not ready, no criteria) drafted none

def test_not_ready_with_no_criteria_and_no_draft_is_fine():
    criteria, source = criteria_source([], [], ready=False)
    assert (criteria, source) == ([], "assessor")


def test_ready_with_no_criteria_and_no_draft_degrades():
    """410: a ready assessment with nothing measurable ships empty criteria."""
    criteria, source = criteria_source([], [], ready=True)
    assert (criteria, source) == ([], "assessor")


def test_blank_card_criteria_count_as_none():
    criteria, source = criteria_source(["", "   "], [], ready=True)
    assert (criteria, source) == ([], "assessor")
