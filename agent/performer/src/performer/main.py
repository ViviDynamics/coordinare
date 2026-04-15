"""Performer entrypoint — wire protocol message loop."""
from __future__ import annotations

import asyncio
import json as _json_module
from pathlib import Path
import os
import re
import sys
import uuid
from datetime import UTC, datetime

import psutil
import structlog
from pydantic import ValidationError

from performer.backends import UnsupportedBackendError, get_backend
from performer.backends.base import BackendAdapter, BackendStatus
from performer.config import Settings, get_settings
from performer.github import GitHubAPIError, create_pull_request, get_check_runs, post_pr_comment, post_pull_request_review, resolve_pr_review_threads, summarise_check_runs
from performer.models import Performance, Score, Stand
from performer.protocol import PerformerMessage, PerformerMetrics, PerformerResponse
from performer.workspace import commit_file
from performer.workspace import (
    BranchConflictError,
    WorkspaceSetupError,
    cleanup_stand,
    clone_repository,
    commit_files,
    get_head_sha,
    push_branch,
    run_command,
)

log = structlog.get_logger(__name__)

_CODE_FENCE_RE = re.compile(r"```(?:json)?\s*\n?(.*?)\n?\s*```", re.DOTALL | re.IGNORECASE)


# ---------------------------------------------------------------------------
# 043 — Pre-commit CI check
# ---------------------------------------------------------------------------


async def _run_ci_check(stand_path: Path, label: str = "performer") -> tuple[bool, str]:
    """Run the detected lint command in the workspace before committing.

    Returns ``(True, "")`` if lint passes or no linter detected, or
    ``(False, error_output)`` if lint fails.  The performer should either
    fix the issue or bail with an error.
    """
    try:
        from coordinare.services.ci_detection import detect
    except ImportError:
        # Performer may be deployed without the coordinare package installed
        # (standalone mode).  Fall back gracefully — the coordinare-side gate
        # provides the backstop.
        log.info("ci_check.coordinare_not_available", label=label)
        return True, ""

    result = detect(stand_path)
    if result.lint_command is None:
        log.info("ci_check.no_lint_detected", label=label, stack=result.stack)
        return True, ""

    log.info("ci_check.running", label=label, command=result.lint_command, stack=result.stack)
    run_result = await run_command(result.lint_command, stand_path, timeout=120)
    if run_result.success:
        log.info("ci_check.passed", label=label, command=result.lint_command, duration=run_result.duration_seconds)
        return True, ""

    error_output = (run_result.stderr + "\n" + run_result.stdout).strip()
    log.warning(
        "ci_check.failed",
        label=label,
        command=result.lint_command,
        exit_code=run_result.exit_code,
        output_preview=error_output[:200],
    )
    return False, error_output


def _extract_json(text: str) -> dict | list | None:
    """Try to extract a JSON object from text that may contain prose.

    Strategies: (1) parse full text, (2) find ```json``` code fence,
    (3) find first { ... } or [ ... ] substring.
    """
    try:
        return _json_module.loads(text)
    except (ValueError, TypeError):
        pass
    match = _CODE_FENCE_RE.search(text)
    if match:
        try:
            return _json_module.loads(match.group(1).strip())
        except (ValueError, TypeError):
            pass
    for start_char, end_char in [('{', '}'), ('[', ']')]:
        start = text.find(start_char)
        if start >= 0:
            end = text.rfind(end_char)
            if end > start:
                try:
                    return _json_module.loads(text[start:end + 1])
                except (ValueError, TypeError):
                    pass
    return None


def _doc_folder(score: Score) -> str:
    """Return the docs/cards/{issue}-{slug} path for this card."""
    title_slug = re.sub(r"[^a-z0-9]+", "-", score.title.lower()).strip("-")[:20] or "untitled"
    issue_num = score.issue_number
    if issue_num:
        return f"docs/cards/{issue_num}-{title_slug}"
    return f"docs/cards/{title_slug}"


# ---------------------------------------------------------------------------
# Process-level CPU sampler — must be initialised once at startup
# ---------------------------------------------------------------------------
_self_process: psutil.Process | None = None


def _init_metrics() -> None:
    global _self_process
    try:
        _self_process = psutil.Process(os.getpid())
        _self_process.cpu_percent(interval=None)  # prime the counter
    except psutil.NoSuchProcess:  # pragma: no cover
        pass


# ---------------------------------------------------------------------------
# Action handlers
# ---------------------------------------------------------------------------

