"""Role-path builders for ``main.handle_status`` (spec 438).

The role branches of ``handle_status`` are extracted here verbatim, one
builder per role, each under the 100-line function budget.
``handle_status`` stays in ``performer.main`` as the assembly.

Collaborators (git, GitHub, shared helpers) are looked up on
``performer.main`` at call time rather than imported by name. Two
reasons: it avoids an import cycle (main imports this module), and the
test suite patches those collaborators on ``performer.main`` -- ~85
sites -- which a module-level import here would silently bypass,
letting tests reach real git and GitHub. Same pattern as
``performer.qa_postprocess`` (spec 164 T049).
"""
from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from performer.assessor_questions import (
    assessment_fields_outside_object,
    assessment_fragment_questions,
    assessment_has_duplicate_contract_fields,
    assessment_has_invalid_field_types,
)
from performer.degeneracy import DegenerateArtifactError, classify_file, classify_text
from performer.models import _redact_secrets
from performer.protocol import PerformerResponse

if TYPE_CHECKING:
    from pathlib import Path

    from performer.config import Settings
    from performer.models import Performance

log = structlog.get_logger(__name__)


def _plan_artifact_refusal(
    perf: Performance, plan_content: str, tasks_content: str
) -> PerformerResponse | None:
    """Refuse an empty or degenerate architecture plan/tasks (#396).

    A degenerate artifact (repeated-token narration) must never be committed:
    the turn fails and reports why, before anything is written.
    """
    if not plan_content:
        perf.state = "error"
        perf.error_reason = "Backend produced an empty architecture plan"
        return PerformerResponse(
            status="error",
            session_id=perf.session_id,
            reason="Backend produced an empty architecture plan",
        )
    for artifact_name, artifact_content in (("plan", plan_content), ("tasks", tasks_content)):
        if not artifact_content:
            continue
        verdict = classify_text(artifact_content)
        if verdict.degenerate:
            log.warning(
                "architect.plan_degenerate",
                artifact=artifact_name,
                reasons=list(verdict.reasons),
                session_id=perf.session_id,
            )
            perf.state = "error"
            perf.error_reason = (
                f"degenerate artifact refused ({artifact_name}): "
                + "; ".join(verdict.reasons)
            )
            return PerformerResponse(
                status="error",
                session_id=perf.session_id,
                reason=perf.error_reason,
            )
    return None


def _workspace_file_refusal(
    perf: Performance, artifact_name: str, path: "Path",
) -> PerformerResponse | None:
    """Classify a workspace file BEFORE reading it into memory (#396).

    ``classify_file`` bounds its reads, so a multi-gigabyte plan.md is
    refused without ever being materialized; only a clean file is read whole.
    """
    verdict = classify_file(path)
    if not verdict.degenerate:
        return None
    log.warning(
        "architect.plan_degenerate",
        artifact=artifact_name,
        reasons=list(verdict.reasons),
        session_id=perf.session_id,
    )
    perf.state = "error"
    perf.error_reason = (
        f"degenerate artifact refused ({artifact_name}): "
        + "; ".join(verdict.reasons)
    )
    return PerformerResponse(
        status="error",
        session_id=perf.session_id,
        reason=perf.error_reason,
    )


def _read_workspace_plan(
    perf: Performance, folder: str,
) -> tuple[str, str, PerformerResponse | None]:
    """Read plan/tasks from the workspace, classifying BEFORE reading (#396).

    ``classify_file`` bounds its reads, so a multi-gigabyte plan.md is
    refused without ever being materialized; only a clean file is read whole.
    """
    ws_plan = perf.stand.path / folder / "plan.md"
    ws_tasks = perf.stand.path / folder / "tasks.md"
    if not ws_plan.is_file():
        return "", "", None
    file_refusal = _workspace_file_refusal(perf, "plan", ws_plan)
    if file_refusal:
        return "", "", file_refusal
    try:
        plan_content = ws_plan.read_text(encoding="utf-8").strip()
    except OSError as exc:
        log.warning("architect.read_workspace_plan_failed", path=str(ws_plan), error=str(exc))
        plan_content = ""
    if not plan_content or not ws_tasks.is_file():
        return plan_content, "", None
    tasks_refusal = _workspace_file_refusal(perf, "tasks", ws_tasks)
    if tasks_refusal:
        return "", "", tasks_refusal
    try:
        tasks_content = ws_tasks.read_text(encoding="utf-8").strip()
    except OSError as exc:
        log.warning("architect.read_workspace_tasks_failed", path=str(ws_tasks), error=str(exc))
        tasks_content = ""
    return plan_content, tasks_content, None


async def architect_path(
    perf: Performance,
    backend_status,
    settings: Settings | None,
) -> PerformerResponse:
    """Architect role path (spec 020/165): commit plan file instead of opening a PR."""

    from performer import main as _m

    _extract_json = _m._extract_json
    _doc_folder = _m._doc_folder
    commit_file = _m.commit_file

    # 165: the architect WORKFLOW reports a blueprint and commits nothing.
    # Its output is the JSON report (blueprint, size, write_free_check,
    # workflow_metrics); coordinare lifts the blueprint and slices it into
    # the readers' briefs. The prose path below is untouched (164 FR-005).
    _bp_report = _extract_json(backend_status.output or "")
    if isinstance(_bp_report, dict) and isinstance(_bp_report.get("blueprint"), dict):
        perf.state = "plan_committed"
        log.info(
            "architect.blueprint_reported",
            size=_bp_report.get("size"),
            milestones=len(_bp_report["blueprint"].get("milestones") or []),
            session_id=perf.session_id,
        )
        return PerformerResponse(
            status="plan_committed",
            session_id=perf.session_id,
            report=_bp_report,
            progress=f"blueprint ({_bp_report.get('size') or 'unsized'})",
        )
    folder = _doc_folder(perf.score)
    issue_num = perf.score.issue_number
    plan_content = (backend_status.output or "").strip()
    tasks_content = ""
    plan_source = "inline"

    if plan_content:
        if "---TASKS---" in plan_content:
            parts = plan_content.split("---TASKS---", 1)
            plan_content = parts[0].strip()
            tasks_content = parts[1].strip()
    else:
        plan_content, tasks_content, ws_refusal = _read_workspace_plan(perf, folder)
        if ws_refusal:
            return ws_refusal
        if plan_content:
            plan_source = "workspace"

    refusal = _plan_artifact_refusal(perf, plan_content, tasks_content)
    if refusal:
        return refusal

    log.info("architect.plan_source", source=plan_source, session_id=perf.session_id)

    plan_path = f"{folder}/plan.md"
    commit_msg = f"chore: add architecture plan for #{issue_num}" if issue_num else "chore: add architecture plan"
    try:
        await commit_file(perf.stand, plan_path, plan_content, commit_msg)
    except DegenerateArtifactError as exc:
        # commit_file also scans the index: a degenerate blob staged before
        # the turn reaches this refusal even though the inline content is
        # clean. Translate it into a turn failure, not a crash (#396).
        log.warning(
            "architect.plan_degenerate",
            artifact="plan",
            reasons=list(exc.reasons),
            session_id=perf.session_id,
        )
        perf.state = "error"
        perf.error_reason = str(exc)
        return PerformerResponse(
            status="error",
            session_id=perf.session_id,
            reason=perf.error_reason,
        )

    if tasks_content:
        tasks_path = f"{folder}/tasks.md"
        tasks_msg = f"chore: add implementation tasks for #{issue_num}" if issue_num else "chore: add implementation tasks"
        try:
            await commit_file(perf.stand, tasks_path, tasks_content, tasks_msg)
        except Exception as exc:
            log.warning("architect.commit_tasks_failed", error=str(exc))
    perf.plan_path = plan_path
    perf.state = "plan_committed"
    return PerformerResponse(
        status="plan_committed",
        session_id=perf.session_id,
        plan_path=plan_path,
    )


