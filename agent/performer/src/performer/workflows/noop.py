"""T018 — the contract-preservation workflow.

Runs nothing and reports nothing.  Its only job is to prove the layer is wired
correctly: a role configured with ``workflow: noop`` must still produce a valid
``PerformerResponse``, and removing the config line must reproduce the pre-164
behaviour exactly.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from performer.workflows.base import WorkflowResult

if TYPE_CHECKING:
    from performer.models import Score, Stand


class NoopWorkflow:
    """A workflow that does nothing, successfully."""

    name = "noop"
    #: 343: no user-visible sequence, so nothing to trail.
    steps: tuple[str, ...] = ()

    async def run(
        self,
        stand: "Stand",
        score: "Score",
        toolkit: Any,
    ) -> WorkflowResult:
        return WorkflowResult(report={"noop": True, "role": score.role})
