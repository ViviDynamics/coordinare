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

412: two verdicts short-circuit the whole sequence in code, before any model
call. An empty parsed diff is ``nothing_to_scan``; a diff with no statically
scannable source (binary, lockfiles, docs -- decided by an extension allow-list,
not by a model) is ``not_applicable``. Both advance with a note rather than
passing, and neither posts a GitHub review.

Selected by ``workflow: security`` on the role; a sibling of the spec-169
reviewer that imports its parser, survey, anchor rules and poster.
"""
from __future__ import annotations

import shlex
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from performer.models import BackendEvent, BackendEventType
from performer.noise_paths import is_sanitizer_omitted_path
from performer.workflows.base import WorkflowResult
from performer.workflows.reviewer.survey import opened_paths, run_reviewer_survey, survey_output_lines
from performer.workflows.security.budgets import SecurityBudgets
from performer.workflows.security.findings import run_findings_step, run_reanchor_step
from performer.workflows.security.gate import run_gate, to_findings
from performer.workflows.security.intake import build_intake
from performer.workflows.reviewer.diffparse import _UNNAMED_TAIL_PATH
from performer.workflows.security.models import SECURITY_CATEGORIES, ScanResult, SecurityRecord
from performer.workflows.security.personas import render_scan_findings
from performer.workflows.security.post import post_security_review
from performer.workflows.security.report import build_report, write_free_check
from performer.workflows.security.scanner import NothingToScan, ScannerUnavailable, default_runner, is_scannable_source, run_scan

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
    #: 343: ordered steps, the single source for the wire and the latch.
    steps = STATES

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
    ) -> list[Any]:
        """Ask the model what security scanning applies to this repository (#366).

        Replaces a fixed `semgrep + bandit` pair. That pair produced a passing
        verdict on every non-Python repository, because bandit exits 0 having
        parsed nothing and the old code read only its ``results`` array.

        An empty plan is a hold, never a pass: `run_scan` raises
        ``ScannerUnavailable`` on an empty tool list, which routes to the
        existing env_blocked path.
        """
        from performer.workflows.budget import Budget
        from performer.workflows.security.tooling import ScanPlan, plan_persona, tool_inventory

        listing = "\n".join(f"- {p}" for p in intake.changed_paths[:200])
        content = [{
            "type": "text",
            "text": (
                f"Changed files in this pull request:\n{listing}\n\n"
                "Decide what static security scanning applies to this repository "
                "and return the plan JSON."
            ),
        }]
        plan: ScanPlan = await toolkit.call_model(
            persona=plan_persona(tool_inventory()), schema=ScanPlan, content=content,
            budget=Budget.for_step("security_tooling"),
        )
        log.info(
            "security.tools_determined",
            tools=[tool.name for tool in plan.tools],
            nothing_applies=plan.nothing_applies or None,
        )
        return list(plan.tools)

    def _scan_reader(self, toolkit: Any) -> Any:
        """Read one scanner's raw output with the model (#366).

        No per-tool normalizer and no JSON-shape assumption. The reader also
        reports, per file, whether the tool actually examined it -- which is the
        signal the old code threw away and the reason an unscanned repository
        could pass.
        """
        from performer.workflows.budget import Budget
        from performer.workflows.security.tooling import ScanReading, read_persona

        async def read(tool: str, argv: list[str], stdout: str, files: list[str]) -> ScanReading:
            listing = "\n".join(f"- {p}" for p in files[:200])
            content = [{
                "type": "text",
                "text": (
                    f"Tool: {tool}\nCommand: {' '.join(argv)}\n\n"
                    f"Files it was given:\n{listing}\n\n"
                    f"Raw output:\n{stdout[-40000:]}\n\n"
                    "Return the findings and per-file coverage JSON."
                ),
            }]
            return await toolkit.call_model(
                persona=read_persona(), schema=ScanReading, content=content,
                budget=Budget.for_step("security_scan_read"),
            )

        return read

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

        # 412: a fetch outage ("failed"/"unavailable") is NOT an empty diff --
        # hold with a reason instead of advancing on a diff that was never seen.
        if not intake.changed_files and intake.pr_diff_status in ("failed", "unavailable"):
            record = SecurityRecord(
                changed_files=[], diff_truncated=intake.diff_truncated, scan=[],
                verdict="env_blocked", covered_files=[], unread_files=[],
                hold_reason="the pull-request diff could not be fetched (" + intake.pr_diff_status + "), so the security review held instead of advancing on nothing",
            )
            log.info("security.diff_fetch_held", status=intake.pr_diff_status)
            return await self._finish(toolkit, workspace, record, metrics, [])

        # 412: the empty-diff and non-source verdicts are decided in code,
        # before the tooling model call. "Nothing parsed" and "nothing a
        # static scanner can read" are properties of the file list, not
        # judgements for a model, and the old path ran the whole scan-and-gate
        # sequence on them to produce a passing verdict from zero evidence.
        # 412 round 6: the sanitizer bounds the per-path metadata; an overflow
        # means unnamed unread files exist, so the scan cannot see the whole
        # change set.
        if intake.unread_overflow:
            record = SecurityRecord(
                changed_files=intake.changed_files, diff_truncated=intake.diff_truncated, scan=[],
                verdict="env_blocked", covered_files=[], unread_files=[],
                hold_reason=f"the truncated diff could not name {intake.unread_overflow} more unread file(s), so the security review held instead of scanning a partial change set",
            )
            log.info("security.unread_overflow_held", overflow=intake.unread_overflow)
            return await self._finish(toolkit, workspace, record, metrics, [])

        if not intake.changed_files:
            if intake.diff_truncated:
                # 412 round 5: a truncated diff whose cut hides an unknown
                # set of files (no machine names were emitted) is not a
                # genuine empty diff -- hold instead of a vacuous advance.
                record = SecurityRecord(
                    changed_files=[], diff_truncated=True, scan=[],
                    verdict="env_blocked", covered_files=[], unread_files=[],
                    hold_reason="the pull-request diff was truncated before any file header, so the unread set is unknown and the security review held instead of advancing on nothing",
                )
                log.info("security.truncated_no_files_held")
                return await self._finish(toolkit, workspace, record, metrics, [])
            # 412 round 25: a delivered-and-empty sanitized diff is only
            # "nothing to scan" when the PR itself changed nothing. The
            # dispatch carries the GitHub-side changed-path list, which
            # survives sanitization -- a binary- or docs-only change set is
            # not_applicable, while a scannable path the diff never showed
            # holds (its content was never seen).
            # 412 round 39: the dispatch caps the changed-path list. An
            # overflow means paths were cut, so the classification below
            # would judge a partial list -- a scannable path could hide in
            # the tail and read as not_applicable. Hold instead.
            if intake.pr_changed_paths_overflow:
                record = SecurityRecord(
                    changed_files=[], diff_truncated=intake.diff_truncated, scan=[],
                    verdict="env_blocked", covered_files=[], unread_files=[],
                    hold_reason="the dispatch changed-path list overflowed its budget, so a scannable path may hide beyond it and the security review held instead of classifying a partial list",
                )
                log.info("security.path_overflow_held")
                return await self._finish(toolkit, workspace, record, metrics, [])
            if intake.pr_changed_paths:
                # 412 round 31: a path the sanitizer deliberately omitted
                # (``.codex/``, ``.venv/``, ``vendor/bundle/`` -- the diff
                # note says so) explains an empty sanitized diff; it is not
                # content the workflow never saw, so it does not turn the
                # advance-with-note verdict into a hold.
                scannable = [
                    p for p in intake.pr_changed_paths
                    if is_scannable_source(p) and not is_sanitizer_omitted_path(p)
                ]
                if scannable:
                    record = SecurityRecord(
                        changed_files=[], diff_truncated=intake.diff_truncated, scan=[],
                        verdict="env_blocked", covered_files=[], unread_files=scannable,
                        hold_reason="the sanitized diff delivered no content but the change set names scannable source files, so the security review held instead of advancing on nothing",
                    )
                    log.info("security.hold", reason="sanitized_empty_scannable_paths", unread=scannable)
                    return await self._finish(toolkit, workspace, record, metrics, [])
                record = SecurityRecord(
                    changed_files=[], diff_truncated=intake.diff_truncated, scan=[],
                    verdict="not_applicable", covered_files=list(intake.pr_changed_paths), unread_files=[],
                )
                log.info("security.not_applicable", files=intake.pr_changed_paths, reason="changed_paths_all_non_scannable")
                return await self._finish(toolkit, workspace, record, metrics, [])
            record = SecurityRecord(
                changed_files=[], diff_truncated=intake.diff_truncated, scan=[],
                verdict="nothing_to_scan", covered_files=[], unread_files=[],
            )
            log.info("security.nothing_to_scan")
            return await self._finish(toolkit, workspace, record, metrics, [])
        # 412 round 11: deletions are classified before the source check --
        # a deleted source file must not make a mixed diff look scannable and
        # dodge the code-decided not_applicable verdict. 412 round 12: the
        # all-deletion no-target case also short-circuits here, in code,
        # before the tooling model is asked to plan a scan of nothing.
        deleted_paths = {f.path for f in intake.changed_files if getattr(f, "deleted", False)}
        # 412 round 14: the synthetic tail is not on disk -- it stays in
        # changed_files (the coverage gates hold on it) but must never reach
        # a scanner argv, where the nonexistent path can fail the scan before
        # the real files are examined.
        # 412 round 15: named phantoms beyond the truncation cap have no
        # hunks and nothing read -- the same rule; they are not on disk.
        # 412 round 19: the source classifier gates the argv too -- a
        # lockfile that made it into the change set is not a scan target,
        # and handing it to a model-selected scanner only invites parse
        # errors and false findings.
        # 412 round 20: a truncation cut clears ``deleted`` so a cut-through
        # deletion holds as unread -- ``deleted_before_cut`` still keeps it
        # out of the argv, or the scanner is pointed at a missing file.
        scannable_paths = [
            f.path for f in intake.changed_files
            if f.path not in deleted_paths
            and f.path != _UNNAMED_TAIL_PATH
            and (f.hunks or f.fully_in_diff)
            and is_scannable_source(f.path)
            and not f.deleted_before_cut
        ]
        # 412 round 21: a truncated diff that NAMES scannable-classified files
        # it never showed (beyond the cap) holds even when visible sources
        # keep the scanner busy -- the coverage pass can OPEN those named
        # files, but open is not scanned, and a passing verdict on a partial
        # scan would read as a clean bill for the whole change set.
        if intake.diff_truncated and scannable_paths:
            unscanned = [
                f.path for f in intake.changed_files
                if f.path not in deleted_paths
                and f.path != _UNNAMED_TAIL_PATH
                and not f.fully_in_diff
                and not f.hunks
                and not f.deleted_before_cut
                and is_scannable_source(f.path)
            ]
            if unscanned:
                record = SecurityRecord(
                    changed_files=intake.changed_files, diff_truncated=intake.diff_truncated, scan=[],
                    verdict="env_blocked",
                    hold_reason="the truncated diff named source files whose content was never shown; open is not scanned",
                    covered_files=[],
                    unread_files=unscanned,
                )
                log.info("security.hold", reason="truncated_unread_sources", unread=unscanned)
                return await self._finish(toolkit, workspace, record, metrics, [])
        # 412 round 13: the unnamed tail of a truncated diff keeps the gates
        # from certifying the visible subset -- a docs-or-lockfiles diff
        # whose tail is unaccounted must hold on that tail, the way the
        # reviewer workflow already does, not advance as not_applicable.
        has_unnamed_tail = _UNNAMED_TAIL_PATH in set(intake.changed_paths)
        if has_unnamed_tail and not scannable_paths:
            # Visible change set has nothing to scan, but the tail is
            # unaccounted -- hold instead of advancing on the visible subset.
            record = SecurityRecord(
                changed_files=intake.changed_files, diff_truncated=intake.diff_truncated, scan=[],
                verdict="env_blocked",
                hold_reason="the truncated diff's unnamed tail is unaccounted; the visible change set has no scannable source",
                covered_files=[p for p in intake.changed_paths if p != _UNNAMED_TAIL_PATH],
                unread_files=[_UNNAMED_TAIL_PATH],
            )
            log.info("security.hold", reason="unnamed_tail", unread=[_UNNAMED_TAIL_PATH])
            return await self._finish(toolkit, workspace, record, metrics, [])
        if not scannable_paths:
            if not all(getattr(f, "deleted", False) for f in intake.changed_files):
                if intake.diff_truncated:
                    # 412 round 15: no on-disk target, but the change set holds
                    # named phantoms beyond the truncation cap -- coverage cannot
                    # advance on the visible subset, and nothing exists to scan.
                    # 412 round 34: the hold derives per-file coverage exactly
                    # like the gate does -- a file fully present (or opened) in
                    # the visible part, or deleted, is covered; only the
                    # entries the truncation hid are unread.
                    def _hidden(f: Any) -> bool:
                        return not (
                            getattr(f, "fully_in_diff", False)
                            or getattr(f, "opened_by_survey", False)
                            or getattr(f, "deleted", False)
                        )

                    record = SecurityRecord(
                        changed_files=intake.changed_files, diff_truncated=intake.diff_truncated, scan=[],
                        verdict="env_blocked",
                        hold_reason="the truncated diff named the remaining files without content; nothing exists on disk to scan",
                        covered_files=[f.path for f in intake.changed_files if not _hidden(f)],
                        unread_files=[f.path for f in intake.changed_files if _hidden(f)],
                    )
                    log.info("security.hold", reason="named_phantom_only")
                    return await self._finish(toolkit, workspace, record, metrics, [])
                # 412 round 19: a scannable-CLASSIFIED file that was never read
                # (no hunks and not fully in the diff) still blocks the
                # advance -- the classifier, not the argv filter, decides what
                # a scanner would have been pointed at.
                unread_sources = [
                    f.path for f in intake.changed_files
                    if f.path not in deleted_paths
                    and f.path != _UNNAMED_TAIL_PATH
                    and not f.fully_in_diff
                    and is_scannable_source(f.path)
                ]
                if unread_sources:
                    record = SecurityRecord(
                        changed_files=intake.changed_files, diff_truncated=intake.diff_truncated, scan=[],
                        verdict="env_blocked",
                        hold_reason="the change set names source files whose content was never read",
                        covered_files=[],
                        unread_files=unread_sources,
                    )
                    log.info("security.hold", reason="unread_sources", unread=unread_sources)
                    return await self._finish(toolkit, workspace, record, metrics, [])
                # 412 round 18: the diff is complete -- these are ordinary
                # no-hunk entries (mode-change-only, rename-only, empty
                # files), not truncation phantoms. Nothing was cut, so the
                # phantom hold would misfire on a docs-only change; the
                # code-decided verdict advances.
                # 412 round 26: the diff is complete, so every entry is
                # accounted for -- report the coverage instead of losing it
                # in the persisted projection, exactly like the
                # all-deletions branch.
                record = SecurityRecord(
                    changed_files=intake.changed_files, diff_truncated=intake.diff_truncated, scan=[],
                    verdict="not_applicable",
                    covered_files=list(intake.changed_paths),
                    unread_files=[],
                )
                log.info("security.not_applicable", files=intake.changed_paths, reason="no_hunk_entries")
                return await self._finish(toolkit, workspace, record, metrics, [])
            # 412 round 8: deleted files are covered by the diff's own
            # removals -- they are never on disk, so the record must not read
            # them as unread. 412 round 19: the verdict is not_applicable --
            # files WERE parsed here; nothing_to_scan is the zero-parsed
            # verdict, and "no statically scannable source" is this one.
            record = SecurityRecord(
                changed_files=intake.changed_files, diff_truncated=intake.diff_truncated, scan=[],
                verdict="not_applicable",
                covered_files=list(intake.changed_paths), unread_files=[],
            )
            log.info("security.not_applicable", files=intake.changed_paths, reason="all_deletions")
            return await self._finish(toolkit, workspace, record, metrics, [])

        # tooling: the model determines what scanning applies to this repository (spec 366)
        self._step(toolkit, "tooling", f"{len(intake.changed_files)} changed file(s)")
        t_tooling = time.monotonic()
        tools_to_run = await self._determine_tools(toolkit, intake, workspace, budgets)
        metrics.step_durations_ms["tooling"] = int((time.monotonic() - t_tooling) * 1000)

        # scan: run model-determined tools, fail closed (spec 366)
        self._step(toolkit, "scan", f"{len(tools_to_run)} tool(s)")
        t = time.monotonic()
        scan_results: list[ScanResult] = []
        # 412 round 8: a deleted file is absent from the worktree -- a tool
        # aimed at it fails or examines nothing and the stage reads
        # env_blocked. Deleted paths stay in the changed set for coverage and
        # survey; only existing files are scannable targets.
        if deleted_paths:
            log.info("security.deleted_paths_excluded", files=sorted(deleted_paths))
        try:
            if not scannable_paths:
                raise NothingToScan("every changed file is a deletion; there is no on-disk source to scan")
            scanner_raw, scan_results = await run_scan(scannable_paths, workspace, tools=tools_to_run, read=self._scan_reader(toolkit), runner=self._scan_runner or default_runner, budgets=budgets)
        except NothingToScan as exc:
            # 412: defense in depth -- the intake short-circuit should catch
            # this first, but a run_scan asked to scan nothing reports it
            # instead of reading as a clean pass.
            timed("scan", t)
            record = SecurityRecord(
                changed_files=intake.changed_files, diff_truncated=intake.diff_truncated, scan=[],
                verdict="nothing_to_scan", hold_reason=str(exc), covered_files=list(intake.changed_paths), unread_files=[],
            )
            return await self._finish(toolkit, workspace, record, metrics, [])
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
        # 412 round 32: the anchor pool is the STRICT read set -- explicit
        # content reads over every changed path (``opened_paths``), plus the
        # unchanged-path reads discovered below. The loose
        # ``opened_by_survey`` flag drives coverage only; a read-only
        # command that names the path without printing content does not
        # anchor a finding.
        surveyed = sorted(
            opened_paths(outcome.records(), [f.path for f in files]) | set(_opened_unchanged(outcome, files))
        )
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
            scanner_findings=result.scanner_findings[:200], baseline_scanner_findings=result.baseline_findings[:200], blocking=result.blocking, advisory=result.advisory,
            coverage_pass_ran=outcome.coverage_pass_ran, coverage_pass_output=outcome.coverage_pass_output,
            verdict=result.verdict, covered_files=result.covered_files,
            hold_reason=("the review could not cover every changed file: " + ", ".join(result.unread_files)) if result.verdict == "env_blocked" else None,
        )
        if record.verdict != "env_blocked":
            self._step(toolkit, "post", record.verdict)
            t = time.monotonic()
            posted = await post_security_review(score, result.blocking, result.advisory, files, poster=self._poster, baseline=result.baseline_findings[:200])
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


def _strip_rev_prefix(token: str) -> str:
    """Drop a ``REV:`` git-object prefix (``HEAD:Dockerfile`` -> ``Dockerfile``).

    Scheme-like tokens (``https://...``) pass through untouched. The revision
    may itself contain ``/`` (``origin/main:path``); the strict ``git show``
    check already validated the command, so any remaining ``rev:path`` shape
    loses its revision prefix.
    """
    if ":" not in token or token.startswith(("http://", "https://")):
        return token
    rev, _, rest = token.partition(":")
    return rest if rev else token


def _opened_unchanged(outcome, changed_files) -> list[str]:
    """Unchanged file paths a survey command actually read content from (412).

    The strict definition (``opened_paths``: a cat/head/tail/sed read or a
    ``git show REV:path`` object, naming the path as a whole argument) is the
    anchoring contract -- a listing is not a read, which is what the loose
    coverage definition exists for. Root-level names (Dockerfile, Makefile,
    .env.example) need no separator, so the old "/"-in-path guard is gone.
    """
    changed = {f.path for f in changed_files}
    candidates: set[str] = set()
    for r in outcome.records():
        if not (r.allowed and r.exit_code == 0):
            continue
        try:
            lexer = shlex.shlex(r.command, posix=True, punctuation_chars=";&|")
            lexer.whitespace_split = True
            tokens = [t for t in lexer if t]
        except ValueError:
            tokens = [t for t in r.command.replace("'", " ").replace('"', " ").split() if t]
        # 412 round 19: revision-prefix stripping is a ``git show`` rule --
        # ``cat config/v1:prod.py`` is a real path, and stripping its "rev"
        # would orphan the very survey read that anchors an unchanged file.
        # 412 round 20: git accepts flag forms between the command and its
        # subcommand -- ``git --no-pager show REV:path`` is a common read --
        # and ``-c k=v`` consumes a value, so flags (and a value-taking
        # flag's argument) keep the ``show`` window open instead of closing
        # it. Any other non-flag token closes the window as before.
        since_sep = 0
        in_git_show = False
        flag_wants_value = False
        for token in tokens:
            if token in {";", "&", "|", "&&", "||"}:
                since_sep = 0
                in_git_show = False
                flag_wants_value = False
                continue
            if in_git_show:
                pass
            elif since_sep == 0 and token == "git":
                since_sep += 1
                continue
            elif since_sep == 1 and flag_wants_value:
                flag_wants_value = False
                continue
            elif since_sep == 1 and token == "show":
                since_sep += 1
                in_git_show = True
                continue
            elif since_sep == 1 and token.startswith("-"):
                flag_wants_value = token == "-c"
                continue
            since_sep += 1
            candidate = token[2:] if token.startswith("./") else token
            if in_git_show:
                # ``git show REV:path`` reads the object at *path* -- the
                # revision prefix must not ride along, or the anchor check
                # never matches the bare path a finding is pinned to.
                candidate = _strip_rev_prefix(candidate)
            if candidate and candidate not in changed:
                candidates.add(candidate)
    return sorted(opened_paths(outcome.records(), sorted(candidates)))


__all__ = ["SecurityWorkflow", "STATES"]
