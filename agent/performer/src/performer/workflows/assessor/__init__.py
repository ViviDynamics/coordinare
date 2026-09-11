"""Assessor role workflow (spec 166): intake, assess, gate, report.

The assessor reads a card and its clarifications, makes one schema-validated
assessment call, applies gate rules to bound the clarification loop, and commits
nothing. Coordinare records the assessment on the card and advances it to
architecting. The architect's intake renders the assessment first so the plan
starts from the product manager's reading rather than raw text.

Selected by ``workflow: assessor`` on the role and run behind the same
``WorkflowAdapter`` seam as QA (spec 164).
"""
from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

import structlog

from performer.models import BackendEvent, BackendEventType
from performer.workflows.assessor.assess import run_assess_step
from performer.workflows.assessor.gate import run_gate
from performer.workflows.assessor.intake import build_intake
from performer.workflows.assessor.report import build_report
from performer.workflows.base import WorkflowResult

if TYPE_CHECKING:
    from performer.models import Score, Stand

log = structlog.get_logger(__name__)

STEPS: tuple[str, ...] = ("intake", "assess", "gate", "report")


class AssessorWorkflow:
    """Sequences the four assessor steps; only code advances the sequence."""

    name = "assessor"
    #: 343: ordered steps, the single source for the wire and the latch.
    steps = STEPS

    @staticmethod
    def _step(toolkit, name: str, detail: str = "") -> None:
        """Emit a step transition event."""
        emit = getattr(toolkit, "emit", None)
        if callable(emit):
            emit(
                BackendEvent(
                    type=BackendEventType.progress,
                    text=f"assessor.{name}",
                    detail=detail,
                )
            )

    async def run(self, stand: "Stand", score: "Score", toolkit: Any) -> WorkflowResult:
        """Run the assessor workflow: intake, assess, gate, report.

        Args:
            stand: The workspace stand (unused; assessor has no repo access).
            score: The dispatch payload.
            toolkit: The execution toolkit (model call, metrics, events).

        Returns:
            WorkflowResult with the assessment report and metrics.
        """
        metrics = toolkit.metrics
        durations = metrics.step_durations_ms

        def timed(name: str, started: float) -> None:
            durations[name] = int((time.monotonic() - started) * 1000)

        # 1. intake: read card and clarifications
        self._step(toolkit, "intake")
        t = time.monotonic()
        intake = build_intake(score)
        timed("intake", t)

        # 2. assess: one schema-guarded model call
        self._step(toolkit, "assess")
        t = time.monotonic()
        model_assessment = await run_assess_step(toolkit, intake.as_text())
        timed("assess", t)

        # 3. gate: apply FR-006 through FR-009 rules
        self._step(toolkit, "gate")
        t = time.monotonic()
        assessment, record = run_gate(model_assessment, intake)
        timed("gate", t)

        # 4. report: package the assessment with metrics and prove write-free
        self._step(toolkit, "report")
        t = time.monotonic()
        timed("report", t)  # before build_report copies the durations into the report
        report = build_report(assessment, record, toolkit)

        log.info(
            "assessor.assessed",
            ready=assessment.ready,
            questions=len(assessment.questions),
            criteria_source=assessment.criteria_source,
            answered_rounds=intake.answered_rounds,
        )

        raw_events = getattr(toolkit, "events", [])
        events = list(raw_events() if callable(raw_events) else raw_events)
        return WorkflowResult(report=report, findings=[], events=events, metrics=metrics)


__all__ = ["AssessorWorkflow", "STEPS"]
