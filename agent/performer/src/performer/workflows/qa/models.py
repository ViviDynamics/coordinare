"""QA-specific models (spec 164 T020).

Layer-level types (ExecutedCheck, Observation) live in
``performer.workflows.models`` — see the note there.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, model_validator

from performer.workflows.models import Observation

#: What a plan check can be. Closed: an unrecognised kind is a schema
#: violation, not an invitation to improvise.
CheckKind = Literal["command", "flow", "visual"]

#: What a flow step may do. Deliberately small — the model decides WHAT to do,
#: our Playwright driver owns HOW. A multi-step flow is strictly harder than the
#: single screenshot that already needed a deterministic backstop (qa_capture).
FlowAction = Literal["goto", "fill", "click", "expect_text", "screenshot", "http_assert"]


def normalise_criterion(text: str | None) -> str:
    """The one criterion normalisation, shared by the judge and the binder.

    Two implementations drifted: the judge compared normalised text while
    plan.py bound checks with exact equality, so a plan quoting a criterion
    with different spacing dropped every check as 'unbound' and the run
    failed closed on a plan that described the card (411).
    """
    return " ".join((text or "").split()).casefold()

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
        ..., description="What to do. One of the listed actions only.",
    )
    target: str | None = Field(
        default=None,
        description=(
            "WHERE to act. For 'goto' this is the URL or path (e.g. '/signin'). "
            "For 'fill' and 'click' it is a CSS selector (e.g. '#email'). For "
            "'screenshot' it may be an output path. For 'http_assert' it is "
            "the URL or path of an API endpoint. Never free text."
        ),
    )
    value: str | None = Field(
        default=None,
        description=(
            "The text involved. For 'fill' the text to type; for 'expect_text' "
            "the text that must appear on the page. For 'http_assert' a JSON "
            "object of assertions: status, contains, json_path and equals "
            "(e.g. '{\"status\": 200, \"json_path\": \"$.ok\", \"equals\": true}'). "
            "Leave null for 'goto', 'click' and 'screenshot' -- a destination "
            "belongs in target."
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
        default=None, description="Required when kind is 'command'. A shell command.",
    )
    steps: list[FlowStep] = Field(
        default_factory=list,
        description="Required when kind is 'flow'. Ordered step objects, not prose.",
    )

    @model_validator(mode="after")
    def _reject_checks_that_cannot_run(self) -> PlanCheck:
        """A check that cannot run is a schema error, not a pass.

        execute.py used to substitute `command or "true"` for a missing
        command and ran zero-step flows the same way: both got exit 0, so a
        malformed plan read as a demonstration (411). A bare visual check is
        deliberately NOT rejected — it is a plan to observe a page, and the
        harness appends the screenshot capture itself.
        """
        if self.kind == "command" and not (self.command or "").strip():
            raise ValueError(
                "a 'command' check requires a non-empty shell command",
            )
        if self.kind == "flow" and not self.steps:
            raise ValueError(
                "a 'flow' check requires at least one step",
            )
        return self

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

    @model_validator(mode="after")
    def _reject_duplicate_or_empty_check_ids(self) -> TestPlan:
        """Check ids name evidence and executed-check records.

        Two checks sharing an id share one evidence file — one capture would
        satisfy both — and an empty id names nothing at all. Both are plan
        schema errors, refused at the boundary rather than reconciled after
        execution (411 round-seven review).
        """
        ids = [c.id for c in self.checks]
        if len(ids) != len(set(ids)):
            raise ValueError("check ids must be unique: two checks sharing an id share one evidence file")
        if any(not str(i).strip() for i in ids):
            raise ValueError("check ids must be non-empty")
        return self

    def needs_baseline(self) -> bool:
        """Baseline costs a second boot; skip it when nothing visual is planned.

        A flow that drives the UI compares rendered output against the merge
        base. An http_assert-only flow performs no DOM comparison — the
        assertion runs against the head revision, so booting the old one adds
        a failure mode without buying comparison value, and an unbootable old
        revision env-blocked a check the head could answer (411 round-seven
        review)."""
        for c in self.checks:
            if c.kind == "visual":
                return True
            if c.kind == "flow" and any(s.action != "http_assert" for s in c.steps):
                return True
        return False

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
