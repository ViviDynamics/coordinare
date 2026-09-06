"""165 FR-003: intake assembles without a model call and reads only what exists."""
from __future__ import annotations

from types import SimpleNamespace

from performer.workflows.architect.intake import build_intake


def _score(**over):
    base = dict(
        title="Add categories",
        description="Time entries need a category.",
        acceptance_criteria=["A select appears", "It is required"],
        clarifications=[{"question": "Which page?", "answer": "The entry form"}, {"question": "x", "answer": ""}],
        issue_number=162,
    )
    base.update(over)
    return SimpleNamespace(**base)


def test_intake_reads_assessment_and_agent_instructions_when_present(tmp_path):
    (tmp_path / "docs" / "cards" / "162-add-categories").mkdir(parents=True)
    (tmp_path / "docs" / "cards" / "162-add-categories" / "assessment.md").write_text("Sufficient. Touches TimeEntry.")
    (tmp_path / "AGENTS.md").write_text("Run rubocop before committing.")

    intake = build_intake(_score(), tmp_path)

    text = intake.as_text()
    assert "Sufficient. Touches TimeEntry." in text
    assert "Run rubocop" in text
    assert "- A select appears" in text
    assert "Q: Which page?" in text and "A: The entry form" in text
    assert "Q: x" not in text, "unanswered clarifications are not intake"


def test_intake_without_files_is_still_complete(tmp_path):
    intake = build_intake(_score(), tmp_path)
    assert intake.assessment == "" and intake.agent_instructions == ""
    assert "# Card: Add categories" in intake.as_text()


def test_large_documents_are_capped(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("x" * 20000)
    intake = build_intake(_score(), tmp_path)
    assert len(intake.agent_instructions) == 6000
