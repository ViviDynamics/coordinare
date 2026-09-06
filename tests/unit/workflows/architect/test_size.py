"""165 FR-006 / R5: size is a pure function of the blueprint, decided by code.

small = one milestone, no data model change, no interface. Everything else is
large. Each threshold is tested at its boundary and mutation-checked."""
from __future__ import annotations

from performer.workflows.architect.models import Blueprint
from performer.workflows.architect.size import size_of

_CRIT = [{"surface": "/", "action": "open", "expected": "ok", "kind": "functional"}]
_MS = {"goal": "g", "scope": ["a"], "done_when": "d"}


def _bp(milestones=1, changes=0, interfaces=0, docs=0) -> Blueprint:
    return Blueprint.model_validate({
        "summary": "s",
        "milestones": [_MS] * milestones,
        "modules": [],
        "data_model": {"changes": [{"kind": "column", "name": "c", "note": "n"}] * changes},
        "interfaces": [{"name": "i", "kind": "class", "contract": "c"}] * interfaces,
        "risks": [],
        "criteria": _CRIT,
        "docs": [{"topic": "t", "location": "l", "say": "s"}] * docs,
    })


def test_one_milestone_and_nothing_else_is_small():
    assert size_of(_bp()) == "small"


def test_two_milestones_are_large():
    assert size_of(_bp(milestones=2)) == "large"


def test_one_milestone_with_a_data_model_change_is_large():
    assert size_of(_bp(changes=1)) == "large"


def test_one_milestone_with_an_interface_is_large():
    assert size_of(_bp(interfaces=1)) == "large"


def test_docs_do_not_affect_size():
    """Whether a documenter runs is decided by the docs list itself (FR-014),
    not by size; a small card may still carry one doc topic."""
    assert size_of(_bp(docs=2)) == "small"
