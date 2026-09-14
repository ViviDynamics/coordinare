"""Value objects shared by the workflow layer.

NOTE (deviation from data-model.md): ``ExecutedCheck`` and ``Observation`` are
specified there under the QA workflow, but ``contracts/role_workflow.md`` has the
generic Toolkit returning both.  They are layer-level types, so they live here;
QA-specific models (TestPlan, PlanCheck, FlowStep, VisualDelta, Finding) stay in
``workflows/qa/models.py``.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel

#: Closed enum for observed UI elements.  Anything outside it is a schema
#: violation, not an improvisation (FR-010).
ElementKind = Literal[
    "heading", "text_input", "password_input", "dropdown",
    "button", "link", "banner", "image", "other",
]


class ExecutedCheck(BaseModel):
    """Evidence that something actually ran.

    ``exit_code`` is always real.  Nothing in the layer infers success from a
    model's opinion — that conflation is what spec 120 exists to prevent.
    """

    plan_check_id: str = ""
    command: str
    exit_code: int
    output_excerpt: str = ""
    passed: bool

    @classmethod
    def from_result(cls, command: str, exit_code: int, output: str,
                    *, plan_check_id: str = "", output_budget: int = 2000) -> "ExecutedCheck":
        return cls(
            plan_check_id=plan_check_id,
            command=command,
            exit_code=exit_code,
            # A budget below zero must not become a Python negative slice,
            # which would return nearly the whole output.
            output_excerpt=output[: max(output_budget, 0)],
            passed=exit_code == 0,
        )


class Observation(BaseModel):
    """One element seen on a rendered page.

    ``label`` comes from the DOM, never from the model: pooling repeated model
    descriptions on free-text labels discarded the feature under test during
    design (three runs named one dropdown three different ways).  The pooling
    key is ``(kind, position)`` (FR-011).
    """

    kind: ElementKind
    position: int
    label: str | None = None
    seen_in: int = 1

    @property
    def pool_key(self) -> tuple[str, int]:
        return (self.kind, self.position)
