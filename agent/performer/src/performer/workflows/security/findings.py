"""Security findings call (spec 170 FR-007) and the one re-anchor call (FR-008).

One schema-guarded model call under the ``security_findings`` budget; the
toolkit reprompts once on a schema violation and raises on the second. The
schema pins ``category`` to the configured set, requires ``introduced_by``, and
forbids severity, routing and verdict keys.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel

from performer.workflows.budget import Budget
from performer.workflows.reviewer.findings import brief_summary
from performer.workflows.reviewer.models import ChangedFile
from performer.workflows.security.intake import SecurityIntake
from performer.workflows.security.models import SecurityFinding, model_security_findings_schema
from performer.workflows.security.personas import FINDINGS_PERSONA, REANCHOR_PERSONA, render_scan_findings

__all__ = ["run_findings_step", "run_reanchor_step"]

_DIFF_CHARS = 60000


async def run_findings_step(toolkit, intake: SecurityIntake, scan_findings: list[dict[str, Any]], survey_notes: str, categories: tuple[str, ...], max_findings: int) -> BaseModel:
    schema = model_security_findings_schema(categories, max_findings)
    persona = FINDINGS_PERSONA.format(
        categories=", ".join(categories),
        diff=intake.diff_text[:_DIFF_CHARS],
        scan_findings=render_scan_findings(scan_findings),
        survey_notes=survey_notes[-20000:] or "(no survey output)",
        brief_summary=brief_summary(intake.brief),
    )
    content = [{"type": "text", "text": "Perform the taint analysis over the pull request above and return the findings JSON."}]
    return await toolkit.call_model(persona=persona, schema=schema, content=content, budget=Budget.for_step("security_findings"))


async def run_reanchor_step(toolkit, dropped: list[SecurityFinding], changed_files: list[ChangedFile], categories: tuple[str, ...], max_findings: int, context: str) -> BaseModel:
    schema = model_security_findings_schema(categories, max_findings)
    listing = "\n".join(f"- {f.path}:{f.line} [{f.category}] introduced by {f.introduced_by}: {f.problem[:200]} (evidence: {f.evidence[:100]!r})" for f in dropped)
    persona = REANCHOR_PERSONA.format(dropped_findings=listing, changed_files="\n".join(f"- {f.path}" for f in changed_files))
    content = [{"type": "text", "text": context[-40000:] + "\n\nReturn the re-anchored findings JSON."}]
    return await toolkit.call_model(persona=persona, schema=schema, content=content, budget=Budget.for_step("security_findings"))