def handle_health(settings: Settings) -> PerformerResponse:
    """Return healthy/unhealthy immediately without any I/O."""
    try:
        get_backend(settings.AGENT_BACKEND)
    except UnsupportedBackendError as exc:
        return PerformerResponse(status="unhealthy", reason=str(exc))
    return PerformerResponse(status="healthy")


async def handle_dispatch(
    msg: PerformerMessage,
    settings: Settings,
) -> tuple[PerformerResponse, Performance]:
    """Clone the repo, start the backend, and return accepted + session_id."""
    score = Score(**msg.payload)
    # All fields now come from Score (proper pydantic fields, not raw payload)
    role = score.role
    raw_backend = score.backend or settings.AGENT_BACKEND
    backend_name = raw_backend.replace("-", "_").lower()  # normalize kebab-case
    model_name = score.model or None

    # 036: Apply GitHub API URL from dispatch payload so the performer's
    # GitHub client connects to the same instance (e.g. GitHub Enterprise).
    github_api_url = score.github_api_url
    if github_api_url and isinstance(github_api_url, str) and github_api_url.strip():
        from urllib.parse import urlparse
        # Mirror coordinare-side validation: reject whitespace, restrict http to localhost
        # Log only scheme+host (redacted) to avoid leaking credentials/query params
        if any(ch.isspace() for ch in github_api_url.strip()):
            log.warning("dispatch.invalid_github_api_url", reason="whitespace")
        else:
            cleaned = github_api_url.strip().rstrip("/")
            parsed = urlparse(cleaned)
            safe_host = f"{parsed.scheme}://{parsed.hostname or ''}"
            if parsed.username is not None or parsed.password is not None:
                log.warning("dispatch.invalid_github_api_url", reason="credentials", host=safe_host)
            elif parsed.query or parsed.fragment:
                log.warning("dispatch.invalid_github_api_url", reason="query_or_fragment", host=safe_host)
            elif parsed.scheme == "https" and parsed.netloc:
                settings.GITHUB_API_URL = cleaned
            elif parsed.scheme == "http" and parsed.netloc:
                hostname = parsed.hostname or ""
                if hostname in ("localhost", "127.0.0.1", "::1"):
                    settings.GITHUB_API_URL = cleaned
                else:
                    log.warning("dispatch.invalid_github_api_url", reason="http_non_localhost", host=safe_host)
            else:
                log.warning("dispatch.invalid_github_api_url", reason="invalid_scheme_or_host", host=safe_host)

    stand: Stand = await clone_repository(score)
    try:
        backend = get_backend(backend_name)
        await backend.start(stand, score, model=model_name)
    except BaseException:
        cleanup_stand(stand)
        raise
    session_id = str(uuid.uuid4())
    # 021: read pr_url from Score (set on card by implementer)
    pr_url = score.pr_url or None
    pr_node_id = score.pr_node_id or None
    perf = Performance(
        session_id=session_id,
        stand=stand,
        score=score,
        backend=backend,
        role=role,
        pr_url=pr_url,
        pr_node_id=pr_node_id,
    )
    log.info("dispatch accepted", session_id=session_id)
    return PerformerResponse(
        status="accepted",
        session_id=session_id,
        backend=backend_name,
    ), perf


def _format_check_failures(failed_runs: list[dict]) -> str:  # type: ignore[type-arg]
    """Format failed check run details for relay to the backend."""
    parts = []
    for run in failed_runs:
        name = run.get("name", "unknown")
        output = run.get("output") or {}
        title = output.get("title") or ""
        summary = output.get("summary") or ""
        text = (output.get("text") or "")[:500]
        part = f"### {name}"
        if title:
            part += f"\n{title}"
        if summary:
            part += f"\n{summary}"
        if text:
            part += f"\n{text}"
        parts.append(part)
    return "\n\n".join(parts)