async def assessor_path(
    perf: Performance,
    backend_status,
    settings: Settings | None,
) -> PerformerResponse:
    """Assessor role path (spec 166/410): assessment report, blocked questions, or decline verdict."""
    from performer import main as _m

    _extract_json = _m._extract_json
    _doc_folder = _m._doc_folder
    commit_file = _m.commit_file

    # 166: the assessor WORKFLOW reports an assessment and commits nothing.
    # Its output is the JSON report (assessment, gate_record, write_free_check,
    # workflow_metrics); coordinare lifts the assessment and records it on the
    # card session. The prose path below is untouched (164 FR-005).
    assess_raw = backend_status.output or ""
    # Format repair cannot answer a human decision recovered from an earlier
    # response. Surface it before interpreting any replacement readiness verdict.
    if perf.assessment_questions:
        perf.open_questions = perf.assessment_questions
        perf.state = "blocked"
        return PerformerResponse(
            status="blocked", session_id=perf.session_id,
            questions=perf.assessment_questions,
        )
    _lenient = await _assessor_lenient_response(perf, assess_raw, settings)
    if _lenient is not None:
        return _lenient

    _workflow = await _assessor_workflow_response(perf, assess_raw)
    if _workflow is not None:
        return _workflow

    assess_output = _extract_json(assess_raw) if isinstance(assess_raw, str) else assess_raw
    sufficient = assess_output.get("sufficient", True)
    raw_questions = assess_output.get("questions", [])
    questions = raw_questions if isinstance(raw_questions, list) else []

    # If insufficient but no questions were generated, treat as sufficient
    # (mirrors the legacy assess_card behaviour).
    if not sufficient and not questions:
        log.info(
            "assessor.no_questions_treating_as_sufficient",
            session_id=perf.session_id,
        )
        sufficient = True

    if sufficient:
        # Write assessment report for the architect and human readers
        folder = _doc_folder(perf.score)
        assessment_path = f"{folder}/assessment.md"
        assess_issue_num = perf.score.issue_number
        # Build structured assessment content from the raw output
        assessment_content = f"# Assessment: {perf.score.title}\n\n"
        assessment_content += assess_raw if isinstance(assess_raw, str) else str(assess_output)
        try:
            await commit_file(
                perf.stand, assessment_path, assessment_content,
                f"chore: add assessment for #{assess_issue_num}" if assess_issue_num else "chore: add assessment",
            )
            log.info("assessor.assessment_committed", path=assessment_path)
        except Exception as exc:
            log.warning("assessor.commit_assessment_failed", error=str(exc))

        perf.state = "assessment_complete"
        return PerformerResponse(
            status="assessment_complete",
            session_id=perf.session_id,
        )

    # Insufficient — block with questions
    perf.assessment_questions = [str(q) for q in questions]
    perf.state = "blocked"
    perf.open_questions = perf.assessment_questions
    return PerformerResponse(
        status="blocked",
        session_id=perf.session_id,
        questions=perf.assessment_questions,
    )


async def _assessor_workflow_response(
    perf: Performance,
    assess_raw: str,
) -> PerformerResponse:
    """166: map an assessment WORKFLOW record; None when not a record."""

    from performer import main as _m

    _extract_json = _m._extract_json

    _ar = _extract_json(assess_raw) if isinstance(assess_raw, str) else assess_raw
    if isinstance(_ar, dict) and isinstance(_ar.get("assessment"), dict):
        _assessment = _ar["assessment"]
        _ready = bool(_assessment.get("ready"))
        _questions = [str(q) for q in (_assessment.get("questions") or [])]
        _verdict = str(_assessment.get("verdict") or "work")
        log.info(
            "assessor.assessment_reported",
            ready=_ready,
            questions=len(_questions),
            criteria_source=_assessment.get("criteria_source"),
            verdict=_verdict,
            session_id=perf.session_id,
        )
        # 410: a verdict that declines the card never advances the
        # lifecycle — coordinare turns it into the defined board outcome
        # (comment + close for not_work, comment + back to backlog for
        # needs_split) instead of dispatching the architect.
        if _verdict in ("not_work", "needs_split"):
            perf.state = "assessment_complete"
            return PerformerResponse(
                status="assessment_not_work" if _verdict == "not_work" else "assessment_needs_split",
                session_id=perf.session_id,
                report=_ar,
                progress=f"assessment ({_verdict})",
            )
        if _ready:
            perf.state = "assessment_complete"
            return PerformerResponse(
                status="assessment_complete",
                session_id=perf.session_id,
                report=_ar,
                progress="assessment (ready)",
            )
        # Not ready: the same blocked shape the prose assessor returns
        # (FR-011, FR-015), so coordinare's open_questions path and the
        # issue comment are untouched. The gate guarantees a question.
        perf.assessment_questions = _questions
        perf.state = "blocked"
        perf.open_questions = perf.assessment_questions
        return PerformerResponse(
            status="blocked",
            session_id=perf.session_id,
            questions=perf.assessment_questions,
            report=_ar,
        )


async def _assessor_lenient_response(
    perf: Performance,
    assess_raw: str,
    settings: Settings | None,
) -> PerformerResponse:
    """Unparseable/empty assessment: retry with a nudge, else the lenient 077 commit. None when parsed."""

    from performer import main as _m

    _extract_json = _m._extract_json
    _handle_backend_parse_failure = _m._handle_backend_parse_failure
    _doc_folder = _m._doc_folder
    commit_file = _m.commit_file

    if not assess_raw.strip():
        return await _handle_backend_parse_failure(
            perf, assess_raw, "assessment", settings, "was empty",
        )
    assess_output = _extract_json(assess_raw) if isinstance(assess_raw, str) else assess_raw
    if not isinstance(assess_output, dict) or assessment_fields_outside_object(assess_raw, _extract_json) or (
        "assessment" in assess_output and not isinstance(assess_output["assessment"], dict)
    ) or (
        assessment_has_duplicate_contract_fields(assess_raw, _extract_json)
    ) or (
        assessment_has_invalid_field_types(assess_output)
    ) or (
        "assessment" in assess_output
        and any(key in assess_output for key in ("questions", "sufficient", "ready"))
    ) or (
        "sufficient" not in assess_output and "assessment" not in assess_output
        and (assess_output.get("questions") or "ready" in assess_output)
    ):
        contract_fragment, fragment_questions = assessment_fragment_questions(assess_raw, _extract_json)
        if fragment_questions:
            perf.assessment_questions = fragment_questions
            perf.open_questions = fragment_questions
        async def _assessor_lenient_sufficient() -> PerformerResponse:
            if fragment_questions:
                perf.assessment_questions = fragment_questions
                perf.open_questions = fragment_questions
                perf.state = "blocked"
                return PerformerResponse(
                    status="blocked", session_id=perf.session_id,
                    questions=fragment_questions,
                )
            # 077: prose assessment (no parseable JSON) → treat as sufficient.
            # We cannot extract blocking questions from prose, and the parser
            # already biases to sufficient when no questions are present
            # (see below), so prose-with-no-structured-questions is the same
            # case. The raw prose is committed as the assessment report (same
            # as the structured-sufficient path); no error_reason is produced,
            # so no unredacted text reaches operator surfaces.
            folder = _doc_folder(perf.score)
            assessment_path = f"{folder}/assessment.md"
            n = perf.score.issue_number
            # Redact before committing: unstructured backend output is
            # untrusted and may echo credentials; this path (unlike the
            # structured-sufficient one) handles arbitrary prose, so scrub it.
            content = (
                f"# Assessment: {perf.score.title}\n\n"
                f"_(Recorded from unstructured backend output.)_\n\n"
                f"{_redact_secrets(assess_raw)}"
            )
            try:
                await commit_file(
                    perf.stand, assessment_path, content,
                    f"chore: add assessment for #{n}" if n else "chore: add assessment",
                )
            except Exception as exc:
                log.warning("assessor.commit_assessment_failed", error=str(exc))
            perf.state = "assessment_complete"
            return PerformerResponse(
                status="assessment_complete", session_id=perf.session_id,
            )

        return await _handle_backend_parse_failure(
            perf, assess_raw, "assessment", settings,
            "could not be parsed as a JSON object",
            lenient_fallback=(
                _assessor_lenient_sufficient
                if fragment_questions or not contract_fragment else None
            ),
        )

    return None




