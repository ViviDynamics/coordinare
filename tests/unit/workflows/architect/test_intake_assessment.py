"""Architect intake assessment rendering (spec 166 FR-014).

When the Score carries an assessment dict from the workflow, intake renders it
as the first section with goal, expected behaviour, out of scope, assumptions,
clarifications, and draft criteria (when criteria_source is assessor).
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from performer.workflows.architect.intake import Intake, build_intake


def _assessment_dict(criteria_source="assessor", criteria=None):
    """A sample assessment dict."""
    if criteria is None:
        criteria = [
            {"surface": "/api/users", "action": "POST", "expected": "user created", "kind": "functional"},
        ]
    return {
        "ready": True,
        "goal": "Enable user registration",
        "expected_behavior": "New users can create accounts",
        "out_of_scope": ["Email verification", "Password reset"],
        "questions": [],
        "assumptions": ["Admin can later manage users"],
        "criteria": criteria,
        "criteria_source": criteria_source,
        "clarifications": [
            {"question": "Which auth method?", "answer": "Local username/password"},
        ],
        "assessment_hash": "a" * 64,
    }


def test_intake_renders_structured_assessment_first():
    """With an assessment dict, as_text renders it as the first section after
    the card title and description."""
    intake = Intake(
        title="User registration",
        description="Add local auth",
        assessment=_assessment_dict(),
        criteria=["Option 1", "Option 2"],
    )
    text = intake.as_text()

    lines = text.split("\n")
    assert lines[0] == "# Card: User registration"
    assert "Add local auth" in text
    # Assessment comes before criteria
    assert text.index("## Assessment") < text.index("## Acceptance criteria")
    assert "Enable user registration" in text
    assert "New users can create accounts" in text


def test_assessment_section_contains_all_parts():
    """The assessment section includes goal, expected behaviour, out of scope,
    assumptions, clarifications, and draft criteria."""
    intake = Intake(
        title="Test",
        description="",
        assessment=_assessment_dict(),
    )
    text = intake.as_text()

    assert "Goal: Enable user registration" in text
    assert "Expected behaviour: New users can create accounts" in text
    assert "Out of scope:\n- Email verification" in text
    assert "Assumptions:\n- Admin can later manage users" in text
    assert "Clarifications:\n- Q: Which auth method?" in text
    assert "A: Local username/password" in text


def test_draft_criteria_labelled_when_assessor_source():
    """When criteria_source is assessor, criteria are labelled as a draft to
    refine into the verification brief."""
    intake = Intake(
        title="Test",
        description="",
        assessment=_assessment_dict(criteria_source="assessor"),
    )
    text = intake.as_text()

    assert "Draft acceptance criteria (refine these into the verification brief):" in text
    assert "/api/users: POST -> user created [functional]" in text


def test_no_criteria_section_when_card_source():
    """When criteria_source is card, no draft criteria section is rendered
    (the card's own criteria render in the standard Acceptance criteria section)."""
    intake = Intake(
        title="Test",
        description="",
        criteria=["User can register"],
        assessment=_assessment_dict(criteria_source="card", criteria=[]),
    )
    text = intake.as_text()

    assert "Draft acceptance criteria" not in text
    # The card's criteria render in the standard section
    assert "## Acceptance criteria\n- User can register" in text


def test_as_text_unchanged_when_no_assessment():
    """When assessment is empty or not present, as_text renders the same as before
    (card title, description, criteria, clarifications, agent instructions)."""
    intake_no_assessment = Intake(
        title="Test card",
        description="Do something",
        criteria=["Criterion 1"],
    )
    text = intake_no_assessment.as_text()

    assert "# Card: Test card" in text
    assert "Do something" in text
    assert "## Acceptance criteria" in text
    assert "- Criterion 1" in text
    assert "## Assessment" not in text


def test_build_intake_prefers_workflow_assessment():
    """build_intake checks for assessment in the Score first (workflow report),
    before falling back to the workspace."""
    tmp_path = Path("/tmp/test")
    workflow_assessment = _assessment_dict()
    score = SimpleNamespace(
        title="Card",
        description="Desc",
        assessment=workflow_assessment,
        acceptance_criteria=[],
        clarifications=[],
        issue_number=42,
        doc_folder=None,
    )

    intake = build_intake(score, tmp_path)

    assert intake.assessment == workflow_assessment


def test_build_intake_fallback_to_workspace_when_no_assessment_in_score():
    """When Score has no assessment, build_intake looks in the workspace."""
    tmp_path = Path("/tmp/test")
    score = SimpleNamespace(
        title="Card",
        description="Desc",
        acceptance_criteria=[],
        clarifications=[],
        issue_number=None,
        doc_folder=None,
    )

    intake = build_intake(score, tmp_path)

    # No assessment found (path doesn't exist)
    assert intake.assessment == ""


def test_empty_assessment_dict_renders_no_section():
    """An empty or minimal assessment dict renders nothing."""
    intake = Intake(
        title="Test",
        description="",
        assessment={"goal": "", "expected_behavior": ""},  # Empty fields
    )
    text = intake.as_text()

    # Should not have Assessment section if all fields are empty
    assert "## Assessment" not in text or "##" not in text.split("## Assessment", 1)[1].split("\n")[0]


def test_string_assessment_still_works():
    """A string assessment (legacy prose path) is rendered as before."""
    prose = "# My Assessment\n\nThis is prose assessment."
    intake = Intake(
        title="Test",
        description="",
        assessment=prose,
    )
    text = intake.as_text()

    assert "## Assessment\n# My Assessment" in text
    assert "This is prose assessment." in text


def test_clarifications_without_answers_not_rendered():
    """Clarifications with no answer (or empty answer) are not rendered in the
    assessment section."""
    assessment = _assessment_dict()
    assessment["clarifications"] = [
        {"question": "Answered?", "answer": "Yes"},
        {"question": "No answer?", "answer": ""},  # Empty answer
        {"question": "Missing answer", "answer": None},
    ]
    intake = Intake(
        title="Test",
        description="",
        assessment=assessment,
    )
    text = intake.as_text()

    assert "Answered?" in text
    assert "No answer?" not in text
    assert "Missing answer" not in text
