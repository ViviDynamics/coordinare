"""Reviewer findings call (spec 169 FR-005) and the one re-anchor call (FR-006).

One schema-guarded model call under the ``findings`` budget; the toolkit
reprompts once on a schema violation and raises on the second. The schema
pins ``category`` to the configured set and forbids a verdict key.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from performer.workflows.budget import Budget
from performer.workflows.reviewer.intake import Intake
from performer.workflows.reviewer.models import ChangedFile, Finding, model_findings_schema
from performer.workflows.reviewer.personas import REANCHOR_PERSONA, REVIEW_PERSONA, render_prior_comments

__all__ = ["run_findings_step", "run_reanchor_step", "brief_summary"]

_DIFF_CHARS = 60000


def brief_summary(brief: dict[str, Any]) -> str:
    if not brief:
        return "(no implementation brief; the documentation rule does not apply)"
    parts = []
    for key in ("summary", "goal", "work_kind"):
        if brief.get(key):
            parts.append(f"{key}: {str(brief[key])[:500]}")
    milestones = brief.get("milestones") or []
    if isinstance(milestones, list) and milestones:
        parts.append("milestones: " + "; ".join(str(m.get("goal", ""))[:120] for m in milestones if isinstance(m, dict))[:1500])
    return "\n".join(parts) or "(brief present)"


async def run_findings_step(toolkit, intake: Intake, survey_notes: str, categories: tuple[str, ...], max_findings: int) -> BaseModel:
    schema = model_findings_schema(categories, max_findings)
    persona = REVIEW_PERSONA.format(
        categories=", ".join(categories),
        diff=intake.diff_text[:_DIFF_CHARS],
        prior_comments=render_prior_comments(intake.prior_comments),
        survey_notes=survey_notes[-20000:] or "(no survey output)",
        brief_summary=brief_summary(intake.brief),
    )
    content = [{"type": "text", "text": "Review the pull request above and return the findings JSON."}]
    return await toolkit.call_model(persona=persona, schema=schema, content=content, budget=Budget.for_step("findings"))


async def run_reanchor_step(toolkit, dropped: list[Finding], changed_files: list[ChangedFile], categories: tuple[str, ...], max_findings: int, context: str) -> BaseModel:
    """One re-anchor call for the dropped findings; its output is gated again."""
    schema = model_findings_schema(categories, max_findings)
    listing = "\n".join(f"- {f.path}:{f.line} [{f.category}] {f.problem[:200]} (evidence: {f.evidence[:100]!r})" for f in dropped)
    persona = REANCHOR_PERSONA.format(dropped_findings=listing, changed_files="\n".join(f"- {f.path}" for f in changed_files))
    content = [{"type": "text", "text": context[-40000:] + "\n\nReturn the re-anchored findings JSON."}]
    return await toolkit.call_model(persona=persona, schema=schema, content=content, budget=Budget.for_step("findings"))
