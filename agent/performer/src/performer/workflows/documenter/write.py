"""Documenter write step (spec 171 FR-006): one schema-guarded call per page."""
from __future__ import annotations

from pydantic import BaseModel

from performer.workflows.budget import Budget
from performer.workflows.documenter.models import PagePlan, model_page_write_schema
from performer.workflows.documenter.personas import render_write_persona

__all__ = ["run_write_step"]


async def run_write_step(toolkit, plan: PagePlan, *, current_content: str, evidence: str, changed_hunks: str, max_chars: int) -> BaseModel:
    schema = model_page_write_schema(max_chars)
    persona = render_write_persona(
        path=plan.path, kind=plan.kind or "reference", current_content=current_content[:max_chars], say=list(plan.say),
        evidence=evidence, changed_hunks=changed_hunks, max_chars=max_chars,
        allow_retire=plan.source == "inventory" and plan.exists,  # retire is only ever accepted for such a page (FR-006)
    )
    content = [{"type": "text", "text": f"Write or judge the page {plan.path} now and return the JSON."}]
    return await toolkit.call_model(persona=persona, schema=schema, content=content, budget=Budget.for_step("doc_write"))
