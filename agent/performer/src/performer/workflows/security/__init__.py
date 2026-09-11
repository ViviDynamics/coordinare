"""Security role workflow (spec 170, 366): intake, tooling, scan, survey, findings, gate, post, report.

The model determines what security tooling applies to this repository (spec 366,
reversing spec 170's fail-closed scanner-before-model design). The scan runs
inside the performer (a missing or broken scanner, or no applicable tools, holds
the card in env_blocked). The stage surveys the code around the change read-only,
makes one schema-guarded taint-analysis call over a fixed category set, keeps only
findings whose anchors verify against the diff or the surveyed code, assigns
severity and routing by code from the category, merges the scanner findings as a
floor that cannot be dropped, derives the verdict, posts exactly one GitHub
review and commits nothing. Coordinare routes the blocking findings as today and
lifts the implementer's share into the spec-169 carrier so the spec-167 repair
lane fixes them.

Selected by ``workflow: security`` on the role; a sibling of the spec-169
reviewer that imports its parser, survey, anchor rules and poster.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from performer.models import BackendEvent, BackendEventType
from performer.workflows.base import WorkflowResult
from performer.workflows.reviewer.survey import run_reviewer_survey, survey_output_lines
from performer.workflows.security.budgets import SecurityBudgets
from performer.workflows.security.findings import run_findings_step, run_reanchor_step
from performer.workflows.security.gate import run_gate, to_findings
from performer.workflows.security.intake import build_intake
from performer.workflows.security.models import SECURITY_CATEGORIES, ScanResult, SecurityRecord
from performer.workflows.security.personas import render_scan_findings
from performer.workflows.security.post import post_security_review
from performer.workflows.security.report import build_report, write_free_check
from performer.workflows.security.scanner import ScannerUnavailable, default_runner, run_scan

if TYPE_CHECKING:
    from performer.models import Score, Stand

log = structlog.get_logger(__name__)

STATES: tuple[str, ...] = ("intake", "tooling", "scan", "survey", "coverage", "findings", "gate", "post", "report")
_REANCHOR_DIFF_CHARS = 30000


def _record_dict(r) -> dict[str, Any]:
    return {"command": r.command, "exit_code": r.exit_code, "truncated": r.truncated, "output_chars": len(r.output or "")}


class SecurityWorkflow:
    """Sequences the security steps; only code advances the sequence."""

    name = "security"

    def __init__(self, categories: tuple[str, ...] = SECURITY_CATEGORIES, poster=None, scan_runner=None) -> None:
        self.categories = tuple(categories)
        self._poster = poster
        self._scan_runner = scan_runner

    @staticmethod
    def _step(toolkit, name: str, detail: str = "") -> None:
        emit = getattr(toolkit, "emit", None)
        if callable(emit):
            emit(BackendEvent(type=BackendEventType.progress, text=f"security.{name}", detail=detail))

    async def _determine_tools(
        self, toolkit: Any, intake: Any, workspace: Path, budgets: SecurityBudgets
    ) -> list[tuple[str, Any, Any]]:
        """Ask the model what security tooling applies to this repository.

        Spec 366: Reverses spec 170's decision (scanner before model call).
        The model determines tool applicability and names the tools to run.
        Returns a list of (tool_name, build_command_fn, normalize_result_fn) tuples.
        """
        from performer.workflows.security.scanner import normalize_semgrep, normalize_bandit, build_semgrep_command, build_bandit_command

        # For now, default to semgrep + bandit (this will be replaced by a model call
        # in the final implementation). The model reads the repository structure and
        # determines what tooling is appropriate.
        tools = [
            ("semgrep", lambda files, budgets: build_semgrep_command(files, budgets.semgrep_config), normalize_semgrep),
            ("bandit", lambda files, budgets: build_bandit_command(files), normalize_bandit),
        ]
        log.info("security.tools_determined", tools=[t[0] for t in tools])
        return tools

    async def run(self, stand: "Stand", score: "Score", toolkit: Any) -> WorkflowResult:
        metrics = toolkit.metrics
        durations = metrics.step_durations_ms
        workspace = Path(getattr(stand, "path", ".") or ".")
        budgets = SecurityBudgets.from_env(getattr(score, "workflow_env", None) or {})

        def timed(name: str, started: float) -> None:
            durations[name] = int((time.monotonic() - started) * 1000)

        self._step(toolkit, "intake")
        t = time.monotonic()
        intake = build_intake(score)
        timed("intake", t)
        log.info("security.intake", files=len(intake.changed_files), truncated=intake.diff_truncated, brief=intake.brief_present if hasattr(intake, "brief_present") else bool(intake.brief))

        # tooling: the model determines what scanning applies to this repository (spec 366)
        self._step(toolkit, "tooling", f"{len(intake.changed_files)} changed file(s)")
        t_tooling = time.monotonic()
        tools_to_run = await self._determine_tools(toolkit, intake, workspace, budgets)
        metrics.step_durations_ms["tooling"] = int((time.monotonic() - t_tooling) * 1000)

        # scan: run model-determined tools, fail closed (spec 366)
        self._step(toolkit, "scan", f"{len(tools_to_run)} tool(s)")
        t = time.monotonic()
        scan_results: list[ScanResult] = []
        try:
            scanner_raw, scan_results = await run_scan(intake.changed_paths, workspace, tools=tools_to_run, runner=self._scan_runner or default_runner, budgets=budgets)
        except ScannerUnavailable as exc:
            timed("scan", t)
            reason = f"{exc.tool}: {exc.reason}"
            log.warning("security.scanner_unavailable", tool=exc.tool, reason=exc.reason)
            record = SecurityRecord(
                changed_files=intake.changed_files, diff_truncated=intake.diff_truncated, scan=list(getattr(exc, "results", []) or []),
                verdict="env_blocked", hold_reason=reason, covered_files=[], unread_files=intake.changed_paths,
            )
            return await self._finish(toolkit, workspace, record, metrics, [])
        metrics.commands_run += len(scan_results)
        timed("scan", t)
        log.info("security.scan", tools=[(r.tool, r.exit_code, r.finding_count, r.duration_ms) for r in scan_results], findings=len(scanner_raw))

        self._step(toolkit, "survey")
        t = time.monotonic()
        intake_text = intake.as_text(render_scan_findings(scanner_raw))
        outcome, files = await run_reviewer_survey(toolkit, _IntakeView(intake, intake_text), workspace, budgets.survey_budget())
        timed("survey", t)
        if outcome.coverage_pass_ran:
            self._step(toolkit, "coverage")
            durations["coverage"] = 0

        self._step(toolkit, "findings")
        t = time.monotonic()
        survey_text = outcome.as_text()
        model_out = await run_findings_step(toolkit, intake, scanner_raw, survey_text, self.categories, budgets.max_findings)
        timed("findings", t)
        before = to_findings(model_out, self.categories)

        self._step(toolkit, "gate", f"{len(before)} model finding(s), {len(scanner_raw)} scanner finding(s)")
        t = time.monotonic()
        diff_lines = intake.diff_line_list()
        survey_lines = survey_output_lines(outcome.records())
        surveyed = [f.path for f in files if f.opened_by_survey] + _opened_unchanged(outcome, files)
        gate_kwargs = dict(changed_files=files, diff_lines=diff_lines, survey_lines=survey_lines, surveyed_files=surveyed,
                           truncated=intake.diff_truncated, coverage_pass_ran=outcome.coverage_pass_ran)
        result = run_gate(before, scanner_raw, **gate_kwargs)
        reanchored = []
        if result.dropped:
            log.info("security.reanchor", dropped=len(result.dropped))
            context = f"## Diff\n{intake.diff_text[:_REANCHOR_DIFF_CHARS]}\n\n## Survey notes\n{survey_text[-10000:]}"
            second = await run_reanchor_step(toolkit, result.dropped, files, self.categories, budgets.max_findings, context)
            reanchored = to_findings(second, self.categories)
            result = run_gate(before, scanner_raw, reanchored=reanchored, **gate_kwargs)
        timed("gate", t)
        log.info("security.gate", blocking=len(result.blocking), advisory=len(result.advisory), dropped=len(result.dropped), reanchored=len(reanchored), verdict=result.verdict, unread=result.unread_files)

        record = SecurityRecord(
            changed_files=files, diff_truncated=intake.diff_truncated, scan=scan_results, unread_files=result.unread_files,
            survey_commands=[_record_dict(r) for r in outcome.records() if r.allowed],
            survey_refusals=[{"command": r.command, "reason": r.refusal_reason} for r in outcome.records() if not r.allowed],
            findings_before_gate=before[:30], findings_dropped=result.dropped[:30], findings_after_anchor_recheck=reanchored[:30],
            scanner_findings=result.scanner_findings[:200], blocking=result.blocking, advisory=result.advisory,
            coverage_pass_ran=outcome.coverage_pass_ran, coverage_pass_output=outcome.coverage_pass_output,
            verdict=result.verdict, covered_files=result.covered_files,
            hold_reason=("the review could not cover every changed file: " + ", ".join(result.unread_files)) if result.verdict == "env_blocked" else None,
        )
        if record.verdict != "env_blocked":
            self._step(toolkit, "post", record.verdict)
            t = time.monotonic()
            posted = await post_security_review(score, result.blocking, result.advisory, files, poster=self._poster)
            timed("post", t)
            if posted.ok:
                record.posted_review_url = posted.url
            else:
                record.post_error = posted.error
                record.hold_reason = posted.error
                record.verdict = "env_blocked"
        else:
            log.info("security.hold", reason="coverage", unread=result.unread_files)
        return await self._finish(toolkit, workspace, record, metrics, [f.model_dump() for f in result.blocking])

    async def _finish(self, toolkit, workspace: Path, record: SecurityRecord, metrics, findings: list[dict]) -> WorkflowResult:
        self._step(toolkit, "report")
        t = time.monotonic()
        check = await write_free_check(toolkit, workspace)
        metrics.step_durations_ms["report"] = int((time.monotonic() - t) * 1000)
        record.workflow_metrics = {"model_calls": metrics.model_calls, "commands_run": metrics.commands_run}
        report = build_report(record, check, metrics)
        raw_events = getattr(toolkit, "events", [])
        events = list(raw_events() if callable(raw_events) else raw_events)
        return WorkflowResult(report=report, findings=findings, events=events, metrics=metrics)


class _IntakeView:
    """What the reviewer's survey reads from an intake: changed files, truncation and the text."""

    def __init__(self, intake, text: str) -> None:
        self.changed_files = intake.changed_files
        self.diff_truncated = intake.diff_truncated
        self._text = text

    def as_text(self) -> str:
        return self._text


def _opened_unchanged(outcome, changed_files) -> list[str]:
    """Paths of files the survey opened that are not changed files (a sink may live there)."""
    from performer.workflows.reviewer.survey import command_names_path  # noqa: PLC0415

    changed = {f.path for f in changed_files}
    opened: list[str] = []
    for r in outcome.records():
        if not (r.allowed and r.exit_code == 0):
            continue
        for token in r.command.replace("'", " ").replace('"', " ").split():
            candidate = token[2:] if token.startswith("./") else token
            if "/" in candidate and "." in candidate.rsplit("/", 1)[-1] and candidate not in changed and command_names_path(r.command, candidate):
                opened.append(candidate)
    return sorted(set(opened))


__all__ = ["SecurityWorkflow", "STATES"]
