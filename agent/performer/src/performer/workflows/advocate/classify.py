"""Advocate classification (spec 173): the run's only model step.

Issues are batched so a repository with many unhandled issues costs a bounded
number of calls rather than one per issue. The toolkit owns the budget, the
schema instruction and the reprompt; this module owns only what to ask.
"""
from __future__ import annotations

import structlog

from performer.workflows.advocate.models import (
    Classification,
    ClassificationBatch,
    IssueCandidate,
)
from performer.workflows.advocate.personas import CLASSIFY_PERSONA, render_call
from performer.workflows.budget import Budget

log = structlog.get_logger(__name__)


def batches(issues: list[IssueCandidate], size: int) -> list[list[IssueCandidate]]:
    """Split *issues* into calls of at most *size*."""
    step = max(1, size)
    return [issues[i:i + step] for i in range(0, len(issues), step)]


async def classify_issues(
    issues: list[IssueCandidate],
    documentation: str,
    toolkit: object,
    *,
    max_per_call: int,
) -> list[Classification]:
    """One guarded call per batch. Returns every classification that validated.

    A batch that cannot be validated is dropped with a log line rather than
    raising: one malformed batch must not discard the work of the others, and
    every issue it covered escalates by the gate's own rules, because no
    classification arrives for it.
    """
    results: list[Classification] = []
    for batch in batches(issues, max_per_call):
        content = [{"type": "text", "text": render_call(batch, documentation)}]
        try:
            parsed = await toolkit.call_model(  # type: ignore[attr-defined]
                persona=CLASSIFY_PERSONA,
                schema=ClassificationBatch,
                content=content,
                budget=Budget.for_step("advocate_classify"),
            )
        except Exception as exc:  # noqa: BLE001 - one bad batch must not sink the run
            log.warning(
                "advocate.batch_unusable",
                issues=[i.issue_id for i in batch],
                error=str(exc),
            )
            continue
        results.extend(parsed.classifications)
    return results