async def _poll_check_runs(perf: Performance, settings: Settings | None) -> PerformerResponse:
    """Poll GitHub Check Runs for the PR head commit and route accordingly."""
    if not perf.pr_head_sha:
        perf.state = "error"
        return PerformerResponse(
            status="error",
            session_id=perf.session_id,
            reason="PR head SHA not set; cannot poll check runs",
        )

    owner, repo = perf.score.owner_repo
    try:
        check_runs = await get_check_runs(
            owner, repo, perf.pr_head_sha, perf.score.effective_github_token
        )
    except GitHubAPIError as exc:
        if 400 <= exc.status_code < 500 and exc.status_code != 429:
            # Deterministic client error (bad/empty token, bad ref) — fail fast.
            # 429 (rate limit) is excluded: it is transient and falls through to retry.
            perf.state = "error"
            return PerformerResponse(
                status="error",
                session_id=perf.session_id,
                reason=f"check-run poll failed: {exc}",
            )
        log.warning("check_runs_api_error", session_id=perf.session_id, status_code=exc.status_code)
        return PerformerResponse(
            status="working",
            session_id=perf.session_id,
            progress="Waiting for CI checks (API error, retrying)...",
        )
    except asyncio.CancelledError:
        raise
    except Exception:
        log.warning("check_runs_api_error", session_id=perf.session_id)
        return PerformerResponse(
            status="working",
            session_id=perf.session_id,
            progress="Waiting for CI checks (API error, retrying)...",
        )

    verdict, failed = summarise_check_runs(check_runs)

    if verdict == "pass":
        perf.state = "pr_opened"
        return PerformerResponse(
            status="pr_opened",
            session_id=perf.session_id,
            pr_url=perf.pr_url,
            pr_node_id=perf.pr_node_id,
        )

    if verdict == "pending":
        return PerformerResponse(
            status="working",
            session_id=perf.session_id,
            progress="CI checks in progress...",
        )

    # verdict == "fail"
    max_attempts = settings.CHECK_MAX_ATTEMPTS if settings is not None else 3
    if perf.check_attempt >= max_attempts:
        names = ", ".join(r.get("name", "unknown") for r in failed)
        questions = [f"CI checks failed after {perf.check_attempt} fix attempt(s): {names}"]
        perf.state = "blocked"
        perf.open_questions = questions
        return PerformerResponse(
            status="blocked",
            session_id=perf.session_id,
            questions=questions,
        )

    perf.check_attempt += 1
    failure_msg = _format_check_failures(failed)
    await perf.backend.relay_feedback(
        f"CI checks failed. Fix the following:\n\n{failure_msg}"
    )
    perf.state = "working"
    return PerformerResponse(
        status="working",
        session_id=perf.session_id,
        progress=f"Fixing CI failures (attempt {perf.check_attempt})...",
    )