async def intake_path(
    perf: Performance,
    backend_status,
    settings: Settings | None,
) -> PerformerResponse:
    """Advocate/curator role path (spec 173): map the intake run record onto a terminal status."""

    from performer import main as _m

    _extract_json = _m._extract_json

    _key = "advocate" if perf.role == "advocate" else "curation"
    _ok = "advocate_complete" if perf.role == "advocate" else "curation_complete"
    _raw = backend_status.output or ""
    _ir = _extract_json(_raw) if isinstance(_raw, str) else _raw
    _rec = _ir.get(_key) if isinstance(_ir, dict) else None
    if (
        isinstance(_rec, dict)
        and _rec.get("verdict") in (_ok, "env_blocked")
        and isinstance(_rec.get("outcomes"), list)
        and isinstance(_rec.get("model_calls"), int)
    ):
        _verdict = str(_rec.get("verdict") or "")
        log.info(
            "intake.run_reported",
            role=perf.role,
            verdict=_verdict,
            outcomes=len(_rec.get("outcomes") or []),
            model_calls=_rec.get("model_calls"),
            error=_rec.get("error"),
        )
        perf.state = _verdict
        if _verdict == "env_blocked":
            _reason = str(_rec.get("error") or "the intake run could not complete")
            perf.open_questions = [_reason]
            return PerformerResponse(
                status="env_blocked", session_id=perf.session_id,
                questions=[_reason], report=_ir,
            )
        return PerformerResponse(
            status=_ok, session_id=perf.session_id,
            body=f"{len(_rec.get('outcomes') or [])} issue(s) handled",
            report=_ir,
        )
    # An unusable report must NOT fall through. For every other role a
    # fall-through lands on the prose path, which is a reasonable
    # default; for these two it lands on the tail that pushes a branch
    # and opens a pull request. There is no prose path to fall back to
    # here, so a malformed report is an error, reported as one.
    log.warning(
        "intake.report_unusable",
        role=perf.role,
        has_key=isinstance(_ir, dict) and _key in _ir,
    )
    perf.state = "error"
    perf.error_reason = f"{perf.role} run returned no usable report"
    return PerformerResponse(
        status="error", session_id=perf.session_id,
        body=f"{perf.role} run returned no usable report",
        report=_ir if isinstance(_ir, dict) else None,
    )


async def closing_record_path(
    perf: Performance,
    backend_status,
    settings: Settings | None,
) -> PerformerResponse:
    """Closer WORKFLOW record path (spec 172). Returns None when the output is not a workflow report so the shared reviewer prose path can run."""

    from performer import main as _m

    _extract_json = _m._extract_json

    _cr_raw = backend_status.output or ""
    _cr = _extract_json(_cr_raw) if isinstance(_cr_raw, str) else _cr_raw
    if (isinstance(_cr, dict) and isinstance(_cr.get("closing"), dict)
            and _cr["closing"].get("verdict") in ("approved", "changes_requested", "env_blocked")
            and isinstance(_cr["closing"].get("threads_read"), int)
            and isinstance(_cr["closing"].get("classifications"), list)):
        _closing = _cr["closing"]
        _verdict = str(_closing.get("verdict"))
        _open = [t for t in (_closing.get("open_threads") or []) if isinstance(t, dict)]
        log.info(
            "closer.record_reported",
            verdict=_verdict,
            threads=_closing.get("threads_read"),
            resolved=len(_closing.get("resolved") or []),
            open=len(_open),
            model_calls=(_closing.get("workflow_metrics") or {}).get("model_calls"),
            session_id=perf.session_id,
        )
        if _verdict == "env_blocked":
            perf.state = "env_blocked"
            return PerformerResponse(
                status="env_blocked", session_id=perf.session_id,
                reason=str(_closing.get("hold_reason") or "the closing review could not complete"), report=_cr,
            )
        if _verdict == "approved":
            # The workflow resolved what it judged; never resolve again here.
            perf.state = "approved"
            perf.review_suggestions = []
            return PerformerResponse(status="approved", session_id=perf.session_id, suggestions=[], report=_cr)
        _comments = [
            {"path": str(t.get("path") or ""), "line": int(t.get("line") or 0),
             "body": f"unresolved review thread: {t.get('excerpt') or t.get('thread_id')}"}
            for t in _open
        ]
        max_cycles = settings.REVIEWER_MAX_CYCLES if settings else 3
        perf.review_cycle += 1
        if perf.review_cycle >= max_cycles:
            summary = f"Review cycle limit reached ({perf.review_cycle}). Unresolved threads remain."
            perf.state = "blocked"
            perf.open_questions = [summary]
            return PerformerResponse(status="blocked", session_id=perf.session_id, questions=[summary], report=_cr)
        perf.review_comments = _comments
        perf.state = "changes_requested"
        return PerformerResponse(
            status="changes_requested", session_id=perf.session_id, comments=_comments,
            body=f"{len(_open)} review thread(s) still open", report=_cr,
        )

    return None


async def reviewer_path(
    perf: Performance,
    backend_status,
    settings: Settings | None,
) -> PerformerResponse:
    """Reviewer/closing_review prose path (spec 021): post the review verdict to the GitHub PR."""

    from performer import main as _m

    _extract_json = _m._extract_json
    _handle_backend_parse_failure = _m._handle_backend_parse_failure
    _persona_tag = _m._persona_tag

    review_raw = backend_status.output or ""
    # 169: the reviewer WORKFLOW reports a review record and has already
    # posted the one GitHub review itself (REQUEST_CHANGES with inline
    # comments, or COMMENT). Map its verdict onto the response the prose
    # path returns for that outcome and skip the prose post. Without the
    # report key the prose path below is untouched (FR-015).
    _workflow = await _reviewer_workflow_response(perf, review_raw, settings)
    if _workflow is not None:
        return _workflow

    _lenient = await _reviewer_lenient_response(perf, review_raw, settings)
    if _lenient is not None:
        return _lenient

    review_output = _extract_json(review_raw) if isinstance(review_raw, str) else review_raw
    return await _reviewer_post_response(perf, review_output, review_raw, settings)


async def _reviewer_workflow_response(
    perf: Performance,
    review_raw: str,
    settings: Settings | None,
) -> PerformerResponse:
    """169: map a reviewer WORKFLOW record onto a terminal response; None when not a workflow record."""

    from performer import main as _m

    _extract_json = _m._extract_json
    get_head_sha = _m.get_head_sha

    _rr = _extract_json(review_raw) if isinstance(review_raw, str) else review_raw
    if isinstance(_rr, dict) and isinstance(_rr.get("review"), dict) and _rr["review"].get("verdict") in ("approved", "changes_requested", "env_blocked", "nothing_to_review"):
        _review = _rr["review"]
        _verdict = str(_review.get("verdict") or "")
        _findings = [f for f in (_review.get("findings") or []) if isinstance(f, dict)]
        log.info(
            "reviewer.review_reported",
            verdict=_verdict,
            findings=len(_findings),
            dropped=len(_review.get("findings_dropped") or []),
            coverage_pass=_review.get("coverage_pass_ran"),
            posted=_review.get("posted_review_url"),
            session_id=perf.session_id,
        )
        if _verdict == "approved":
            perf.state = "approved"
            perf.review_suggestions = []
            return PerformerResponse(status="approved", session_id=perf.session_id, suggestions=[], report=_rr)
        if _verdict == "changes_requested":
            _comments = [
                {
                    "path": str(f.get("path") or ""),
                    "line": int(f.get("line") or 0),
                    "body": f"{f.get('category')}: {f.get('problem')} Why blocking: {f.get('why_blocking')}",
                }
                for f in _findings
            ]
            max_cycles = settings.REVIEWER_MAX_CYCLES if settings else 3
            perf.review_cycle += 1
            if perf.review_cycle >= max_cycles:
                summary = f"Review cycle limit reached ({perf.review_cycle}). Unresolved issues remain."
                perf.state = "blocked"
                perf.open_questions = [summary]
                return PerformerResponse(status="blocked", session_id=perf.session_id, questions=[summary], report=_rr)
            perf.review_comments = _comments
            perf.state = "changes_requested"
            return PerformerResponse(
                status="changes_requested", session_id=perf.session_id, comments=_comments,
                body=f"{len(_findings)} blocking finding(s) from the reviewer workflow", report=_rr,
            )
        if _verdict == "nothing_to_review":
            # 412: the parsed diff was empty. Advance with the note in
            # the report; never "approved", since there is no diff to
            # approve. The settled head rides along so the coordinare
            # can record the verdict slot and skip a re-dispatch.
            perf.state = "nothing_to_review"
            _head = None
            try:
                _head = await get_head_sha(perf.stand)
            except Exception as exc:
                log.warning("nothing_to_review.head_after_failed", error=str(exc))
            return PerformerResponse(status="nothing_to_review", session_id=perf.session_id, report=_rr, head_after=_head)
        _reason = str(_review.get("post_error") or "")
        if not _reason:
            _unread = _review.get("unread_files") or []
            _reason = "the review could not cover every changed file: " + ", ".join(str(u) for u in _unread[:10])
        perf.state = "env_blocked"
        return PerformerResponse(status="env_blocked", session_id=perf.session_id, reason=_reason, report=_rr)
