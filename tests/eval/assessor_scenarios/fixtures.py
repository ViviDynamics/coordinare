"""Three fixture cards for the assessor workflow eval (spec 166 SC-006).

Each fixture carries the card as the assessor would receive it, the canned
model assessment the stub returns, and the expectations scoring checks.
Deterministic by construction; the live mode swaps the stub for the gateway
and keeps the expectations.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Expectation:
    """Scoring expectations for the fixture."""

    ready: bool
    min_questions: int
    max_questions: int
    min_criteria: int
    max_criteria: int
    criteria_source: str  # "card" or "assessor"
    blocked: bool  # True if the report should be blocked, False if complete


@dataclass(frozen=True)
class Fixture:
    """A fixture card for assessor eval."""

    name: str
    title: str
    description: str
    criteria: list[str]
    clarifications: list[dict]  # [{"question": str, "answer": str}, ...]
    model_assessment: dict  # What the stub model returns for this fixture
    expect: Expectation


CLEAR = Fixture(
    name="clear",
    title="Fix typo in contact page heading",
    description="The contact page heading reads 'Contct us'. It should read 'Contact us'.",
    criteria=["The contact page heading reads 'Contact us'"],
    clarifications=[],
    model_assessment={
        "ready": True,
        "goal": "Fix the typo in the contact page heading from 'Contct us' to 'Contact us'.",
        "expected_behavior": "When users visit the contact page, the heading displays the correct spelling.",
        "out_of_scope": [],
        "questions": [],
        "assumptions": [],
        "criteria": [],
    },
    expect=Expectation(
        ready=True,
        min_questions=0,
        max_questions=0,
        min_criteria=0,
        max_criteria=0,
        criteria_source="card",
        blocked=False,
    ),
)

AMBIGUOUS = Fixture(
    name="ambiguous",
    title="Make the services page better",
    description="The services page needs improvement but the exact goal is unclear.",
    criteria=[],
    clarifications=[],
    model_assessment={
        "ready": False,
        "goal": "Improve the services page presentation and content.",
        "expected_behavior": "",
        "out_of_scope": [],
        "questions": [
            "Which audience is this page intended for: prospective clients, existing customers, or both?",
            "Are we improving the visual design, the content clarity, or both?",
            "What specific metrics define success (conversion rate, engagement, time-on-page)?",
            "Should we also update the services pricing or just the descriptions?",
        ],
        "assumptions": [],
        "criteria": [
            {"surface": "/services", "action": "view page", "expected": "improved presentation", "kind": "visual"},
            {"surface": "/services", "action": "review content", "expected": "clarity improved", "kind": "functional"},
        ],
    },
    expect=Expectation(
        ready=False,
        min_questions=1,
        max_questions=2,
        min_criteria=1,
        max_criteria=8,
        criteria_source="assessor",
        blocked=True,
    ),
)

ANSWERED = Fixture(
    name="answered",
    title="Update the services page for the new year",
    description="Refresh the services page with updated information for the new year.",
    criteria=[],
    clarifications=[
        {
            "question": "Who is the primary audience for the services page?",
            "answer": "Prospective clients and businesses looking to understand our offerings.",
        },
        {
            "question": "Should we update pricing information as well?",
            "answer": "Yes, pricing should be updated to reflect the new year rates.",
        },
    ],
    model_assessment={
        "ready": False,
        "goal": "Update the services page with new year information for prospective business clients.",
        "expected_behavior": "The services page displays updated descriptions, pricing, and any new service offerings.",
        "out_of_scope": ["Site-wide redesign", "Blog updates"],
        "questions": [
            "Should we include case studies or testimonials on the updated page?",
        ],
        "assumptions": [],
        "criteria": [
            {"surface": "/services", "action": "visit the page", "expected": "content reflects new year", "kind": "functional"},
            {"surface": "/services", "action": "review pricing", "expected": "pricing updated to new year rates", "kind": "functional"},
        ],
    },
    expect=Expectation(
        ready=True,
        min_questions=0,
        max_questions=0,
        min_criteria=1,
        max_criteria=8,
        criteria_source="assessor",
        blocked=False,
    ),
)

FIXTURES: tuple[Fixture, ...] = (CLEAR, AMBIGUOUS, ANSWERED)