async def handle_status(
    msg: PerformerMessage,
    perf: Performance | None,
    settings: Settings | None = None,
) -> PerformerResponse:
    """Return the current session state."""
    if perf is None or msg.session_id != perf.session_id:
        return PerformerResponse(
            status="session_expired",
            session_id=msg.session_id,
            reason="No active performance with that session ID",
        )

    # FR-015: enforce per-session wall-clock timeout
    if settings is not None:
        elapsed = (datetime.now(UTC) - perf.started_at).total_seconds()
        if elapsed > settings.AGENT_TIMEOUT:
            perf.state = "error"
            perf.error_reason = f"session timed out after {elapsed:.0f}s"
            try:
                await perf.backend.stop()
            except Exception:
                pass
            return PerformerResponse(
                status="error",
                session_id=perf.session_id,
                reason=f"session timed out after {elapsed:.0f}s",
            )

    # Return a stable response for terminal/parked states without re-running
    # backend logic.  Without this guard, a coordinare poll arriving after
    # _poll_check_runs sets perf.state = "blocked" would fall through to
    # backend.get_status(), see "done", and re-execute the push/PR-open path.
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
    if perf.state == "docs_committed":
        return PerformerResponse(
            status="docs_committed",
            session_id=perf.session_id,
            files_modified=perf.docs_files_modified,
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

    backend_status: BackendStatus = perf.backend.get_status()

    if backend_status.state == "done":
        # 020: Architect path — commit plan file instead of opening a PR.
        # Note: backends must populate BackendStatus.output with the generated
        # plan content when role=="architecting". The architect backend adapter
        # (not yet implemented) will set this field; existing backends (opencode,
        # claude_code, codex) do not — they will hit the empty-plan error below.
        if perf.role == "architecting":
            plan_content = backend_status.output or ""
            if not plan_content.strip():
                perf.state = "error"
                perf.error_reason = "Backend produced an empty architecture plan"
                return PerformerResponse(
                    status="error",
                    session_id=perf.session_id,
                    reason="Backend produced an empty architecture plan",
                )
            folder = _doc_folder(perf.score)
            issue_num = perf.score.issue_number

            # Split plan and tasks if the separator is present
            tasks_content = ""
            if "---TASKS---" in plan_content:
                parts = plan_content.split("---TASKS---", 1)
                plan_content = parts[0].strip()
                tasks_content = parts[1].strip()

            plan_path = f"{folder}/plan.md"
            commit_msg = f"chore: add architecture plan for #{issue_num}" if issue_num else "chore: add architecture plan"
            await commit_file(perf.stand, plan_path, plan_content, commit_msg)

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

        # Assessor path — evaluate whether the card specification is sufficient.
        # Backend output is expected to be JSON with: sufficient (bool),
        # questions (list[str]).  When sufficient, reports assessment_complete
        # (a terminal success status that advances the lifecycle).  When
        # insufficient, reports blocked with the generated questions.
        if perf.role == "assessing":
            assess_raw = backend_status.output or ""
            if not assess_raw.strip():
                perf.state = "error"
                perf.error_reason = "Backend produced empty assessment output"
                return PerformerResponse(
                    status="error", session_id=perf.session_id,
                    reason="Backend produced empty assessment output",
                )
            assess_output = _extract_json(assess_raw) if isinstance(assess_raw, str) else assess_raw
            if assess_output is None:
                perf.state = "error"
                perf.error_reason = "Backend produced invalid JSON assessment output"
                return PerformerResponse(
                    status="error", session_id=perf.session_id,
                    reason="Backend produced invalid JSON assessment output",
                )
            if not isinstance(assess_output, dict):
                perf.state = "error"
                perf.error_reason = "Backend assessment output is not a JSON object"
                return PerformerResponse(
                    status="error", session_id=perf.session_id,
                    reason="Backend assessment output is not a JSON object",
                )

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

        # 021: Reviewer path — post review to GitHub PR, return approved or changes_requested.
        # Backend output is expected to be JSON with: approved (bool), comments (list),
        # suggestions (list), body (str). Existing backends don't produce this yet —
        # the reviewer backend adapter will be implemented separately.
        #
        # 042: The "closing_review" stage shares the same code path — its only
        # difference is the persona instructions injected by the coordinare.
        # The closer posts a verdict and, on approval, resolves every open
        # thread so the PR can clear the "all comments resolved" merge gate.
        if perf.role in ("reviewing", "closing_review"):
            review_raw = backend_status.output or ""
            if not review_raw.strip():
                perf.state = "error"
                perf.error_reason = "Backend produced empty review output"
                return PerformerResponse(
                    status="error", session_id=perf.session_id,
                    reason="Backend produced empty review output",
                )
            review_output = _extract_json(review_raw) if isinstance(review_raw, str) else review_raw
            if not isinstance(review_output, dict):
                perf.state = "error"
                perf.error_reason = "Backend review output could not be parsed as JSON object"
                log.warning("reviewer.invalid_output", output_preview=review_raw[:200] if review_raw else "")
                return PerformerResponse(
                    status="error", session_id=perf.session_id,
                    reason="Backend review output could not be parsed as JSON object",
                )

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
            full_body = f"**Bot Review: {verdict}**\n\n{review_body}"
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
            )

        # 022: Security performer path — analyse findings, post advisories, pass or fail.
        if perf.role == "security":
            sec_raw = backend_status.output or ""
            if not sec_raw.strip():
                perf.state = "error"
                perf.error_reason = "Backend produced empty security output"
                return PerformerResponse(
                    status="error", session_id=perf.session_id,
                    reason="Backend produced empty security output",
                )
            sec_output = _extract_json(sec_raw) if isinstance(sec_raw, str) else sec_raw
            if sec_output is None:
                perf.state = "error"
                perf.error_reason = "Backend produced invalid JSON security output"
                return PerformerResponse(
                    status="error", session_id=perf.session_id,
                    reason="Backend produced invalid JSON security output",
                )
            if not isinstance(sec_output, dict):
                perf.state = "error"
                perf.error_reason = "Backend security output is not a JSON object"
                return PerformerResponse(
                    status="error", session_id=perf.session_id,
                    reason="Backend security output is not a JSON object",
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
                for finding in advisory:
                    cat = finding.get("category", "unknown")
                    sev = finding.get("severity", "")
                    desc = finding.get("description", "")
                    body = f"[Advisory - Security] **{cat}** ({sev})\n\n{desc}"
                    try:
                        await post_pr_comment(owner, repo, pr_number, body=body, token=token)
                    except Exception as exc:
                        log.warning("advisory_comment_failed", category=cat, error=str(exc), exc_info=True)

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

        # 023: QA performer path — validate acceptance criteria, commit new tests, pass or fail.
        if perf.role == "qa":
            qa_raw = backend_status.output or ""
            if not qa_raw.strip():
                perf.state = "error"
                perf.error_reason = "Backend produced empty QA output"
                return PerformerResponse(
                    status="error", session_id=perf.session_id,
                    reason="Backend produced empty QA output",
                )
            qa_output = _extract_json(qa_raw) if isinstance(qa_raw, str) else qa_raw
            if qa_output is None:
                perf.state = "error"
                perf.error_reason = "Backend produced invalid JSON QA output"
                return PerformerResponse(
                    status="error", session_id=perf.session_id,
                    reason="Backend produced invalid JSON QA output",
                )
            if not isinstance(qa_output, dict):
                perf.state = "error"
                perf.error_reason = "Backend QA output is not a JSON object"
                return PerformerResponse(
                    status="error", session_id=perf.session_id,
                    reason="Backend QA output is not a JSON object",
                )

            # Commit new test files written by the backend (FR-005)
            new_tests = qa_output.get("new_test_files", [])
            if isinstance(new_tests, list):
                for tf in new_tests:
                    if isinstance(tf, dict) and tf.get("path") and "content" in tf:
                        try:
                            await commit_file(perf.stand, tf["path"], tf["content"],
                                              "test: add QA acceptance criterion tests")
                            perf.qa_new_tests.append(tf["path"])
                        except Exception as exc:
                            log.error("qa_test_commit_failed", path=tf.get("path"), error=str(exc))
                            perf.state = "error"
                            perf.error_reason = f"Failed to commit new test file {tf.get('path')}: {exc}"
                            return PerformerResponse(
                                status="error", session_id=perf.session_id,
                                reason=perf.error_reason,
                            )

            # Commit QA report to the architecture folder
            folder = _doc_folder(perf.score)
            qa_report_content = f"# QA Report: {perf.score.title}\n\n"
            qa_passed_flag = qa_output.get("passed", True)
            criteria_checked = qa_output.get("criteria_checked", 0)
            criteria_passed = qa_output.get("criteria_passed", 0)
            qa_report_content += f"**Result: {'PASSED' if qa_passed_flag else 'FAILED'}**\n\n"
            qa_report_content += f"- Criteria checked: {criteria_checked}\n"
            qa_report_content += f"- Criteria passed: {criteria_passed}\n"
            if perf.qa_new_tests:
                qa_report_content += f"- New tests added: {len(perf.qa_new_tests)}\n"
                for tp in perf.qa_new_tests:
                    qa_report_content += f"  - `{tp}`\n"
            qa_failures = qa_output.get("failures", [])
            if isinstance(qa_failures, list) and qa_failures:
                qa_report_content += "\n## Failures\n\n"
                for f in qa_failures:
                    if isinstance(f, dict):
                        criterion = f.get("criterion", "")
                        expected = f.get("expected", "")
                        actual = f.get("actual", "")
                        qa_report_content += f"- **{criterion}**\n  Expected: {expected}\n  Actual: {actual}\n\n"
            try:
                issue_num = perf.score.issue_number
                await commit_file(
                    perf.stand, f"{folder}/qa.md", qa_report_content,
                    f"chore: add QA report for #{issue_num}" if issue_num else "chore: add QA report",
                )
            except Exception as exc:
                log.warning("qa.commit_report_failed", error=str(exc))

            # Check for environment failure before acceptance criteria
            env_error = qa_output.get("environment_error")
            if env_error:
                perf.state = "blocked"
                perf.open_questions = [str(env_error)]
                return PerformerResponse(
                    status="blocked",
                    session_id=perf.session_id,
                    questions=[str(env_error)],
                )

            # Check for failures
            raw_failures = qa_output.get("failures", [])
            failures = [f for f in (raw_failures if isinstance(raw_failures, list) else []) if isinstance(f, dict)]

            if not failures:
                perf.state = "qa_passed"
                perf.qa_report = {
                    "criteria_checked": qa_output.get("criteria_checked", 0),
                    "criteria_passed": qa_output.get("criteria_passed", 0),
                    "new_tests_added": len(perf.qa_new_tests),
                }
                return PerformerResponse(
                    status="qa_passed",
                    session_id=perf.session_id,
                    report=perf.qa_report,
                )

            # Failures exist
            max_cycles = settings.QA_MAX_CYCLES if settings else 3
            perf.qa_cycle += 1
            if perf.qa_cycle >= max_cycles:
                summary = f"QA: {len(failures)} acceptance criterion failure(s) after {perf.qa_cycle} fix attempt(s)"
                perf.state = "blocked"
                perf.open_questions = [summary]
                return PerformerResponse(
                    status="blocked",
                    session_id=perf.session_id,
                    questions=[summary],
                )
            perf.qa_failures = [f for f in failures if isinstance(f, dict)]
            perf.state = "qa_failed"
            return PerformerResponse(
                status="qa_failed",
                session_id=perf.session_id,
                failures=perf.qa_failures,
            )

        # 024: Tech writer path — commit documentation files, return docs_committed.
        if perf.role == "documenting":
            docs_raw = backend_status.output or ""
            if not docs_raw.strip():
                # Empty diff or config-only changes — return docs_committed with empty list (FR-010)
                perf.state = "docs_committed"
                perf.docs_files_modified = []
                return PerformerResponse(
                    status="docs_committed",
                    session_id=perf.session_id,
                    files_modified=[],
                )
            docs_output = _extract_json(docs_raw) if isinstance(docs_raw, str) else docs_raw
            if docs_output is None:
                perf.state = "error"
                perf.error_reason = "Backend produced invalid JSON docs output"
                return PerformerResponse(
                    status="error", session_id=perf.session_id,
                    reason="Backend produced invalid JSON docs output",
                )
            if not isinstance(docs_output, dict):
                perf.state = "error"
                perf.error_reason = "Backend docs output is not a JSON object"
                return PerformerResponse(
                    status="error", session_id=perf.session_id,
                    reason="Backend docs output is not a JSON object",
                )

            # 044: Batch-commit all documentation files in a single commit
            # instead of per-file commits (which produced 14+ "docs: update
            # documentation" commits on PR #94).
            doc_files = docs_output.get("files", [])
            if not isinstance(doc_files, list):
                log.warning("docs.files_not_a_list", files_type=type(doc_files).__name__)
                doc_files = []
            valid_files = [
                df for df in doc_files
                if isinstance(df, dict) and df.get("path") and isinstance(df.get("content"), str)
            ]
            if valid_files:
                issue_num = perf.score.issue_number
                batch_msg = (
                    f"docs(#{issue_num}): update wiki and card documentation"
                    if issue_num
                    else "docs: update wiki and card documentation"
                )
                try:
                    committed = await commit_files(perf.stand, valid_files, batch_msg)
                    perf.docs_files_modified.extend(committed)
                except Exception as exc:
                    log.error("docs_batch_commit_failed", error=str(exc))
                    perf.state = "error"
                    perf.error_reason = f"Failed to batch-commit doc files: {exc}"
                    return PerformerResponse(
                        status="error", session_id=perf.session_id,
                        reason=perf.error_reason,
                    )

            perf.state = "docs_committed"
            return PerformerResponse(
                status="docs_committed",
                session_id=perf.session_id,
                files_modified=perf.docs_files_modified,
            )

        # 043: Run lint before pushing — catch CI violations at the source
        # rather than discovering them post-push when the PR is already in review.
        ci_ok, ci_error = await _run_ci_check(perf.stand.path, label=perf.role)
        if not ci_ok:
            log.warning("pre_push_ci_failed", role=perf.role, error_preview=ci_error[:200])
            perf.state = "changes_requested"
            perf.review_comments = [{"body": f"Lint failed before push:\n{ci_error[:500]}"}]
            return PerformerResponse(
                status="changes_requested",
                session_id=perf.session_id,
                comments=[{"body": f"Lint failed before push:\n{ci_error[:500]}"}],
            )

        # Default path: push branch and open PR
        owner, repo = perf.score.owner_repo
        await push_branch(perf.stand, perf.score)
        pr_url, pr_node_id = await create_pull_request(
            owner, repo, perf.score, perf.stand.branch, perf.score.effective_github_token
        )
        perf.pr_url = pr_url
        perf.pr_node_id = pr_node_id
        perf.pr_head_sha = await get_head_sha(perf.stand)

        # Only resolve review threads if the implementer actually pushed new
        # commits that address the feedback.  Never resolve threads without
        # corresponding code changes — that hides unresolved issues.
        # Note: thread resolution is intentionally removed.  Human reviewers
        # should verify fixes and resolve their own threads.

        perf.state = "waiting_for_checks"
        return PerformerResponse(
            status="working",
            session_id=perf.session_id,
            progress="Waiting for CI checks...",
        )

    if backend_status.state == "blocked":
        perf.state = "blocked"
        perf.open_questions = backend_status.questions
        return PerformerResponse(
            status="blocked",
            session_id=perf.session_id,
            questions=backend_status.questions,
        )

    if backend_status.state == "error":
        perf.state = "error"
        perf.error_reason = backend_status.error_reason
        return PerformerResponse(
            status="error",
            session_id=perf.session_id,
            reason=backend_status.error_reason,
        )

    # working — attach metrics and drain buffered events
    metrics = collect_metrics(perf.backend, backend_status)
    events = [e.model_dump() for e in perf.backend.drain_events()]
    return PerformerResponse(
        status="working",
        session_id=perf.session_id,
        progress=backend_status.progress,
        metrics=metrics,
        events=events,
    )


async def handle_relay_feedback(
    msg: PerformerMessage,
    perf: Performance | None,
) -> PerformerResponse:
    """Deliver feedback to the running backend."""
    if perf is None or msg.session_id != perf.session_id:
        return PerformerResponse(
            status="session_expired",
            session_id=msg.session_id,
            reason="No active performance with that session ID",
        )
    # Coordinare sends {"pr_url": "...", "comments": [{"body": "..."}, ...]}.
    # Accept that shape as well as a plain {"feedback": "..."} string for tests.
    payload = msg.payload
    feedback: str = payload.get("feedback", "")
    if not feedback:
        parts: list[str] = []
        pr_url = payload.get("pr_url", "")
        if pr_url:
            parts.append(f"PR: {pr_url}")
        for item in payload.get("comments", []):
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                body = item.get("body") or item.get("comment", "")
                if body:
                    parts.append(str(body))
        feedback = "\n\n".join(parts)
    await perf.backend.relay_feedback(feedback)
    return PerformerResponse(status="acknowledged", session_id=perf.session_id)


# ---------------------------------------------------------------------------
# Metrics collection (US3)
# ---------------------------------------------------------------------------

def collect_metrics(
    backend: BackendAdapter,
    status: BackendStatus | None = None,
) -> PerformerMetrics:
    """Collect best-effort process tree metrics.

    ``status`` may be a pre-fetched ``BackendStatus`` to avoid a second
    ``get_status()`` call when the caller already has one.
    """
    pid = os.getpid()
    child_pids: list[int] = []
    memory_bytes: int | None = None
    cpu_percent: float | None = None
    tokens_processed: int | None = None

    try:
        p = _self_process or psutil.Process(pid)
        children = p.children(recursive=True)
        child_pids = [c.pid for c in children]

        rss = p.memory_info().rss
        for child in children:  # pragma: no cover
            try:  # pragma: no cover
                rss += child.memory_info().rss  # pragma: no cover
            except psutil.NoSuchProcess:  # pragma: no cover
                pass  # pragma: no cover
        memory_bytes = rss

        cpu_percent = p.cpu_percent(interval=None)
    except psutil.NoSuchProcess:  # pragma: no cover
        pass

    try:
        bs: BackendStatus = status if status is not None else backend.get_status()
        tokens_processed = bs.tokens_processed
    except Exception:  # pragma: no cover
        pass

    return PerformerMetrics(
        pid=pid,
        child_pids=child_pids,
        memory_bytes=memory_bytes,
        cpu_percent=cpu_percent,
        tokens_processed=tokens_processed,
    )


# ---------------------------------------------------------------------------
# Message loop
# ---------------------------------------------------------------------------

async def run_loop() -> None:
    """Main message loop — read JSON from stdin, write JSON to stdout."""
    # stdout is reserved for the JSON wire protocol — all logs must go to stderr.
    structlog.configure(
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
    )
    _init_metrics()
    settings = get_settings()
    perf: Performance | None = None

    reader = asyncio.StreamReader()
    protocol = asyncio.StreamReaderProtocol(reader)
    loop = asyncio.get_running_loop()
    await loop.connect_read_pipe(lambda: protocol, sys.stdin)

    while True:
        # Watchdog: once a session is active, cap how long we wait for the next
        # message to the remaining AGENT_TIMEOUT budget.  If the coordinare stops
        # polling the performer self-terminates rather than leaking the backend.
        try:
            if perf is not None:
                elapsed = (datetime.now(UTC) - perf.started_at).total_seconds()
                remaining = settings.AGENT_TIMEOUT - elapsed
                if remaining <= 0:
                    raise asyncio.TimeoutError
                raw = await asyncio.wait_for(reader.readline(), timeout=remaining)
            else:
                raw = await reader.readline()
        except asyncio.TimeoutError:
            log.warning("session watchdog fired — stopping backend",
                        session_id=perf.session_id if perf else "")
            if perf is not None and perf.state not in ("pr_opened", "plan_committed", "approved", "changes_requested", "security_passed", "security_failed", "qa_passed", "qa_failed", "docs_committed", "error"):
                perf.state = "error"
                perf.error_reason = (
                    f"watchdog: session exceeded {settings.AGENT_TIMEOUT:.0f}s"
                )
                try:
                    await perf.backend.stop()
                except Exception:  # pragma: no cover
                    pass
            break
        except Exception:  # pragma: no cover
            break
        if not raw:
            break

        line = raw.decode(errors="replace").strip()
        if not line:
            continue

        try:
            msg = PerformerMessage.model_validate_json(line)
        except Exception as exc:
            # Log exception type only — exc text may contain sensitive input values
            # (e.g. github_token surfaced by pydantic ValidationError).
            log.error("invalid message", exc_type=type(exc).__name__)
            resp = PerformerResponse(status="error", reason=f"invalid message: {type(exc).__name__}")
            _write_response(resp)
            continue

        try:
            if msg.action == "health":
                resp = handle_health(settings)

            elif msg.action == "dispatch":
                # Note: no per-dispatch timeout here — AGENT_TIMEOUT is the
                # end-to-end session budget enforced by the watchdog above.
                # A separate clone/setup timeout lives inside _run_git().
                try:
                    resp, perf = await handle_dispatch(msg, settings)
                except WorkspaceSetupError as exc:
                    resp = PerformerResponse(
                        status="error",
                        session_id=msg.session_id,
                        reason=str(exc),
                    )
                except ValidationError as exc:
                    # Report field names only — never echo payload values which
                    # may contain secrets such as github_token.
                    fields = ", ".join(
                        sorted({str(e["loc"][0]) for e in exc.errors() if e["loc"]})
                    )
                    resp = PerformerResponse(
                        status="error",
                        session_id=msg.session_id,
                        reason=f"invalid dispatch payload — bad fields: {fields}",
                    )
                except Exception as exc:
                    # Log exception type only — raw exc string may contain tokens
                    # or other sensitive context from git/http operations.
                    log.error("dispatch error", exc_type=type(exc).__name__)
                    resp = PerformerResponse(
                        status="error",
                        session_id=msg.session_id,
                        reason=f"dispatch failed: {type(exc).__name__}",
                    )

            elif msg.action == "status":
                # Refresh GitHub token if the coordinare sent a fresh one
                refreshed_token = msg.payload.get("github_token")
                if refreshed_token and perf is not None and perf.score is not None:
                    perf.score.github_token = refreshed_token
                    log.debug("token_refreshed", session_id=perf.session_id)
                try:
                    resp = await handle_status(msg, perf, settings)
                except (BranchConflictError, GitHubAPIError, WorkspaceSetupError) as exc:
                    if perf:
                        perf.state = "error"
                        perf.error_reason = str(exc)
                    resp = PerformerResponse(
                        status="error",
                        session_id=msg.session_id,
                        reason=str(exc),
                    )

            elif msg.action == "relay_feedback":
                resp = await handle_relay_feedback(msg, perf)

            else:  # pragma: no cover — Literal[action] makes this unreachable
                resp = PerformerResponse(
                    status="error",
                    session_id=msg.session_id,
                    reason=f"unknown action: {msg.action}",
                )

        except Exception as exc:  # pragma: no cover
            log.exception("unhandled error in run_loop")
            resp = PerformerResponse(
                status="error",
                session_id=msg.session_id,
                reason=f"internal error: {type(exc).__name__}: {exc}",
            )

        _write_response(resp)

        # Terminal states exit the loop
        if resp.status in ("pr_opened", "plan_committed", "approved", "changes_requested", "security_passed", "security_failed", "qa_passed", "qa_failed", "docs_committed", "error") and msg.action != "health":
            break
        # session_expired with no active session means nothing will ever start
        if resp.status == "session_expired" and perf is None:
            break

    # Cleanup regardless of outcome
    if perf is not None:
        try:
            await perf.backend.stop()
        except Exception:  # pragma: no cover
            pass
        cleanup_stand(perf.stand)


def _write_response(resp: PerformerResponse) -> None:  # pragma: no cover
    sys.stdout.write(resp.model_dump_json(exclude_none=True) + "\n")
    sys.stdout.flush()


def main() -> None:  # pragma: no cover
    asyncio.run(run_loop())
