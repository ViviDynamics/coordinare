"""Architect role workflow (spec 165): intake, survey, blueprint, size, report.

The model plans and witnesses; code decides and verifies. The architect reads
a bounded slice of the repository through a read-only allow-list, produces one
schema-validated blueprint, and commits nothing. Coordinare slices the
blueprint into the implementer's, documenter's and QA's briefs (spec 165 R2).

Selected by ``workflow: architect`` on the role and run behind the same
``WorkflowAdapter`` seam as QA (spec 164).
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from performer.models import BackendEvent, BackendEventType
from performer.workflows.architect.blueprint import run_blueprint_step
from performer.workflows.architect.intake import build_intake
from performer.workflows.architect.report import build_report, write_free_check
from performer.workflows.architect.size import size_of
from performer.workflows.architect.survey import SurveyBudget, run_survey_step
from performer.workflows.base import WorkflowResult

if TYPE_CHECKING:
    from performer.models import Score, Stand

log = structlog.get_logger(__name__)

STEPS: tuple[str, ...] = ("intake", "survey", "blueprint", "size", "report")


class ArchitectWorkflow:
    """Sequences the five architect steps; only code advances the sequence."""

    name = "architect"
    #: 343: ordered steps, the single source for the wire and the latch.
    steps = STEPS

    @staticmethod
    def _step(toolkit, name: str, detail: str = "") -> None:
        emit = getattr(toolkit, "emit", None)
        if callable(emit):
            emit(BackendEvent(type=BackendEventType.progress, text=f"architect.{name}", detail=detail))

    async def run(self, stand: "Stand", score: "Score", toolkit: Any) -> WorkflowResult:
        metrics = toolkit.metrics
        workspace = Path(getattr(stand, "path", ".") or ".")
        durations = metrics.step_durations_ms

        def timed(name: str, started: float) -> None:
            durations[name] = int((time.monotonic() - started) * 1000)

        # 1. intake: no model call
        self._step(toolkit, "intake")
        t = time.monotonic()
        intake = build_intake(score, workspace)
        timed("intake", t)

        # 2. survey: model proposes, allow-list decides, code runs
        self._step(toolkit, "survey")
        t = time.monotonic()
        budget = SurveyBudget.from_env(getattr(score, "workflow_env", None) or {})
        survey = await run_survey_step(toolkit, intake.as_text(), workspace, budget)
        timed("survey", t)
        log.info("architect.survey_done", commands=len(survey.records), refused=survey.refused)

        # 3. blueprint: one schema-guarded call; a hollow plan raises here
        self._step(toolkit, "blueprint")
        t = time.monotonic()
        blueprint = await run_blueprint_step(toolkit, intake.as_text(), survey.as_text())
        timed("blueprint", t)

        # 4. size: pure function, never the model
        self._step(toolkit, "size")
        t = time.monotonic()
        size = size_of(blueprint)
        timed("size", t)

        # 5. report: prove the tree is clean, then package
        self._step(toolkit, "report")
        t = time.monotonic()
        check = await write_free_check(toolkit, workspace)
        timed("report", t)  # before build_report copies the durations into the report
        report = build_report(blueprint, size, survey, check, metrics)
        log.info(
            "architect.blueprint",
            size=size,
            milestones=len(blueprint.milestones),
            criteria=len(blueprint.criteria),
            docs=len(blueprint.docs),
            refused_commands=survey.refused,
        )
        raw_events = getattr(toolkit, "events", [])
        events = list(raw_events() if callable(raw_events) else raw_events)
        return WorkflowResult(report=report, findings=[], events=events, metrics=metrics)


__all__ = ["ArchitectWorkflow", "STEPS"]
