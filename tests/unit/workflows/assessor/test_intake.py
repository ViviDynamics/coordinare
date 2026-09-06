"""T008 - Assessor intake step (spec 166 FR-001).

No model call. Reads the card and clarifications, logs the answered-round
count, and returns the intake text.
"""
from __future__ import annotations

from dataclasses import dataclass

from performer.workflows.assessor.intake import build_intake


@dataclass
class FakeScore:
    """Minimal Score mock for testing."""
    title: str = "Do something"
    description: str = "Make it work"
    acceptance_criteria: list[str] = None
    clarifications: list[dict] = None
    prior_clarifications: list[dict] = None

    def __post_init__(self):
        if self.acceptance_criteria is None:
            self.acceptance_criteria = []
        if self.clarifications is None:
            self.clarifications = []
        if self.prior_clarifications is None:
            self.prior_clarifications = []


def test_build_intake_minimal():
    """Minimal score builds intake."""
    score = FakeScore()
    intake = build_intake(score)
    text = intake.as_text()
    assert "Do something" in text
    assert "Make it work" in text
    assert intake.answered_rounds == 0


def test_build_intake_with_acceptance_criteria():
    """Acceptance criteria are included."""
    score = FakeScore(
        acceptance_criteria=["Must work", "Must be fast"]
    )
    intake = build_intake(score)
    text = intake.as_text()
    assert "Must work" in text
    assert "Must be fast" in text


def test_build_intake_counts_answered_rounds():
    """Answered round count: both question and answer non-blank."""
    score = FakeScore(
        clarifications=[
            {"question": "What's the goal?", "answer": "Make it work"},
            {"question": "Why?", "answer": ""},  # Blank answer, not counted
            {"question": "How?", "answer": "By coding"},
        ]
    )
    intake = build_intake(score)
    assert intake.answered_rounds == 2


def test_build_intake_merges_clarifications():
    """Clarifications and prior_clarifications are merged."""
    score = FakeScore(
        clarifications=[
            {"question": "First?", "answer": "Yes"},
        ],
        prior_clarifications=[
            {"question": "Second?", "answer": "Also yes"},
        ]
    )
    intake = build_intake(score)
    text = intake.as_text()
    assert "First?" in text
    assert "Yes" in text
    assert "Second?" in text
    assert "Also yes" in text


def test_build_intake_deduplicates_questions():
    """Same question (normalized) appears once."""
    score = FakeScore(
        clarifications=[
            {"question": "What's the goal?", "answer": "Make it work"},
            {"question": "What's the goal?", "answer": "Make it work"},
        ]
    )
    intake = build_intake(score)
    text = intake.as_text()
    # Should appear only once
    assert text.count("What's the goal?") == 1


def test_build_intake_blank_answers_not_counted():
    """Blank answers do not count as answered rounds."""
    score = FakeScore(
        clarifications=[
            {"question": "Q1?", "answer": "A1"},
            {"question": "Q2?", "answer": ""},
            {"question": "Q3?", "answer": "   "},  # Whitespace only
        ]
    )
    intake = build_intake(score)
    assert intake.answered_rounds == 1


def test_build_intake_as_text_format():
    """as_text() returns properly formatted text."""
    score = FakeScore(
        title="Fix bug",
        description="A serious bug",
        acceptance_criteria=["Pass tests"],
        clarifications=[{"question": "Which bug?", "answer": "The one in login"}]
    )
    intake = build_intake(score)
    text = intake.as_text()
    assert "# Card: Fix bug" in text
    assert "A serious bug" in text
    assert "## Acceptance criteria" in text
    assert "- Pass tests" in text
    assert "## Clarifications" in text
    assert "Which bug?" in text
    assert "The one in login" in text


def test_build_intake_latest_answer_wins():
    """When the same normalized question appears multiple times, the latest non-blank answer wins."""
    score = FakeScore(
        clarifications=[
            {"question": "What's the goal?", "answer": "First answer"},
            {"question": "What's the goal?", "answer": "Second answer"},  # Latest, should win
        ]
    )
    intake = build_intake(score)
    text = intake.as_text()
    assert "Second answer" in text
    assert "First answer" not in text


def test_build_intake_latest_answer_wins_with_blanks():
    """Latest non-blank answer wins even if followed by blank answers."""
    score = FakeScore(
        clarifications=[
            {"question": "What's the goal?", "answer": "First answer"},
            {"question": "What's the goal?", "answer": ""},  # Blank
            {"question": "What's the goal?", "answer": "Second answer"},  # Latest, should win
        ]
    )
    intake = build_intake(score)
    text = intake.as_text()
    assert "Second answer" in text
    assert "First answer" not in text


def test_build_intake_expands_plural_questions_entry():
    """Expand a {'questions': [...], 'answer': ''} entry into separate rounds."""
    score = FakeScore(
        clarifications=[
            {"question": "What's the goal?", "answer": "Make it work"},
            {"questions": ["Who is the user?", "What is the scope?"], "answer": ""},
        ]
    )
    intake = build_intake(score)
    text = intake.as_text()
    # The first clarification should appear
    assert "What's the goal?" in text
    assert "Make it work" in text
    # The plural questions should be expanded and appear as separate unanswered rounds
    assert "Who is the user?" in text
    assert "What is the scope?" in text
    # They should not have answers because they are unanswered
    assert intake.answered_rounds == 1  # Only the first one counts


def test_build_intake_plural_questions_mixed_list():
    """Mix single-question and plural-questions entries."""
    score = FakeScore(
        clarifications=[
            {"question": "First?", "answer": "Answered"},
            {"questions": ["Second?", "Third?"], "answer": ""},
            {"question": "Fourth?", "answer": "Answered"},
        ]
    )
    intake = build_intake(score)
    text = intake.as_text()
    assert "First?" in text and "Answered" in text
    assert "Second?" in text
    assert "Third?" in text
    assert "Fourth?" in text
    # Only the single-answer entries count
    assert intake.answered_rounds == 2
