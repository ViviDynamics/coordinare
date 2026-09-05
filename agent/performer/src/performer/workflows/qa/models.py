"""QA-specific models (spec 164 T020).

Layer-level types (ExecutedCheck, Observation) live in
``performer.workflows.models`` — see the note there.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from performer.workflows.models import Observation

#: What a plan check can be. Closed: an unrecognised kind is a schema
#: violation, not an invitation to improvise.
CheckKind = Literal["command", "flow", "visual"]

#: What a flow step may do. Deliberately small — the model decides WHAT to do,
#: our Playwright driver owns HOW. A multi-step flow is strictly harder than the
#: single screenshot that already needed a deterministic backstop (qa_capture).
FlowAction = Literal["goto", "fill", "click", "expect_text", "screenshot"]

Severity = Literal["critical", "high", "medium", "low"]

FindingCategory = Literal[
    "unmet_criterion",
    "unexpected_regression",
    "misplaced_implementation",
    "no_functional_change",
    "step_unavailable",
]


class FlowStep(BaseModel):
    """One declarative UI step.

    `target` and `value` carry different things and the model must be told
    which is which: the first live eval run produced
    ``{"action": "goto", "target": null, "value": "/signin"}`` -- the URL in the
    wrong field -- because both were bare optional strings.
    """

    action: FlowAction = Field(
        ..., description="What to do. One of the listed actions only."
    )
    target: str | None = Field(
        default=None,
        description=(
            "WHERE to act. For 'goto' this is the URL or path (e.g. '/signin'). "
            "For 'fill' and 'click' it is a CSS selector (e.g. '#email'). For "
            "'screenshot' it may be an output path. Never free text."
        ),
    )
    value: str | None = Field(
        default=None,
        description=(
            "The text involved. For 'fill' the text to type; for 'expect_text' "
            "the text that must appear on the page. Leave null for 'goto', "
            "'click' and 'screenshot' -- a destination belongs in target."
        ),
    )


class PlanCheck(BaseModel):
    id: str = Field(..., description="Short unique slug for this check, e.g. 'signin-workspace'.")
    criterion: str = Field(
        ...,
        description=(
            "The acceptance criterion this check demonstrates, copied VERBATIM "
            "from the criteria given. A check serving no stated criterion is "
            "discarded."
        ),
    )
    kind: CheckKind = Field(
        ...,
        description=(
            "'command' to run a shell command (preferred when one can "
            "demonstrate the criterion), 'flow' to drive the UI, 'visual' to "
            "compare the rendered page."
        ),
    )
    command: str | None = Field(
        default=None, description="Required when kind is 'command'. A shell command."
    )
    steps: list[FlowStep] = Field(
        default_factory=list,
        description="Required when kind is 'flow'. Ordered step objects, not prose.",
    )

    def is_visual_or_flow(self) -> bool:
        return self.kind in ("flow", "visual")


class TestPlan(BaseModel):
    """What QA intends to check, produced BEFORE anything executes.

    Emitted as an event so it is inspectable, and treated as evidence: a
    criterion with no plan entry and no executed check cannot be counted as
    passed, which folds into the existing evidence floor rather than inventing
    a second gate.
    """

    #: Not a pytest test class, despite the name.
    __test__ = False

    checks: list[PlanCheck] = Field(default_factory=list)
    surfaces: list[str] = Field(default_factory=list)

    def needs_baseline(self) -> bool:
        """Baseline costs a second boot; skip it when nothing visual is planned."""
        return any(c.is_visual_or_flow() for c in self.checks)

    def criteria(self) -> set[str]:
        return {c.criterion for c in self.checks}


class VisualDelta(BaseModel):
    added: list[Observation] = Field(default_factory=list)
    removed: list[Observation] = Field(default_factory=list)
    layout_defects: list[str] = Field(default_factory=list)

    def is_empty(self) -> bool:
        return not self.added and not self.removed


class Finding(BaseModel):
    """The repair brief entry.

    Shape-compatible with scanner_findings so the existing dedup key
    (file, line, category) and merge path apply unchanged.

    INVARIANT: no field carries a prescribed fix (FR-014). This reports what
    failed and how to reproduce it. Prescribing the fix would have QA doing the
    implementer's job with less context.
    """

    file: str | None = None
    line: int | None = None
    category: FindingCategory
    severity: Severity = "high"
    criterion: str = ""
    plan_check_id: str | None = None
    expected: str = ""
    observed: str = ""
    evidence: dict | None = None
    repro_command: str | None = None


class CriterionVerdict(BaseModel):
    criterion: str
    passed: bool
    plan_check_ids: list[str] = Field(default_factory=list)
    note: str = ""


class JudgeOutput(BaseModel):
    """Step 5's model-facing schema."""

    criteria: list[CriterionVerdict] = Field(default_factory=list)
    delta_matches_expected: bool = False
    unexpected_changes: list[str] = Field(default_factory=list)