async def _reviewer_lenient_response(
    perf: Performance,
    review_raw: str,
    settings: Settings | None,
) -> PerformerResponse:
    """Unparseable/empty review output: retry with a nudge, else the lenient 077 fallback. None when the output parsed."""

    from performer import main as _m

    _extract_json = _m._extract_json
    _handle_backend_parse_failure = _m._handle_backend_parse_failure
    _persona_tag = _m._persona_tag
    post_pull_request_review = _m.post_pull_request_review

    if not review_raw.strip():
        return await _handle_backend_parse_failure(
            perf, review_raw, "review", settings, "was empty",
        )
    review_output = _extract_json(review_raw) if isinstance(review_raw, str) else review_raw
    if not isinstance(review_output, dict):
        async def _reviewer_lenient_changes() -> PerformerResponse:
            # 077: prose review (no parseable JSON) → request changes. NEVER
            # auto-approve on ambiguity (safety). Post the model's notes as
            # feedback so the implementer can act, REDACTED before posting to
            # the PR (a public surface), and keep the loop moving instead of
            # hard-erroring → Blocked column.
            pr_number = 0
            pr_url = (perf.pr_url or "").rstrip("/")
            if pr_url and "/" in pr_url:
                try:
                    pr_number = int(pr_url.rsplit("/", 1)[-1])
                except (ValueError, IndexError):
                    pass
            if pr_number <= 0:
                perf.state = "error"
                perf.error_reason = (
                    f"Cannot post review: pr_url is missing or invalid ({perf.pr_url!r})"
                )
                return PerformerResponse(
                    status="error", session_id=perf.session_id,
                    reason=perf.error_reason,
                )
            owner, repo = perf.score.owner_repo
            token = perf.score.effective_github_token
            header_label = (
                "Bot Closer Review" if perf.role == "closing_review" else "Bot Review"
            )
            safe_body = _redact_secrets(review_raw[:1500])
            full_body = (
                f"{_persona_tag(perf.score, perf.role)}\n\n"
                f"**{header_label}: CHANGES REQUESTED**\n\n"
                "_(Backend did not return a structured verdict; recording its "
                "notes verbatim.)_\n\n"
                f"{safe_body}"
            )
            try:
                await post_pull_request_review(
                    owner, repo, pr_number, event="COMMENT",
                    body=full_body, comments=[], token=token,
                )
            except Exception as exc:
                log.warning("reviewer.lenient_post_failed", error=str(exc))
            max_cycles = settings.REVIEWER_MAX_CYCLES if settings else 3
            perf.review_cycle += 1
            if perf.review_cycle >= max_cycles:
                summary = (
                    f"Review cycle limit reached ({perf.review_cycle}). "
                    "Unresolved issues remain."
                )
                perf.state = "blocked"
                perf.open_questions = [summary]
                return PerformerResponse(
                    status="blocked", session_id=perf.session_id,
                    questions=[summary],
                )
            perf.review_comments = []
            perf.state = "changes_requested"
            return PerformerResponse(
                status="changes_requested", session_id=perf.session_id,
                body=safe_body or None,
            )

        return await _handle_backend_parse_failure(
            perf, review_raw, "review", settings,
            "could not be parsed as a JSON object",
            lenient_fallback=_reviewer_lenient_changes,
        )
async def _reviewer_empty_rejection_response(
    perf: Performance,
    review_raw: str,
    settings: Settings | None,
) -> PerformerResponse:
    """153: a rejection with no comments and no body is not actionable; retry then block."""

    from performer import main as _m

    _handle_backend_parse_failure = _m._handle_backend_parse_failure

    async def _reviewer_empty_rejection() -> PerformerResponse:
        summary = (
            f"The `{perf.role}` reviewer rejected this PR "
            f"(`approved=false`) but returned no structured comments "
            f"and no prose body across "
            f"{perf.parse_retry_count + 1} attempt(s) — there is no "
            f"actionable feedback to relay to the implementer. "
            f"Operator triage required."
        )
        perf.state = "blocked"
        perf.open_questions = [summary]
        return PerformerResponse(
            status="blocked",
            session_id=perf.session_id,
            questions=[summary],
        )

    return await _handle_backend_parse_failure(
        perf, review_raw, "review", settings,
        "was a changes_requested verdict with no comments and no body",
        lenient_fallback=_reviewer_empty_rejection,
    )
async def _reviewer_post_response(
    perf: Performance,
    review_output,
    review_raw: str,
    settings: Settings | None,
) -> PerformerResponse:
    """Normalize the parsed review, post the verdict to GitHub, resolve threads on approval."""

    from performer import main as _m

    _persona_tag = _m._persona_tag
    post_pull_request_review = _m.post_pull_request_review
    resolve_pr_review_threads = _m.resolve_pr_review_threads

    is_approved = review_output.get("approved") is True  # strict bool check
    raw_comments = review_output.get("comments", [])
    # Normalize comments: strings become {"body": str}, dicts pass through
    comments = []
    if isinstance(raw_comments, list):
        for c in raw_comments:
            if isinstance(c, dict):
                comments.append(c)
            elif isinstance(c, str):
                comments.append({"body": c})
    raw_suggestions = review_output.get("suggestions", [])
    suggestions = raw_suggestions if isinstance(raw_suggestions, list) else []
    review_body = str(review_output.get("body", ""))
    if not is_approved and not comments and not review_body.strip():
        _empty = await _reviewer_empty_rejection_response(perf, review_raw, settings)
        if _empty is not None:
            return _empty

    # Always post as COMMENT — the human reviewer handles formal
    # approval.  Bot reviews provide feedback for the implementer.
    verdict = "APPROVED" if is_approved else "CHANGES REQUESTED"
    event = "COMMENT"

    # Extract PR number from pr_url (set by implementer earlier in lifecycle)
    pr_number = 0
    pr_url = (perf.pr_url or "").rstrip("/")
    if pr_url and "/" in pr_url:
        try:
            pr_number = int(pr_url.rsplit("/", 1)[-1])
        except (ValueError, IndexError):
            pass

    if pr_number <= 0:
        perf.state = "error"
        perf.error_reason = f"Cannot post review: pr_url is missing or invalid ({perf.pr_url!r})"
        return PerformerResponse(
            status="error", session_id=perf.session_id,
            reason=perf.error_reason,
        )

    owner, repo = perf.score.owner_repo
    token = perf.score.effective_github_token
    header_label = "Bot Closer Review" if perf.role == "closing_review" else "Bot Review"
    full_body = f"{_persona_tag(perf.score, perf.role)}\n\n**{header_label}: {verdict}**\n\n{review_body}"
    await post_pull_request_review(
        owner, repo, pr_number, event=event,
        body=full_body, comments=comments, token=token,
    )

    if is_approved:
        # Resolve all open review threads — the reviewer has verified
        # that the implementer's fixes address the feedback.
        try:
            resolved = await resolve_pr_review_threads(
                owner, repo, pr_number, token,
            )
            if resolved:
                log.info("reviewer.resolved_threads", pr_number=pr_number, count=resolved)
        except Exception as exc:
            log.warning("reviewer.resolve_threads_failed", error=str(exc))

        perf.state = "approved"
        perf.review_suggestions = suggestions
        return PerformerResponse(
            status="approved",
            session_id=perf.session_id,
            suggestions=suggestions,
        )
    return await _reviewer_changes_response(perf, comments, review_body, settings)
