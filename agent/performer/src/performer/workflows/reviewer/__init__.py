"""Reviewer role workflow (spec 169): intake, survey, findings, gate, post, report.

The reviewer reads the injected PR diff and the prior comments, surveys the
surrounding code read-only under a budget, makes one schema-guarded findings
call, keeps only findings whose anchors verify against the diff or the survey
output, adds the rule findings, derives the verdict by code, posts exactly one
GitHub review and commits nothing. Coordinare lifts the findings into the card
and injects them into the implementer dispatch only, where the spec-167 repair
lane addresses them one file group at a time.

Selected by ``workflow: reviewer`` on the role and run behind the same
``WorkflowAdapter`` seam as QA (spec 164). The finding categories are a
parameter so the security role can reuse the workflow (FR-018).
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from performer.models import BackendEvent, BackendEventType
from performer.workflows.base import WorkflowResult
from performer.workflows.reviewer.budgets import ReviewerBudgets
from performer.workflows.reviewer.findings import run_findings_step, run_reanchor_step
from performer.workflows.reviewer.gate import run_gate, to_findings
from performer.workflows.reviewer.intake import build_intake
from performer.workflows.reviewer.models import ADVISORY_CATEGORIES, DEFAULT_CATEGORIES, ReviewRecord
from performer.workflows.reviewer.post import post_review
from performer.workflows.reviewer.report import build_report, write_free_check
from performer.workflows.reviewer.survey import opened_paths, run_reviewer_survey, survey_output_lines

if TYPE_CHECKING:
    from performer.models import Score, Stand

log = structlog.get_logger(__name__)

STATES: tuple[str, ...] = ("intake", "survey", "coverage", "findings", "gate", "post", "report")


def _record_dict(r) -> dict[str, Any]:
    return {"command": r.command, "exit_code": r.exit_code, "truncated": r.truncated, "output_chars": len(r.output or "")}


class ReviewerWorkflow:
    """Sequences the reviewer steps; only code advances the sequence."""

    name = "reviewer"
    #: 343: ordered steps, the single source for the wire and the latch.
    steps = STATES

    def __init__(self, categories: tuple[str, ...] = DEFAULT_CATEGORIES, poster=None) -> None:
        self.categories = tuple(categories)
        self._poster = poster

    @staticmethod
    def _step(toolkit, name: str, detail: str = "") -> None:
        emit = getattr(toolkit, "emit", None)
        if callable(emit):
            emit(BackendEvent(type=BackendEventType.progress, text=f"reviewer.{name}", detail=detail))

    async def run(self, stand: "Stand", score: "Score", toolkit: Any) -> WorkflowResult:
        metrics = toolkit.metrics
        durations = metrics.step_durations_ms
        workspace = Path(getattr(stand, "path", ".") or ".")
        budgets = ReviewerBudgets.from_env(getattr(score, "workflow_env", None) or {})

        def timed(name: str, started: float) -> None:
            durations[name] = int((time.monotonic() - started) * 1000)

        self._step(toolkit, "intake")
        t = time.monotonic()
        intake = build_intake(score)
        timed("intake", t)
        log.info("reviewer.intake", files=len(intake.changed_files), truncated=intake.diff_truncated, prior_comments=len(intake.prior_comments), brief=intake.brief_present)

        # 412: a fetch outage ("failed"/"unavailable") is NOT an empty diff --
        # hold with a reason instead of advancing on a diff that was never seen.
        if not intake.changed_files and intake.pr_diff_status in ("failed", "unavailable"):
            record = ReviewRecord(
                changed_files=[], diff_truncated=intake.diff_truncated, unread_files=[],
                verdict="env_blocked", covered_files=[],
                post_error="the pull-request diff could not be fetched (" + intake.pr_diff_status + "), so the review held instead of advancing on nothing",
            )
            log.info("reviewer.diff_fetch_held", status=intake.pr_diff_status)
            self._step(toolkit, "report")
            check = await write_free_check(toolkit, workspace)
            record.workflow_metrics = {"model_calls": metrics.model_calls, "commands_run": metrics.commands_run}
            report = build_report(record, check, metrics)
            raw_events = getattr(toolkit, "events", [])
            events = list(raw_events() if callable(raw_events) else raw_events)
            return WorkflowResult(report=report, findings=[], events=events, metrics=metrics)

        # 412 round 6: the sanitizer bounds the per-path metadata; an overflow
        # means unnamed unread files exist, so coverage cannot be verified.
        if intake.unread_overflow:
            record = ReviewRecord(
                changed_files=intake.changed_files, diff_truncated=intake.diff_truncated, unread_files=[],
                verdict="env_blocked", covered_files=[],
                post_error=f"the truncated diff could not name {intake.unread_overflow} more unread file(s), so coverage cannot be verified and the review held",
            )
            log.info("reviewer.unread_overflow_held", overflow=intake.unread_overflow)
            self._step(toolkit, "report")
            check = await write_free_check(toolkit, workspace)
            record.workflow_metrics = {"model_calls": metrics.model_calls, "commands_run": metrics.commands_run}
            report = build_report(record, check, metrics)
            raw_events = getattr(toolkit, "events", [])
            events = list(raw_events() if callable(raw_events) else raw_events)
            return WorkflowResult(report=report, findings=[], events=events, metrics=metrics)

        # 412: an empty parsed diff is nothing_to_review, decided in code and
        # before any model call. The old path ran the whole sequence and
        # returned "approved" -- coverage over zero files is trivially
        # complete -- a vacuous pass on a diff that was never seen.
        if not intake.changed_files:
            if intake.diff_truncated:
                # 412 round 5: a truncated diff whose cut hides an unknown
                # set of files (no machine names were emitted) is not a
                # genuine empty diff -- hold instead of a vacuous advance.
                record = ReviewRecord(
                    changed_files=[], diff_truncated=True, unread_files=[],
                    verdict="env_blocked", covered_files=[],
                    post_error="the pull-request diff was truncated before any file header, so the unread set is unknown and the review held instead of advancing on nothing",
                )
                log.info("reviewer.truncated_no_files_held")
                self._step(toolkit, "report")
                check = await write_free_check(toolkit, workspace)
                record.workflow_metrics = {"model_calls": metrics.model_calls, "commands_run": metrics.commands_run}
                report = build_report(record, check, metrics)
                raw_events = getattr(toolkit, "events", [])
                events = list(raw_events() if callable(raw_events) else raw_events)
                return WorkflowResult(report=report, findings=[], events=events, metrics=metrics)
            record = ReviewRecord(
                changed_files=[], diff_truncated=intake.diff_truncated, unread_files=[],
                verdict="nothing_to_review", covered_files=[],
            )
            log.info("reviewer.nothing_to_review")
            self._step(toolkit, "report")
            t = time.monotonic()
            check = await write_free_check(toolkit, workspace)
            durations["report"] = int((time.monotonic() - t) * 1000)
            record.workflow_metrics = {"model_calls": metrics.model_calls, "commands_run": metrics.commands_run}
            report = build_report(record, check, metrics)
            raw_events = getattr(toolkit, "events", [])
            events = list(raw_events() if callable(raw_events) else raw_events)
            return WorkflowResult(report=report, findings=[], events=events, metrics=metrics)

        self._step(toolkit, "survey", f"{len(intake.changed_files)} changed file(s)")
        t = time.monotonic()
        outcome, files = await run_reviewer_survey(toolkit, intake, workspace, budgets.survey_budget())
        timed("survey", t)
        if outcome.coverage_pass_ran:
            self._step(toolkit, "coverage")
            durations["coverage"] = 0

        self._step(toolkit, "findings")
        t = time.monotonic()
        survey_text = outcome.as_text()
        model_out = await run_findings_step(toolkit, intake, survey_text, self.categories, budgets.max_findings)
        timed("findings", t)
        before = to_findings(model_out, self.categories)

        self._step(toolkit, "gate", f"{len(before)} finding(s) from the model")
        t = time.monotonic()
        diff_lines = intake.diff_line_list()
        survey_lines = survey_output_lines(outcome.records())
        gate_kwargs = dict(changed_files=files, prior_comments=intake.prior_comments, diff_lines=diff_lines, survey_lines=survey_lines,
                           brief=intake.brief, truncated=intake.diff_truncated, coverage_pass_ran=outcome.coverage_pass_ran,
                           surveyed_files=opened_paths(outcome.records(), [f.path for f in before]))
        result = run_gate(before, model_out.dispositions, **gate_kwargs)
        reanchored = []
        if result.dropped:
            log.info("reviewer.reanchor", dropped=len(result.dropped))
            context = f"## Diff\n{intake.diff_text[:_REANCHOR_DIFF_CHARS]}\n\n## Survey notes\n{survey_text[-10000:]}"
            second = await run_reanchor_step(toolkit, result.dropped, files, self.categories, budgets.max_findings, context)
            reanchored = to_findings(second, self.categories)
            gate_kwargs["surveyed_files"] |= opened_paths(outcome.records(), [f.path for f in reanchored])
            result = run_gate(before, model_out.dispositions, reanchored=reanchored, **gate_kwargs)
        timed("gate", t)
        log.info("reviewer.gate", kept=len(result.findings), dropped=len(result.dropped), reanchored=len(reanchored), verdict=result.verdict, unread=result.unread_files)

        advisory = [f for f in result.findings if f.category in ADVISORY_CATEGORIES]
        record = ReviewRecord(
            changed_files=files, diff_truncated=intake.diff_truncated, unread_files=result.unread_files,
            survey_commands=[_record_dict(r) for r in outcome.records() if r.allowed],
            survey_refusals=[{"command": r.command, "reason": r.refusal_reason} for r in outcome.records() if not r.allowed],
            findings_before_gate=before[:30], findings_dropped=result.dropped[:30], findings_after_anchor_recheck=reanchored[:30],
            dispositions=result.dispositions, coverage_pass_ran=outcome.coverage_pass_ran, coverage_pass_output=outcome.coverage_pass_output,
            verdict=result.verdict, covered_files=result.covered_files, findings=result.blocking,
            advisory_findings=advisory,
        )

        if record.verdict != "env_blocked":
            self._step(toolkit, "post", record.verdict)
            t = time.monotonic()
            posted = await post_review(score, result.blocking, advisory, files, result.fixed_ids, len(result.covered_files), poster=self._poster)
            timed("post", t)
            if posted.ok:
                record.posted_review_url = posted.url
            else:
                record.post_error = posted.error
                record.verdict = "env_blocked"
        else:
            log.info("reviewer.hold", reason="coverage", unread=result.unread_files)

        self._step(toolkit, "report")
        t = time.monotonic()
        check = await write_free_check(toolkit, workspace)
        timed("report", t)
        record.workflow_metrics = {"model_calls": metrics.model_calls, "commands_run": metrics.commands_run}
        report = build_report(record, check, metrics)
        raw_events = getattr(toolkit, "events", [])
        events = list(raw_events() if callable(raw_events) else raw_events)
        return WorkflowResult(report=report, findings=[f.model_dump() for f in result.blocking], events=events, metrics=metrics)


_REANCHOR_DIFF_CHARS = 30000

__all__ = ["ReviewerWorkflow", "STATES"]
