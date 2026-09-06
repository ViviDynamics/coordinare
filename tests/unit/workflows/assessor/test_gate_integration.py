"""T024 - Gate integration test (spec 166 FR-001).

Tests the full run_gate function which applies all gate rules in order:
ready_wins, drop_answered, cap_questions, force_ready, criteria_source.
"""
from __future__ import annotations

from performer.workflows.architect.models import Criterion
from performer.workflows.assessor.gate import run_gate
from performer.workflows.assessor.intake import Intake
from performer.workflows.assessor.models import (
    Assessment,
    ClarificationRound,
    GateRecord,
    ModelAssessment,
)


def test_gate_integration_clear_card():
    """Clear card: ready, no questions, card criteria."""
    model_a = ModelAssessment(
        ready=True,
        goal="Fix the bug",
        expected_behavior="It works",
        out_of_scope=["Performance"],
        questions=[],
        assumptions=[],
        criteria=[],
    )
    intake = Intake(
        title="Fix bug",
        description="A bug",
        criteria=["Works"],
        clarifications=[],
        answered_rounds=0,
    )

    assessment, record = run_gate(model_a, intake)

    assert isinstance(assessment, Assessment)
    assert assessment.ready is True
    assert assessment.questions == []
    assert assessment.criteria_source == "card"
    assert assessment.criteria == []
    assert isinstance(record, GateRecord)


def test_gate_integration_ambiguous_card():
    """Ambiguous card: not ready, two questions (at max), needs draft criteria."""
    model_a = ModelAssessment(
        ready=False,
        goal="Make it better",
        expected_behavior="",
        out_of_scope=[],
        questions=["What does better mean?", "For whom?"],
        assumptions=[],
        criteria=[Criterion(surface="/", action="improve", expected="better", kind="functional")],
    )
    intake = Intake(
        title="Improve",
        description="Vague card",
        criteria=[],
        clarifications=[],
        answered_rounds=0,
    )

    assessment, _record = run_gate(model_a, intake)

    assert assessment.ready is False
    assert len(assessment.questions) == 2
    assert assessment.criteria_source == "assessor"


def test_gate_integration_answered_card():
    """Card with two answered rounds: force ready."""
    model_a = ModelAssessment(
        ready=False,
        goal="Do it",
        expected_behavior="Works",
        out_of_scope=[],
        questions=["Why?"],
        assumptions=[],
        criteria=[Criterion(surface="/", action="test", expected="pass", kind="functional")],
    )
    intake = Intake(
        title="Task",
        description="Something",
        criteria=[],
        clarifications=[
            {"question": "What?", "answer": "This"},
            {"question": "When?", "answer": "Now"},
        ],
        answered_rounds=2,
    )

    assessment, _record = run_gate(model_a, intake)

    assert assessment.ready is True
    assert assessment.questions == []
    assert len(assessment.assumptions) > 0


def test_dedupe_runs_before_the_cap_so_an_answered_question_does_not_take_a_slot():
    """Three questions, the first already answered: both new ones survive.
    With the cap first, the third question would have been lost."""
    from types import SimpleNamespace

    from performer.workflows.assessor.gate import run_gate
    from performer.workflows.assessor.models import ModelAssessment

    model = ModelAssessment(
        ready=False, goal="Rework the services page copy.", expected_behavior="Visitors understand the offer.",
        out_of_scope=[], assumptions=[], criteria=[],
        questions=["Which audience is this page for?", "Should pricing be shown?", "Is a contact form wanted?"],
    )
    intake = SimpleNamespace(
        answered_rounds=1,
        clarifications=[ClarificationRound(question="Which audience?", answer="Prospective clients")],
        criteria=["Visitors can read the offer"],
    )
    assessment, record = run_gate(model, intake)
    assert assessment.questions == ["Should pricing be shown?", "Is a contact form wanted?"]
    assert record.questions_dropped_as_answered == ["Which audience is this page for?"]
    assert record.questions_dropped_by_cap == []
    assert record.questions_kept == 2