async def _reviewer_changes_response(
    perf: Performance,
    comments,
    review_body: str,
    settings: Settings | None,
) -> PerformerResponse:
    """Changes-requested tail: cycle-limit guard, then request changes."""

    # Changes requested
    max_cycles = settings.REVIEWER_MAX_CYCLES if settings else 3
    perf.review_cycle += 1
    if perf.review_cycle >= max_cycles:
        summary = f"Review cycle limit reached ({perf.review_cycle}). Unresolved issues remain."
        perf.state = "blocked"
        perf.open_questions = [summary]
        return PerformerResponse(
            status="blocked",
            session_id=perf.session_id,
            questions=[summary],
        )
    perf.review_comments = comments
    perf.state = "changes_requested"
    return PerformerResponse(
        status="changes_requested",
        session_id=perf.session_id,
        comments=comments,
        body=review_body or None,
    )




async def security_path(
    perf: Performance,
    backend_status,
    settings: Settings | None,
) -> PerformerResponse:
    """Security role path (spec 022/170): analyse findings, post advisories, pass or fail."""
    from performer import main as _m

    _extract_json = _m._extract_json
    _handle_backend_parse_failure = _m._handle_backend_parse_failure
    _doc_folder = _m._doc_folder
    commit_file = _m.commit_file

    sec_raw = backend_status.output or ""
    _workflow = await _security_workflow_response(perf, sec_raw, settings)
    if _workflow is not None:
        return _workflow
    if not sec_raw.strip():
        return await _handle_backend_parse_failure(
            perf, sec_raw, "security", settings, "was empty",
        )
    sec_output = _extract_json(sec_raw) if isinstance(sec_raw, str) else sec_raw
    if not isinstance(sec_output, dict):
        return await _handle_backend_parse_failure(
            perf, sec_raw, "security", settings,
            "could not be parsed as a JSON object",
        )

    raw_findings = sec_output.get("findings", [])
    findings = raw_findings if isinstance(raw_findings, list) else []

    # Commit security report to the architecture folder
    folder = _doc_folder(perf.score)
    sec_report = f"# Security Report: {perf.score.title}\n\n"
    passed = sec_output.get("passed", len(findings) == 0)
    sec_report += f"**Result: {'PASSED' if passed else 'FAILED'}**\n\n"
    if findings:
        sec_report += "## Findings\n\n"
        for f in findings:
            if isinstance(f, dict):
                sev = str(f.get("severity", "unknown"))
                cat = str(f.get("category", "unknown"))
                desc = str(f.get("description", ""))
                fpath = str(f.get("file", ""))
                sec_report += f"- **[{sev.upper()}]** {cat}"
                if fpath:
                    sec_report += f" (`{fpath}`)"
                sec_report += f"\n  {desc}\n\n"
    else:
        sec_report += "No security findings.\n"
    try:
        issue_num = perf.score.issue_number
        await commit_file(
            perf.stand, f"{folder}/security.md", sec_report,
            f"chore: add security report for #{issue_num}" if issue_num else "chore: add security report",
        )
    except Exception as exc:
        log.warning("security.commit_report_failed", error=str(exc))
    await _security_advisory_comments(perf, findings)

    return await _security_verdict_response(perf, findings, settings)


async def _security_workflow_response(
    perf: Performance,
    sec_raw: str,
    settings: Settings | None,
) -> PerformerResponse:
    """170: map a security WORKFLOW record onto routed statuses; None when not a record."""

    from performer import main as _m

    _extract_json = _m._extract_json
    get_head_sha = _m.get_head_sha

    # 170: the security WORKFLOW reports a security record: the scan ran
    # inside the performer, the gate assigned severity and routing by code,
    # and the one GitHub review is already posted. Map the record onto the
    # statuses coordinare routes today (022 findings shape) and skip the
    # prose post-processing (committed report, advisory comments). Without
    # the report key the prose path below is untouched.
    _sr = _extract_json(sec_raw) if isinstance(sec_raw, str) else sec_raw
    if isinstance(_sr, dict) and isinstance(_sr.get("security"), dict) and _sr["security"].get("verdict") in ("security_passed", "security_failed", "env_blocked", "nothing_to_scan", "not_applicable"):
        _sec = _sr["security"]
        _verdict = str(_sec.get("verdict") or "")
        _blocking = [f for f in (_sec.get("blocking") or []) if isinstance(f, dict)]
        _advisory = [f for f in (_sec.get("advisory") or []) if isinstance(f, dict)]
        log.info(
            "security.record_reported",
            verdict=_verdict,
            blocking=len(_blocking),
            advisory=len(_advisory),
            dropped=len(_sec.get("findings_dropped") or []),
            scan=[(r.get("tool"), r.get("exit_code"), r.get("finding_count")) for r in (_sec.get("scan") or []) if isinstance(r, dict)],
            posted=_sec.get("posted_review_url"),
            session_id=perf.session_id,
        )

        def _as_022(f: dict) -> dict:
            desc = f"{f.get('category')}: {f.get('problem')} Why blocking: {f.get('why_blocking')}"
            if f.get("evidence"):
                desc += f" Evidence: {f.get('evidence')}"
            return {
                "severity": str(f.get("severity") or "high"),
                "category": str(f.get("category") or "other_insecure_pattern"),
                "description": desc,
                "file": str(f.get("path") or ""),
                "line": int(f.get("line") or 0),
                "routing": str(f.get("routing") or "implementer"),
            }

        if _verdict == "security_passed":
            perf.state = "security_passed"
            return PerformerResponse(status="security_passed", session_id=perf.session_id, report=_sr)
        if _verdict == "security_failed":
            max_cycles = settings.SECURITY_MAX_CYCLES if settings else 3
            perf.security_cycle += 1
            if perf.security_cycle >= max_cycles:
                summary = f"Security: {len(_blocking)} blocking finding(s) after {perf.security_cycle} fix attempt(s)"
                perf.state = "blocked"
                perf.open_questions = [summary]
                return PerformerResponse(status="blocked", session_id=perf.session_id, questions=[summary], report=_sr)
            perf.security_findings = [_as_022(f) for f in _blocking]
            perf.state = "security_failed"
            return PerformerResponse(
                status="security_failed", session_id=perf.session_id, findings=perf.security_findings, report=_sr,
            )
        if _verdict in ("nothing_to_scan", "not_applicable"):
            # 412: advance-with-note. Nothing statically scannable
            # changed, decided in code from the file list -- recorded
            # as its own verdict, not a pass, so the operator sees it.
            # The settled head rides along so the coordinare can record
            # the verdict slot and skip a re-dispatch.
            perf.state = _verdict
            _head = None
            try:
                _head = await get_head_sha(perf.stand)
            except Exception as exc:
                log.warning("security_advance.head_after_failed", error=str(exc))
            return PerformerResponse(status=_verdict, session_id=perf.session_id, report=_sr, head_after=_head)
        _reason = str(_sec.get("hold_reason") or _sec.get("post_error") or "")
        if not _reason:
            _reason = "the security review could not complete: " + ", ".join(str(u) for u in (_sec.get("unread_files") or [])[:10])
        perf.state = "env_blocked"
        return PerformerResponse(status="env_blocked", session_id=perf.session_id, reason=_reason, report=_sr)


async def _security_advisory_comments(
    perf: Performance,
    findings,
) -> PerformerResponse:
    """FR-007: post deduped advisory comments for medium/low findings."""

    from performer import main as _m

    list_pr_comments = _m.list_pr_comments
    post_pr_comment = _m.post_pr_comment
    _parse_advisory_header = _m._parse_advisory_header
    _advisory_fingerprint = _m._advisory_fingerprint
    _persona_tag = _m._persona_tag

    # Extract PR number for advisory comments
    pr_number = 0
    pr_url = (perf.pr_url or "").rstrip("/")
    if pr_url and "/" in pr_url:
        try:
            pr_number = int(pr_url.rsplit("/", 1)[-1])
        except (ValueError, IndexError):
            pass

    # Post advisory comments for medium/low findings (FR-007)
    advisory = [f for f in findings if isinstance(f, dict) and f.get("severity") in ("medium", "low")]
    if advisory and pr_number <= 0:
        log.warning(
            "security.advisory_comments_skipped",
            count=len(advisory),
            reason="pr_url missing or invalid — cannot post advisory comments",
        )
    if advisory and pr_number > 0:
        owner, repo = perf.score.owner_repo
        token = perf.score.effective_github_token
        seen_fingerprints: set[str] = set()
        try:
            existing = await list_pr_comments(owner, repo, pr_number, token=token)
            for c in existing:
                parsed = _parse_advisory_header(c.get("body", "") if isinstance(c, dict) else "")
                if parsed:
                    seen_fingerprints.add(_advisory_fingerprint(*parsed))
        except Exception as exc:
            log.warning("advisory_list_failed", error=str(exc))
        for finding in advisory:
            cat = finding.get("category", "unknown")
            sev = finding.get("severity", "")
            desc = finding.get("description", "")
            fp = _advisory_fingerprint(cat, sev)
            if fp in seen_fingerprints:
                log.info("advisory_comment_skipped_dedup", category=cat, severity=sev, fingerprint=fp)
                continue
            body = f"{_persona_tag(perf.score, perf.role)}\n\n[Advisory - Security] **{cat}** ({sev})\n\n{desc}"
            try:
                await post_pr_comment(owner, repo, pr_number, body=body, token=token)
                seen_fingerprints.add(fp)
            except Exception as exc:
                log.warning("advisory_comment_failed", category=cat, error=str(exc), exc_info=True)

    return None


