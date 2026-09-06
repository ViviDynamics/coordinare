"""Blueprint step (spec 165 FR-005): one schema-guarded model call.

Reuses 164's guards through the toolkit: one truncation retry at double budget
(``call_with_budget``) and one schema reprompt (``validate_with_reprompt``). A
blueprint that fails validation twice raises ``SchemaViolation`` naming the
fields; the workflow turns that into a performer error, never an empty plan.
"""
from __future__ import annotations

from performer.workflows.architect.models import Blueprint
from performer.workflows.architect.personas import BLUEPRINT
from performer.workflows.budget import Budget


async def run_blueprint_step(toolkit, intake_text: str, survey_text: str) -> Blueprint:
    content = [{"type": "text", "text": f"{intake_text}\n\n## Survey\n{survey_text}"}]
    return await toolkit.call_model(
        persona=BLUEPRINT,
        schema=Blueprint,
        content=content,
        budget=Budget.for_step("blueprint"),
    )
