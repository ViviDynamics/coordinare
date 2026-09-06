"""The stubbed assessor eval is deterministic, so it runs under pytest: every
fixture must pass its expectations (SC-006)."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from performer.workflows import get_workflow
from performer.workflows._text import matches_answered
from performer.workflows.base import WorkflowMetrics
from performer.workflows.toolkit import Toolkit

from tests.eval.assessor_scenarios.fixtures import FIXTURES, Fixture
from tests.eval.assessor_scenarios.scoring import score_run
from tests.eval.assessor_scenarios.stub_model import stub_model_for


async def run_assessor_fixture(fixture: Fixture) -> dict:
    """Run the assessor workflow against a fixture.

    Args:
        fixture: The fixture card.

    Returns:
        The PerformerResponse report dict.
    """
    model_call = stub_model_for(fixture)
    toolkit = Toolkit(
        metrics=WorkflowMetrics(),
        model_call=model_call,
        command_runner=None,
        call_limit=12,
    )

    score = SimpleNamespace(
        title=fixture.title,
        description=fixture.description,
        acceptance_criteria=list(fixture.criteria),
        clarifications=list(fixture.clarifications),
        prior_clarifications=[],
        issue_number=None,
        workflow_env={},
    )

    workflow = get_workflow("assessor")
    result = await workflow.run(
        SimpleNamespace(path="."),
        score,
        toolkit,
    )
    return result.report


@pytest.mark.asyncio
@pytest.mark.parametrize("fixture", FIXTURES, ids=[f.name for f in FIXTURES])
async def test_fixture_meets_its_expectations(fixture: Fixture):
    """Every fixture must pass its expectations with the stubbed model."""
    report = await run_assessor_fixture(fixture)
    score = score_run(fixture, report, live=False)
    assert score.passed, score.notes


@pytest.mark.asyncio
async def test_no_reask_on_all_fixtures():
    """No fixture should ask a question that repeats an answered clarification."""
    for fixture in FIXTURES:
        report = await run_assessor_fixture(fixture)
        assessment = report.get("assessment", {})
        asked_questions = assessment.get("questions", [])

        reasks = []
        for asked in asked_questions:
            for clarif in fixture.clarifications:
                if matches_answered(asked, clarif.get("question", ""), threshold=0.6):
                    reasks.append(asked)

        assert len(reasks) == 0, f"{fixture.name}: questions re-ask answered: {reasks}"


def test_scoring_flags_mismatched_ready():
    """The scorer must be able to fail on ready mismatch."""
    from tests.eval.assessor_scenarios.fixtures import CLEAR

    report = {
        "assessment": {
            "ready": False,  # Contradicts CLEAR's expectation
            "goal": "Fix the typo",
            "expected_behavior": "Heading is correct",
            "out_of_scope": [],
            "questions": ["What exactly needs fixing?"],
            "assumptions": [],
            "criteria": [],
            "criteria_source": "card",
            "clarifications": [],
            "assessment_hash": "a" * 64,
        },
        "write_free_check": {"passed": True, "commands_run": 0},
    }
    score = score_run(CLEAR, report, live=False)
    assert not score.checks["ready"]
    assert score.passed is False


def test_scoring_flags_reask():
    """The scorer must detect when a question re-asks an answered clarification."""
    from tests.eval.assessor_scenarios.fixtures import ANSWERED

    report = {
        "assessment": {
            "ready": False,
            "goal": "Update the services page",
            "expected_behavior": "Services page is updated",
            "out_of_scope": [],
            "questions": [
                "Who is the primary audience for the services page?"
            ],  # This re-asks an answered clarification
            "assumptions": [],
            "criteria": [],
            "criteria_source": "assessor",
            "clarifications": ANSWERED.clarifications,
            "assessment_hash": "b" * 64,
        },
        "write_free_check": {"passed": True, "commands_run": 0},
    }
    score = score_run(ANSWERED, report, live=False)
    assert not score.checks["no_reask"]
    assert score.passed is False