async def _security_verdict_response(
    perf: Performance,
    findings,
    settings: Settings | None,
) -> PerformerResponse:
    """022: pass on no blocking findings; cycle-guard and fail otherwise."""

    # Check for blocking findings (critical/high)
    blocking = [f for f in findings if isinstance(f, dict) and f.get("severity") in ("critical", "high")]
    if not blocking:
        perf.state = "security_passed"
        return PerformerResponse(
            status="security_passed",
            session_id=perf.session_id,
        )

    # Blocking findings exist
    max_cycles = settings.SECURITY_MAX_CYCLES if settings else 3
    perf.security_cycle += 1
    if perf.security_cycle >= max_cycles:
        summary = f"Security: {len(blocking)} blocking finding(s) after {perf.security_cycle} fix attempt(s)"
        perf.state = "blocked"
        perf.open_questions = [summary]
        return PerformerResponse(
            status="blocked",
            session_id=perf.session_id,
            questions=[summary],
        )
    perf.security_findings = [f for f in blocking if isinstance(f, dict)]
    perf.state = "security_failed"
    return PerformerResponse(
        status="security_failed",
        session_id=perf.session_id,
        findings=perf.security_findings,
    )




async def documenting_path(
    perf: Performance,
    backend_status,
    settings: Settings | None,
) -> PerformerResponse:
    """Documenter role path (spec 024/124/171): docs record mapping, plan/write phases, batch commit."""
    from performer import main as _m

    _extract_json = _m._extract_json
    _handle_backend_parse_failure = _m._handle_backend_parse_failure
    _safe_doc_page_path = _m._safe_doc_page_path
    _advance_doc_writes = _m._advance_doc_writes
    _commit_doc_batch = _m._commit_doc_batch
    _start_doc_write = _m._start_doc_write
    _doc_write_dispatch_error = _m._doc_write_dispatch_error

    docs_raw = backend_status.output or ""
    _workflow = await _documenting_workflow_response(perf, docs_raw)
    if _workflow is not None:
        return _workflow
    # WRITE phase: a per-page write just completed → accumulate + advance.
    if perf.doc_phase == "writing":
        return await _advance_doc_writes(perf, docs_raw, settings)

    # PLAN phase: this first completion is the page plan.
    if not docs_raw.strip():
        # Nothing emitted — treat as "no doc changes" (FR-010).
        perf.state = "docs_committed"
        perf.docs_files_modified = []
        return PerformerResponse(
            status="docs_committed", session_id=perf.session_id, files_modified=[],
        )
    plan = _extract_json(docs_raw) if isinstance(docs_raw, str) else docs_raw
    if not isinstance(plan, dict):
        return await _handle_backend_parse_failure(
            perf, docs_raw, "docs plan", settings,
            "could not be parsed as a JSON object",
        )
    raw_del = plan.get("deletions", [])
    perf.doc_deletions = (
        [d for d in raw_del if isinstance(d, str) and d]
        if isinstance(raw_del, list) else []
    )
    # Backward-compat: only hermes is prompted to PLAN. A backend that
    # emitted the legacy single-shot {files} manifest (e.g. a non-hermes
    # documenter, or an old-image hermes) has no "pages" key — commit it
    # directly instead of mis-reading it as an empty plan and silently
    # writing nothing.
    if "pages" not in plan and isinstance(plan.get("files"), list):
        perf.doc_files_pending = [
            f for f in plan["files"]
            if isinstance(f, dict) and f.get("path") and isinstance(f.get("content"), str)
        ]
        return await _commit_doc_batch(perf)
    raw_pages = plan.get("pages", [])
    pages = [
        {"path": p["path"], "intent": str(p.get("intent", "")).strip()}
        for p in (raw_pages if isinstance(raw_pages, list) else [])
        if isinstance(p, dict) and _safe_doc_page_path(p.get("path"))
    ]
    if not pages:
        # Nothing to write — commit any planned deletions (or no-op).
        return await _commit_doc_batch(perf)
    perf.doc_phase = "writing"
    perf.doc_write_queue = pages
    perf.parse_retry_count = 0
    try:
        await _start_doc_write(perf, pages[0], settings)
    except Exception as exc:
        return _doc_write_dispatch_error(perf, pages[0], exc)
    return PerformerResponse(
        status="working", session_id=perf.session_id,
        progress=f"Documenting: writing {pages[0]['path']} (1/{len(pages)})",
    )


async def _documenting_workflow_response(
    perf: Performance,
    docs_raw: str,
) -> PerformerResponse:
    """171: map a documenter WORKFLOW record onto docs_committed/env_blocked; None when not a record."""

    from performer import main as _m

    _extract_json = _m._extract_json
    create_pull_request = _m.create_pull_request

    # 171: the documenter WORKFLOW reports a docs record: the page set was
    # chosen by code, every page passed the contract, and the one commit
    # (with push) is already made through commit_files. Map the record
    # onto docs_committed with the files it wrote, or env_blocked for a
    # hold, and skip the prose plan-then-write path. A dict without a known
    # verdict is not a workflow report (a prose model may emit a "docs"
    # key): the prose path below is untouched.
    _dr = _extract_json(docs_raw) if isinstance(docs_raw, str) else docs_raw
    if isinstance(_dr, dict) and isinstance(_dr.get("docs"), dict) and _dr["docs"].get("verdict") in ("docs_committed", "env_blocked"):
        _docs = _dr["docs"]
        _written = [str(p) for p in (_docs.get("files_written") or []) if isinstance(p, str)]
        _retired = [str(p) for p in (_docs.get("files_retired") or []) if isinstance(p, str)]
        _pointers = [str(p) for p in (_docs.get("pointers_refreshed") or []) if isinstance(p, str)]
        log.info(
            "documenter.record_reported",
            verdict=_docs.get("verdict"),
            mode=_docs.get("mode"),
            planned=len(_docs.get("plan") or []),
            written=len(_written),
            retired=len(_retired),
            dropped=sum(1 for r in (_docs.get("results") or []) if isinstance(r, dict) and r.get("dropped")),
            commit=_docs.get("commit_sha"),
            session_id=perf.session_id,
        )
        if _docs.get("verdict") == "env_blocked":
            perf.state = "env_blocked"
            return PerformerResponse(
                status="env_blocked", session_id=perf.session_id,
                reason=str(_docs.get("hold_reason") or "the documenter could not complete"), report=_dr,
            )
        perf.docs_files_modified = _written + _retired + _pointers
        perf.state = "docs_committed"
        # 124(US2): the symphony-init dispatch is cardless; open the seed PR as
        # the prose path does so WikiInitService can auto-merge it.
        pr_url = pr_node_id = None
        _pr_error: str | None = None
        if getattr(perf.score, "doc_mode", "update") == "init" and perf.docs_files_modified:
            try:
                owner, repo = perf.score.owner_repo
                pr_url, pr_node_id = await create_pull_request(
                    owner, repo, perf.score, perf.stand.branch, perf.score.effective_github_token,
                )
                perf.pr_url, perf.pr_node_id = pr_url, pr_node_id
            except Exception as exc:  # noqa: BLE001 - never fail the doc commit on PR open
                _pr_error = f"wiki init PR open failed (the branch carries the wiki): {exc}"
                log.error("wiki_init.pr_open_failed", error=str(exc))
        return PerformerResponse(
            status="docs_committed", session_id=perf.session_id, files_modified=perf.docs_files_modified,
            pr_url=pr_url, pr_node_id=pr_node_id, report=_dr, reason=_pr_error,
        )




