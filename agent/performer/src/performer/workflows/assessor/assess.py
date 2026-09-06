"""Assessor assess step (spec 166 FR-002, US1): one schema-guarded model call.

Calls the model with the assessment schema under a 3000 token budget. On a
schema violation, reprompts once at double budget. Raises on persistent
violation or budget overflow.
"""
from __future__ import annotations

from performer.workflows.assessor.models import ModelAssessment
from performer.workflows.assessor.personas import ASSESS
from performer.workflows.budget import Budget


async def run_assess_step(toolkit, intake_text: str) -> ModelAssessment:
    """Make one schema-guarded model call.

    Args:
        toolkit: The execution toolkit.
        intake_text: The card context text from the intake step.

    Returns:
        ModelAssessment with ready, goal, expected_behavior, out_of_scope,
        questions, assumptions, criteria.

    Raises:
        SchemaViolation: If the model returns invalid output twice.
    """
    content = [{"type": "text", "text": intake_text}]
    return await toolkit.call_model(
        persona=ASSESS,
        schema=ModelAssessment,
        content=content,
        budget=Budget.for_step("assess"),
    )