async def bootstrap_path(
    perf: Performance,
    backend_status,
    settings: Settings | None,
) -> PerformerResponse:
    """Env-bootstrap role path (spec 060/101/107): workflow report mapping, service inference, readiness gate, verify."""
    from performer import main as _m

    _extract_json = _m._extract_json
    get_settings = _m.get_settings
    asyncio = _m.asyncio
    _run_service_inference = _m._run_service_inference

    _workflow = await _bootstrap_workflow_response(perf, backend_status)
    if _workflow is not None:
        return _workflow
    # 107: START declared services BEFORE running verify.sh. verify.sh
    # embeds a LIVE service probe (spec-093 pg_isready/redis PING) that
    # hard-fails when the service isn't running, so it MUST run after the
    # spec-101 readiness gate has started the services. Order:
    #   inference (writes services-start.sh) → readiness (starts + health-
    #   checks) → verify (toolchain + live service probe, now satisfied).
    # Running verify first made it fail on the service probe before anything
    # started the service, returning early so the readiness gate that starts
    # it was never reached (the website-postgres "never came up" bug).
    inference_timeout = get_settings().SERVICE_INFERENCE_TIMEOUT
    try:
        perf.inference_state = await asyncio.wait_for(
            _run_service_inference(
                perf.stand.path, perf.score.env_cache_path,
            ),
            timeout=inference_timeout,
        )
    except asyncio.TimeoutError:
        # 076 (live QA #150): service_inference is a best-effort,
        # secondary probe; a timeout MUST NOT by itself fail the whole
        # bootstrap (re-dispatch-forever). Record the skip and fall
        # through to the 101 readiness gate, which only blocks when the
        # symphony declares REQUIRED services that aren't connectable.
        log.warning(
            "service_inference.timeout",
            job_id=perf.session_id,
            timeout_seconds=inference_timeout,
            detail=(
                "inference timed out (best-effort); deferring to the "
                "service-readiness gate for required-service handling"
            ),
        )
        perf.inference_state = {
            "inference_succeeded": False,
            "inference_skipped_reason": "timeout",
        }
    ready_ok, ready_failures = await _bootstrap_service_readiness(perf)
    if not ready_ok:
        perf.state = "error"
        perf.error_reason = (
            "env-cache required service(s) not ready: "
            + "; ".join(f["reason"] for f in ready_failures)
        )
        log.warning(
            "env_bootstrap.service_readiness_failed",
            session_id=perf.session_id,
            failures=[f["service"] for f in ready_failures],
        )
        return PerformerResponse(
            status="error",
            session_id=perf.session_id,
            reason=perf.error_reason,
        )

    return await _bootstrap_verify_response(perf)


async def _bootstrap_workflow_response(
    perf: Performance,
    backend_status,
) -> PerformerResponse:
    """Map a BootstrapRun workflow report; None when score.workflow is not env_bootstrap."""

    from performer import main as _m

    _extract_json = _m._extract_json

    if perf.score.workflow == "env_bootstrap":
        from pydantic import ValidationError

        from performer.workflows.env_bootstrap import BootstrapRun

        raw = _extract_json(backend_status.output or "")
        try:
            run = BootstrapRun.model_validate(raw.get("env_bootstrap_run") if isinstance(raw, dict) else None)
            if any(not key.startswith("inference_") or key not in PerformerResponse.model_fields for key in run.inference):
                raise ValueError("invalid inference fields")
            response = PerformerResponse.model_validate({
                **run.inference,
                "status": "env_bootstrap_complete" if run.status == "complete" else "error",
                "session_id": perf.session_id,
                "reason": run.reason or None,
            }, strict=True)
        except (ValidationError, ValueError):
            perf.state = "error"
            perf.error_reason = "invalid bootstrap workflow report"
            return PerformerResponse(status="error", session_id=perf.session_id, reason=perf.error_reason)
        perf.inference_state = run.inference
        perf.state = "env_bootstrap_complete" if run.status == "complete" else "error"
        perf.error_reason = run.reason or None
        return response


async def _bootstrap_service_readiness(
    perf: Performance,
) -> tuple[bool, list[dict]]:
    """101/116: start declared services and gate on readiness. Returns (ok, failures)."""

    from performer import main as _m

    _read_declared_services = _m._read_declared_services

    # 101: service-readiness completion gate — a cache is NOT complete
    # unless every REQUIRED declared service is started + connectable. A
    # rejected/empty manifest (or an unconnectable required service) →
    # bootstrap error, routed through on_bootstrap_complete(success=False)
    # so the cache is not marked ready and re-bootstraps (instead of
    # dispatching cards into a structurally-broken env). No declared
    # services → no-op (behavior unchanged). Started services stay running
    # so the verify.sh live probe below observes them.
    # 116: the 101 readiness gate is part of the coordinare-managed-services
    # subsystem. When coordinare does NOT manage services (the default), the
    # performer owns env setup end-to-end and bootstrap success is decided by
    # the toolchain verify.sh below — skip the gate entirely (pre-101 behavior).
    # The performer's own service inference (_run_service_inference →
    # apply_manual_override) still wrote services.json + scripts above.
    if getattr(perf.score, "coordinare_manages_services", True):
        from performer.workspace import run_service_readiness

        ready_ok, ready_failures = await run_service_readiness(
            perf.score.env_cache_path,
            getattr(perf.stand, "cache_env", None),
            _read_declared_services(perf.stand.path),
            perf.inference_state,
        )
    else:
        ready_ok, ready_failures = True, []
        log.info(
            "env_bootstrap.service_readiness_skipped",
            session_id=perf.session_id,
            reason="coordinare_manages_services=False (performer owns env setup)",
        )
    return ready_ok, ready_failures


async def _bootstrap_verify_response(
    perf: Performance,
) -> PerformerResponse:
    """077 (Tier 2): run verify.sh; error on failure, degraded on missing."""

    # 077 (Tier 2): confirm the install actually worked before reporting
    # success. The agent wrote verify.sh asserting every documented
    # dependency is present + runnable (and, for declared services, a live
    # readiness probe — now satisfied because readiness started them above);
    # a non-zero exit means a silent install failure (e.g. apt-get located
    # no package), so we FAIL the bootstrap here. The coordinare's
    # on_bootstrap_complete(success=False) path then clears readme_sha and
    # retries — instead of marking a broken cache "ready". A missing
    # verify.sh is treated as a degraded (legacy) bootstrap: logged, not
    # failed.
    from performer.workspace import run_env_cache_verify

    verify_passed, verify_detail = await run_env_cache_verify(
        perf.score.env_cache_path,
        getattr(perf.stand, "cache_env", None),
    )
    if verify_passed is False:
        perf.state = "error"
        perf.error_reason = (
            "env-cache verification failed (verify.sh non-zero): "
            f"{verify_detail[-600:]}"
        )
        log.warning(
            "env_bootstrap.verify_failed",
            session_id=perf.session_id,
            detail=verify_detail[-300:],
        )
        return PerformerResponse(
            status="error",
            session_id=perf.session_id,
            reason=perf.error_reason,
        )
    if verify_passed is None:
        log.warning(
            "env_bootstrap.verify_script_missing",
            session_id=perf.session_id,
            detail="verify.sh not written by bootstrap agent; "
            "cannot confirm install — proceeding as degraded",
        )

    perf.state = "env_bootstrap_complete"
    return PerformerResponse(
        status="env_bootstrap_complete",
        session_id=perf.session_id,
        **perf.inference_state,
    )




async def implementing_path(
    perf: Performance,
    backend_status,
    settings: Settings | None,
) -> PerformerResponse:
    """Implementer WORKFLOW record path (spec 167): map the run record onto a response. Returns None when the output is not a workflow report."""

    from performer import main as _m

    _extract_json = _m._extract_json
    get_head_sha = _m.get_head_sha
    push_branch = _m.push_branch
    _format_failure_excerpt = _m._format_failure_excerpt

    _ir_raw = backend_status.output or ""
    _ir = _extract_json(_ir_raw) if isinstance(_ir_raw, str) else _ir_raw
    if isinstance(_ir, dict) and isinstance(_ir.get("implementer_run"), dict):
        _run = _ir["implementer_run"]
        _status = str(_run.get("status") or "")
        _reason = str(_run.get("reason") or "")
        log.info(
            "implementer.run_reported",
            status=_status,
            milestones_completed=_run.get("milestones_completed"),
            milestones_planned=_run.get("milestones_planned"),
            turns=_run.get("turn_count"),
            session_id=perf.session_id,
        )
        _head: str | None = None
        try:
            _head = await get_head_sha(perf.stand)
        except Exception as exc:  # noqa: BLE001 - best effort
            log.warning("implementer.head_after_failed", error=str(exc))
        if _status == "pr_opened":
            perf.pr_url = str(_ir.get("pr_url") or perf.pr_url or "") or None
            perf.pr_node_id = str(_ir.get("pr_node_id") or "") or perf.pr_node_id
            perf.pr_head_sha = _head
            perf.state = "pr_opened"
            return PerformerResponse(
                status="pr_opened", session_id=perf.session_id, pr_url=perf.pr_url,
                pr_node_id=perf.pr_node_id, report=_ir, head_before=perf.head_at_start, head_after=_head,
                # 511: the artefact write-through needs the branch on both
                # pr_opened paths (this workflow path and the legacy
                # waiting_for_checks path in main.py).
                pushed_branch=perf.stand.branch,
                progress="implementer workflow: CI green",
            )
        if _status == "partial_progress":
            # #278: failed milestones reset to their start commit. Push
            # the surviving committed milestones before this clone dies,
            # without creating a PR for an incomplete task.
            try:
                await push_branch(perf.stand, perf.score)
            except Exception as exc:
                perf.state = "env_blocked"
                detail = " ".join(_format_failure_excerpt(str(exc), limit=400).split())[:400]
                reason = f"Partial progress checkpoint push failed: {detail}"
                log.warning("implementer.checkpoint_push_failed", error_type=type(exc).__name__,
                            session_id=perf.session_id)
                return PerformerResponse(
                    status="env_blocked", session_id=perf.session_id,
                    reason=reason, report=_ir,
                )
            # push_branch may rebase onto concurrent remote work.
            try:
                _head = await get_head_sha(perf.stand)
            except Exception as exc:
                log.warning("implementer.checkpoint_head_failed", error_type=type(exc).__name__,
                            session_id=perf.session_id)
                _head = None
            perf.state = "waiting_for_checks"
            return PerformerResponse(
                status="partial_progress", session_id=perf.session_id, report=_ir,
                next_focus=_run.get("next_focus_milestone"), reason=_reason,
                progress=_reason[:200] or "partial progress checkpoint",
                head_before=perf.head_at_start, head_after=_head,
            )
        if _status == "env_blocked":
            perf.state = "env_blocked"
            return PerformerResponse(status="env_blocked", session_id=perf.session_id, reason=_reason, report=_ir,
                                     pr_url=_ir.get("pr_url"), pr_node_id=_ir.get("pr_node_id"))
        perf.state = "changes_requested"
        return PerformerResponse(
            status="changes_requested", session_id=perf.session_id, reason=_reason,
            body=_reason, report=_ir, local_test_failed=True,
        )

    return None


async def sentinel_path(
    perf: Performance,
    backend_status,
    settings: Settings | None,
) -> PerformerResponse:
    """Partial-progress sentinel path (spec 070). Returns None when no sentinel is present so the shared push tail can run."""

    from performer import main as _m

    _extract_trailing_partial_progress = _m._extract_trailing_partial_progress
    push_branch = _m.push_branch
    get_head_sha = _m.get_head_sha
    post_pr_comment = _m.post_pr_comment
    _extract_pr_number = _m._extract_pr_number
    _compute_pr_comment_delta = _m._compute_pr_comment_delta

    sentinel = _extract_trailing_partial_progress(backend_status.output or "")
    if sentinel is not None:
        comment_body = str(sentinel.get("comment") or "").strip()
        next_focus = str(sentinel.get("next_focus") or "").strip() or None
        head_after: str | None = None
        try:
            await push_branch(perf.stand, perf.score)
        except Exception as exc:
            log.warning("partial_progress.push_failed", error=str(exc))
        try:
            head_after = await get_head_sha(perf.stand)
        except Exception as exc:
            log.warning("partial_progress.head_after_failed", error=str(exc))
        if comment_body and perf.score.pr_url:
            owner, repo = perf.score.owner_repo
            pr_number = _extract_pr_number(perf.score.pr_url)
            if pr_number:
                try:
                    await post_pr_comment(
                        owner, repo, pr_number,
                        body=f"[partial_progress] {comment_body}",
                        token=perf.score.effective_github_token,
                    )
                except Exception as exc:
                    log.warning("partial_progress.comment_failed", error=str(exc))
        perf.state = "waiting_for_checks"
        # 072 FR-072-6: emit bot_pr_comment_delta on all terminal
        # ProtocolResponses so the coordinare's per-role guardrail sees
        # signals from partial_progress turns too, not just blocked.
        partial_bot_delta = await _compute_pr_comment_delta(perf)
        return PerformerResponse(
            status="partial_progress",
            session_id=perf.session_id,
            progress=comment_body or "partial progress checkpoint",
            next_focus=next_focus,
            head_before=perf.head_at_start,
            head_after=head_after,
            bot_pr_comment_delta=partial_bot_delta,
        )

    return None


async def _terminal_status_response(
    perf: Performance,
    settings: Settings | None,
) -> PerformerResponse | None:
    """Stable responses for terminal/parked states; None when the run is
    still mid-flight.

    Without this guard, a coordinare poll arriving after _poll_check_runs
    sets perf.state = "blocked" would fall through to backend.get_status(),
    see "done", and re-execute the push/PR-open path.
    """
    from performer import main as _m

    _poll_check_runs = _m._poll_check_runs

    if perf.state == "plan_committed":
        return PerformerResponse(
            status="plan_committed",
            session_id=perf.session_id,
            plan_path=perf.plan_path,
        )
    if perf.state == "assessment_complete":
        return PerformerResponse(
            status="assessment_complete",
            session_id=perf.session_id,
        )
    if perf.state == "approved":
        return PerformerResponse(
            status="approved",
            session_id=perf.session_id,
            suggestions=perf.review_suggestions,
        )
    if perf.state == "changes_requested":
        return PerformerResponse(
            status="changes_requested",
            session_id=perf.session_id,
            comments=perf.review_comments,
        )
    if perf.state == "security_passed":
        return PerformerResponse(
            status="security_passed",
            session_id=perf.session_id,
        )
    if perf.state == "security_failed":
        return PerformerResponse(
            status="security_failed",
            session_id=perf.session_id,
            findings=perf.security_findings,
        )
    if perf.state == "qa_passed":
        return PerformerResponse(
            status="qa_passed",
            session_id=perf.session_id,
            report=perf.qa_report,
        )
    if perf.state == "qa_failed":
        return PerformerResponse(
            status="qa_failed",
            session_id=perf.session_id,
            failures=perf.qa_failures,
        )
    if perf.state == "qa_env_blocked":
        return PerformerResponse(
            status="qa_env_blocked",
            session_id=perf.session_id,
            reason=(perf.qa_report or {}).get("environment_error"),
            report=perf.qa_report,
        )
    if perf.state == "docs_committed":
        return PerformerResponse(
            status="docs_committed",
            session_id=perf.session_id,
            files_modified=perf.docs_files_modified,
        )
    if perf.state == "env_bootstrap_complete":
        return PerformerResponse(
            status="env_bootstrap_complete",
            session_id=perf.session_id,
            **perf.inference_state,
        )
    if perf.state == "diagnostic_complete":
        return PerformerResponse(
            status="diagnostic_complete",
            session_id=perf.session_id,
        )
    if perf.state == "blocked":
        return PerformerResponse(
            status="blocked",
            session_id=perf.session_id,
            questions=perf.open_questions,
        )

    # US5: if we've pushed and created the PR, poll check runs instead of the backend
    if perf.state == "waiting_for_checks":
        return await _poll_check_runs(perf, settings)

    return None

