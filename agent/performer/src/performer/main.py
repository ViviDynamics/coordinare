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
from urllib.parse import urlparse

import psutil
import structlog
from pydantic import ValidationError

from performer.backends import UnsupportedBackendError, get_backend
from performer.backends.base import BackendAdapter, BackendStatus
from performer.config import Settings, get_settings
from performer.github import GitHubAPIError, create_pull_request, get_check_runs, post_issue_comment, post_pr_comment, post_pull_request_review, resolve_pr_review_threads, summarise_check_runs
from performer.models import Performance, Score, Stand, _redact_secrets
from performer.protocol import PerformerMessage, PerformerMetrics, PerformerResponse
from performer.workspace import commit_file
from performer.workspace import (
    BranchConflictError,
    WorkspaceSetupError,
    _git_credential_vars,
    cleanup_stand,
    clone_repository,
    commit_files,
    get_head_sha,
    push_branch,
    run_command,
)

log = structlog.get_logger(__name__)

_CODE_FENCE_RE = re.compile(r"```(?:json)?\s*\n?(.*?)\n?\s*```", re.DOTALL | re.IGNORECASE)
_FORMAT_ERROR_PREFIX = "BACKEND_FORMAT_ERROR:"
_VISUAL_TASK_KEYWORDS = (
    "ui",
    "ux",
    "dashboard",
    "screen",
    "page",
    "view",
    "visual",
    "layout",
    "css",
    "style",
    "frontend",
    "button",
    "component",
    "render",
    "screenshot",
    "gif",
)
_VISUAL_TASK_PATTERN = re.compile(
    r"\b(?:"
    + "|".join(re.escape(keyword) for keyword in _VISUAL_TASK_KEYWORDS)
    + r")\b",
    re.IGNORECASE,
)
_ROLE_ALIASES: dict[str, str] = {
    "assessor": "assessing",
    "reviewer": "reviewing",
    "closer": "closing_review",
    "tech_writer": "documenting",
}
_JSON_ROLE_REQUIRED_KEYS: dict[str, tuple[str, ...]] = {
    "assessing": ("sufficient", "questions"),
    "reviewing": ("approved", "comments", "body"),
    "closing_review": ("approved", "comments", "body"),
    "security": ("passed", "findings"),
    "qa": ("passed", "failures", "verification_steps"),
    "documenting": ("files",),
}


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


def _extract_pr_number(pr_url: str) -> int:
    """Extract PR number from a GitHub PR URL, or 0 when unavailable."""
    raw = (pr_url or "").strip()
    if not raw:
        return 0
    path = urlparse(raw).path or raw
    match = re.search(r"/pull/(\d+)(?:/|$)", path)
    if match is None:
        return 0
    try:
        return int(match.group(1))
    except (ValueError, IndexError):
        return 0


def _normalise_steps(value: object) -> list[str]:
    """Normalize backend-provided step lists into non-empty strings."""
    if not isinstance(value, list):
        return []
    out: list[str] = []
    for item in value:
        if isinstance(item, str):
            step = item.strip()
            if step:
                out.append(step)
            continue
        if isinstance(item, dict):
            step = str(item.get("step", item.get("text", ""))).strip()
            if step:
                out.append(step)
    # Preserve order while removing duplicates.
    deduped: list[str] = []
    seen: set[str] = set()
    for step in out:
        if step in seen:
            continue
        seen.add(step)
        deduped.append(step)
    return deduped


def _normalise_visual_evidence(value: object) -> list[dict[str, str]]:
    """Normalize visual evidence items into a consistent dict shape."""
    if not isinstance(value, list):
        return []
    out: list[dict[str, str]] = []
    for item in value:
        if isinstance(item, str):
            loc = item.strip()
            if loc:
                out.append({
                    "label": "Evidence",
                    "kind": "artifact",
                    "path_or_url": loc,
                    "note": "",
                })
            continue
        if not isinstance(item, dict):
            continue
        label = str(item.get("label", "Evidence")).strip() or "Evidence"
        kind = str(item.get("kind", "artifact")).strip() or "artifact"
        loc = str(item.get("path_or_url", item.get("url", item.get("path", "")))).strip()
        note = str(item.get("note", item.get("description", ""))).strip()
        if not loc and not note:
            continue
        out.append({
            "label": label,
            "kind": kind,
            "path_or_url": loc,
            "note": note,
        })
    return out[:20]


def _looks_like_url(value: str) -> bool:
    """Return True for HTTP(S) URLs."""
    return value.startswith("http://") or value.startswith("https://")


def _qa_visual_validation_required(
    *,
    score: Score,
    qa_output: dict,
    demo_setup_steps: list[str],
    visual_capture_blockers: list[str],
    visual_evidence: list[dict[str, str]],
) -> bool:
    """Determine whether this QA run must include visual artifacts."""
    raw = qa_output.get("visual_validation_required")
    if isinstance(raw, bool):
        return raw

    if demo_setup_steps or visual_capture_blockers or visual_evidence:
        return True

    content = "\n".join(
        [
            score.title,
            score.description,
            "\n".join(score.acceptance_criteria or []),
        ],
    )
    return _VISUAL_TASK_PATTERN.search(content) is not None


def _qa_visual_evidence_failures(
    *,
    required: bool,
    stand_path: Path,
    visual_capture_commands: list[str],
    visual_capture_blockers: list[str],
    visual_evidence: list[dict[str, str]],
) -> list[dict[str, str]]:
    """Return synthetic QA failures for missing/invalid visual evidence."""
    if not required:
        return []

    failures: list[dict[str, str]] = []
    if not visual_evidence:
        attempts_text = " | ".join(visual_capture_commands[:3]) if visual_capture_commands else "none"
        blockers_text = " | ".join(visual_capture_blockers[:3]) if visual_capture_blockers else "none"
        failures.append(
            {
                "criterion": "Visual evidence artifacts captured",
                "expected": "At least one screenshot/GIF/video/artifact generated from this QA run.",
                "actual": (
                    f"No artifacts captured. Commands attempted: {attempts_text}. "
                    f"Blockers: {blockers_text}."
                ),
                "test": "visual-capture",
            },
        )
        return failures

    has_artifact_location = False
    missing_location_count = 0
    for ev in visual_evidence:
        loc = str(ev.get("path_or_url", "")).strip()
        if not loc:
            missing_location_count += 1
            continue
        has_artifact_location = True
        if _looks_like_url(loc):
            continue
        candidate = Path(loc)
        artifact_path = candidate if candidate.is_absolute() else (stand_path / candidate)
        if artifact_path.exists():
            continue
        failures.append(
            {
                "criterion": "Visual evidence artifact path exists",
                "expected": "Each local visual artifact path points to an existing file.",
                "actual": f"Artifact path does not exist: {loc}",
                "test": "visual-capture",
            },
        )
    if missing_location_count:
        failures.append(
            {
                "criterion": "Visual evidence entries include artifact locations",
                "expected": "Each visual evidence entry includes a non-empty path_or_url.",
                "actual": (
                    f"{missing_location_count} visual evidence entr"
                    f"{'y is' if missing_location_count == 1 else 'ies are'} missing path_or_url."
                ),
                "test": "visual-capture",
            },
        )
    if not has_artifact_location:
        failures.append(
            {
                "criterion": "Visual evidence artifacts captured",
                "expected": "At least one visual evidence entry includes a screenshot/GIF/video path_or_url.",
                "actual": "Only note-only visual evidence entries were provided.",
                "test": "visual-capture",
            },
        )
    return failures


def _is_bug_like_ticket(score: Score) -> bool:
    """Heuristic: detect bug-fix tickets for step-label wording."""
    text = f"{score.title}\n{score.description}".lower()
    bug_keywords = ("bug", "fix", "regression", "broken", "error", "defect", "crash", "issue")
    return any(word in text for word in bug_keywords)


def _default_verification_steps(score: Score) -> list[str]:
    """Generate fallback verification steps when backend omitted them."""
    criteria = [str(c).strip() for c in (score.acceptance_criteria or []) if str(c).strip()]
    if criteria:
        return [f"Verify acceptance criterion: {criterion}" for criterion in criteria[:10]]
    return [
        "Run the relevant automated tests and confirm they pass.",
        "Manually validate the changed user flow end-to-end and confirm expected behavior.",
    ]


def _build_qa_pr_comment(
    *,
    score: Score,
    passed: bool,
    criteria_checked: int,
    criteria_passed: int,
    failures: list[dict],
    verification_steps: list[str],
    pre_fix_repro_steps: list[str],
    demo_setup_steps: list[str],
    visual_capture_commands: list[str],
    visual_capture_blockers: list[str],
    visual_evidence: list[dict[str, str]],
    environment_error: str,
) -> str:
    """Render a human-facing QA evidence comment for the PR thread."""
    bug_like = _is_bug_like_ticket(score)
    verify_heading = "Fix Verification Steps" if bug_like else "Demo / Verification Steps"
    lines = [
        "## QA Evidence",
        "",
        f"**Result:** {'PASSED' if passed else 'FAILED'}",
        f"- Criteria checked: {criteria_checked}",
        f"- Criteria passed: {criteria_passed}",
    ]

    if environment_error:
        lines += [
            "",
            "### Environment Blocker",
            f"- {environment_error}",
        ]

    if pre_fix_repro_steps:
        lines += ["", "### Known Reproduction Steps (pre-fix context)"]
        lines.extend(f"{idx}. {step}" for idx, step in enumerate(pre_fix_repro_steps, start=1))

    if demo_setup_steps:
        lines += ["", "### Visual Capture Setup Steps"]
        lines.extend(f"{idx}. {step}" for idx, step in enumerate(demo_setup_steps, start=1))

    if visual_capture_commands:
        lines += ["", "### Visual Capture Commands Attempted"]
        lines.extend(f"{idx}. `{cmd}`" for idx, cmd in enumerate(visual_capture_commands, start=1))

    lines += ["", f"### {verify_heading}"]
    lines.extend(f"{idx}. {step}" for idx, step in enumerate(verification_steps, start=1))

    lines += ["", "### Visual Evidence"]
    if visual_evidence:
        for ev in visual_evidence:
            label = ev.get("label", "Evidence")
            kind = ev.get("kind", "artifact")
            loc = ev.get("path_or_url", "")
            note = ev.get("note", "")
            line = f"- **{label}** ({kind})"
            if loc:
                line += f": `{loc}`"
            if note:
                line += f" — {note}"
            lines.append(line)
    else:
        lines.append("- No visual artifacts captured in this QA run.")
        if visual_capture_blockers:
            lines.append("- Capture blockers:")
            lines.extend(f"  - {blocker}" for blocker in visual_capture_blockers)

    if failures:
        lines += ["", "### Remaining Failures"]
        for failure in failures[:10]:
            criterion = str(failure.get("criterion", ""))
            expected = str(failure.get("expected", ""))
            actual = str(failure.get("actual", ""))
            lines.append(f"- **{criterion or 'Unnamed criterion'}** — expected `{expected}`, got `{actual}`")

    return "\n".join(lines)


def _json_required_role(role: str) -> bool:
    """Return True when a role's terminal output must be JSON."""
    canonical = _ROLE_ALIASES.get(role, role)
    return canonical in _JSON_ROLE_REQUIRED_KEYS


def _json_recovery_prompt(
    *,
    role: str,
    stage_label: str,
    failure_reason: str,
    output_preview: str,
) -> str:
    """Build a strict JSON-only repair prompt for retry turns."""
    canonical = _ROLE_ALIASES.get(role, role)
    required = _JSON_ROLE_REQUIRED_KEYS.get(canonical, ())
    keys_hint = ", ".join(required) if required else "(role schema)"
    return (
        f"Your previous {stage_label} response {failure_reason}. "
        "Return ONLY a valid JSON object now. "
        "Do NOT include markdown, prose, or code fences.\n\n"
        f"Required top-level keys: {keys_hint}\n"
        f"Previous output preview: {output_preview}"
    )


async def _handle_backend_parse_failure(
    perf: Performance,
    raw: str,
    stage_label: str,
    settings: Settings | None,
    failure_reason: str,
) -> PerformerResponse:
    """Retry the backend up to ``settings.BACKEND_PARSE_RETRIES`` times, or bubble
    up the error with an output preview.

    The caller must ``return`` the result of this helper directly.  When a retry
    is scheduled the response is ``status="working"`` (so the coordinare keeps
    polling while the backend re-runs); when the budget is exhausted it's
    ``status="error"`` with a reason that includes up to 300 chars of the last
    failed output.  (045)
    """
    max_retries = settings.BACKEND_PARSE_RETRIES if settings else get_settings().BACKEND_PARSE_RETRIES
    is_json_contract_role = _json_required_role(perf.role)
    # Copilot round 4: ``raw`` is untrusted backend output; run it through
    # the existing secret-pattern redactor before logging it or embedding
    # it in perf.error_reason (which propagates to coordinare-side logs,
    # Slack/email notifications, and persisted snapshots).  Coordinare-side
    # redaction is key-based only, so tokens inside arbitrary text values
    # would otherwise survive into operator-facing surfaces.
    redacted_short = _redact_secrets(raw[:200]) if raw else ""
    if perf.parse_retry_count < max_retries:
        perf.parse_retry_count += 1
        log.warning(
            "backend.invalid_output.retrying",
            stage=stage_label,
            attempt=perf.parse_retry_count,
            max_retries=max_retries,
            failure_reason=failure_reason,
            output_preview=redacted_short,
        )
        recovery_attempted = False
        if is_json_contract_role:
            try:
                await perf.backend.relay_feedback(
                    _json_recovery_prompt(
                        role=perf.role,
                        stage_label=stage_label,
                        failure_reason=failure_reason,
                        output_preview=redacted_short or "<empty>",
                    )
                )
                recovery_attempted = True
            except Exception as exc:
                log.warning(
                    "backend.parse_retry_relay_failed",
                    stage=stage_label,
                    role=perf.role,
                    error=str(exc),
                )
        if not recovery_attempted:
            # Copilot round 5: backends like OpenCodeAdapter launch long-lived
            # ``opencode serve`` processes and start() doesn't tear down previous
            # instances.  Stop the current backend (best-effort) before launching
            # a fresh run so repeated parse failures don't leak subprocesses or
            # background tasks.  Swallow stop() failures — the backend may be
            # already dead, and start() is what matters.
            try:
                await perf.backend.stop()
            except Exception as exc:
                log.warning(
                    "backend.parse_retry_stop_failed",
                    stage=stage_label,
                    error=str(exc),
                )
            model_name = perf.score.model or None
            await perf.backend.start(
                perf.stand, perf.score,
                model=model_name,
                effort=perf.score.effort or None,
                temperature=perf.score.temperature,
                max_tokens=perf.score.max_tokens,
            )
        return PerformerResponse(
            status="working",
            session_id=perf.session_id,
            progress=(
                f"Backend {stage_label} output {failure_reason}; "
                f"{'requested JSON repair' if recovery_attempted else 'restarting backend'} — "
                f"retrying ({perf.parse_retry_count}/{max_retries})"
            ),
        )
    redacted_long = _redact_secrets((raw or "")[:300])
    perf.state = "error"
    prefix = f"{_FORMAT_ERROR_PREFIX} " if is_json_contract_role else ""
    perf.error_reason = (
        f"{prefix}Backend {stage_label} output {failure_reason} after "
        f"{perf.parse_retry_count + 1} attempts. Last output: {redacted_long!r}"
    )
    return PerformerResponse(
        status="error",
        session_id=perf.session_id,
        reason=perf.error_reason,
    )


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
    role = _ROLE_ALIASES.get(score.role, score.role)
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
        await backend.start(
            stand, score,
            model=model_name,
            effort=score.effort or None,
            temperature=score.temperature,
            max_tokens=score.max_tokens,
        )
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
        model=model_name,
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
                return await _handle_backend_parse_failure(
                    perf, assess_raw, "assessment", settings, "was empty",
                )
            assess_output = _extract_json(assess_raw) if isinstance(assess_raw, str) else assess_raw
            if not isinstance(assess_output, dict):
                return await _handle_backend_parse_failure(
                    perf, assess_raw, "assessment", settings,
                    "could not be parsed as a JSON object",
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
                return await _handle_backend_parse_failure(
                    perf, review_raw, "review", settings, "was empty",
                )
            review_output = _extract_json(review_raw) if isinstance(review_raw, str) else review_raw
            if not isinstance(review_output, dict):
                return await _handle_backend_parse_failure(
                    perf, review_raw, "review", settings,
                    "could not be parsed as a JSON object",
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
                return await _handle_backend_parse_failure(
                    perf, qa_raw, "QA", settings, "was empty",
                )
            qa_output = _extract_json(qa_raw) if isinstance(qa_raw, str) else qa_raw
            if not isinstance(qa_output, dict):
                return await _handle_backend_parse_failure(
                    perf, qa_raw, "QA", settings,
                    "could not be parsed as a JSON object",
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

            verification_steps = _normalise_steps(qa_output.get("verification_steps", []))
            demo_steps = _normalise_steps(qa_output.get("demo_steps", []))
            for step in demo_steps:
                if step not in verification_steps:
                    verification_steps.append(step)
            if not verification_steps:
                verification_steps = _default_verification_steps(perf.score)
            pre_fix_repro_steps = _normalise_steps(
                qa_output.get("pre_fix_repro_steps", qa_output.get("reproduction_steps", [])),
            )
            demo_setup_steps = _normalise_steps(qa_output.get("demo_setup_steps", []))
            visual_capture_commands = _normalise_steps(qa_output.get("visual_capture_commands", []))
            visual_capture_blockers = _normalise_steps(qa_output.get("visual_capture_blockers", []))
            visual_evidence = _normalise_visual_evidence(qa_output.get("visual_evidence", []))
            visual_validation_required = _qa_visual_validation_required(
                score=perf.score,
                qa_output=qa_output,
                demo_setup_steps=demo_setup_steps,
                visual_capture_blockers=visual_capture_blockers,
                visual_evidence=visual_evidence,
            )

            # Check for failures/env blockers before summarising and posting evidence.
            raw_failures = qa_output.get("failures", [])
            failures = [f for f in (raw_failures if isinstance(raw_failures, list) else []) if isinstance(f, dict)]
            env_error = str(qa_output.get("environment_error", "")).strip()
            failures.extend(
                _qa_visual_evidence_failures(
                    required=visual_validation_required,
                    stand_path=perf.stand.path,
                    visual_capture_commands=visual_capture_commands,
                    visual_capture_blockers=visual_capture_blockers,
                    visual_evidence=visual_evidence,
                ),
            )
            latest_main_sha = perf.score.latest_main_sha.strip()
            qa_freshness_check: dict = {}
            if not latest_main_sha:
                # Coordinare did not supply latest_main_sha — cannot verify freshness.
                # Record the indeterminate state but do NOT add a failure: QA can still
                # pass when coordinare hasn't yet populated last_known_main_sha (e.g. first
                # cycle after startup).  This is a non-blocking indeterminate.
                qa_freshness_check = {
                    "latest_main_sha": None,
                    "branch_head_sha": None,
                    "up_to_date": None,
                    "detail": "freshness_check_indeterminate",
                }
            else:
                try:
                    branch_head: str | None = None
                    try:
                        branch_head = await get_head_sha(perf.stand)
                    except Exception as _head_exc:
                        log.debug("qa.branch_head_fetch_failed", error=str(_head_exc))
                    _proc = await asyncio.create_subprocess_exec(
                        "git", "merge-base", "--is-ancestor", latest_main_sha, "HEAD",
                        cwd=str(perf.stand.path),
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    try:
                        _rc = await asyncio.wait_for(_proc.wait(), timeout=30)
                    except asyncio.TimeoutError:
                        _proc.kill()
                        await _proc.wait()
                        raise
                    if _rc == 0:
                        qa_freshness_check = {
                            "latest_main_sha": latest_main_sha,
                            "branch_head_sha": branch_head,
                            "up_to_date": True,
                            "detail": "branch includes latest main (git merge-base confirmed)",
                        }
                    elif _rc == 1:
                        # Exit code 1: SHA is not an ancestor (branch is behind main).
                        qa_freshness_check = {
                            "latest_main_sha": latest_main_sha,
                            "branch_head_sha": branch_head,
                            "up_to_date": False,
                            "detail": f"{latest_main_sha[:8]} is not an ancestor of HEAD",
                        }
                        failures.append({
                            "file": None,
                            "line": None,
                            "message": "Branch is behind latest main — rebase required before QA can pass",
                            "type": "freshness",
                            "criterion": "Branch includes latest main",
                            "expected": f"Branch rebased onto {latest_main_sha[:8]}",
                            "actual": "Branch is behind latest main — rebase required",
                        })
                    else:
                        # Exit codes > 1 indicate a git/environment error, not a
                        # definitive "behind main" result — treat as indeterminate.
                        raise RuntimeError(f"git merge-base exited with code {_rc}")
                except Exception as _exc:
                    qa_freshness_check = {
                        "latest_main_sha": None,
                        "branch_head_sha": None,
                        "up_to_date": None,
                        "detail": "freshness_check_indeterminate",
                    }
                    failures.append({
                        "file": None,
                        "line": None,
                        "message": "Branch freshness could not be verified — environment/git failure",
                        "type": "freshness_indeterminate",
                        "criterion": "Branch freshness check",
                        "expected": "Freshness check succeeds",
                        "actual": "Branch freshness could not be verified — environment/git failure",
                    })
                    log.warning("qa.freshness_check_failed", error=str(_exc))

            qa_passed_flag = (not failures) and (not env_error)

            # Commit QA report to the architecture folder
            folder = _doc_folder(perf.score)
            qa_report_content = f"# QA Report: {perf.score.title}\n\n"
            criteria_checked = qa_output.get("criteria_checked", 0)
            criteria_passed = qa_output.get("criteria_passed", 0)
            qa_report_content += f"**Result: {'PASSED' if qa_passed_flag else 'FAILED'}**\n\n"
            qa_report_content += f"- Criteria checked: {criteria_checked}\n"
            qa_report_content += f"- Criteria passed: {criteria_passed}\n"
            if perf.qa_new_tests:
                qa_report_content += f"- New tests added: {len(perf.qa_new_tests)}\n"
                for tp in perf.qa_new_tests:
                    qa_report_content += f"  - `{tp}`\n"
            if pre_fix_repro_steps:
                qa_report_content += "\n## Known Reproduction Steps (pre-fix context)\n\n"
                for idx, step in enumerate(pre_fix_repro_steps, start=1):
                    qa_report_content += f"{idx}. {step}\n"
            if demo_setup_steps:
                qa_report_content += "\n## Visual Capture Setup Steps\n\n"
                for idx, step in enumerate(demo_setup_steps, start=1):
                    qa_report_content += f"{idx}. {step}\n"
            if visual_capture_commands:
                qa_report_content += "\n## Visual Capture Commands Attempted\n\n"
                for idx, command in enumerate(visual_capture_commands, start=1):
                    qa_report_content += f"{idx}. `{command}`\n"
            qa_report_content += "\n## Verification Steps\n\n"
            for idx, step in enumerate(verification_steps, start=1):
                qa_report_content += f"{idx}. {step}\n"
            qa_report_content += "\n## Visual Evidence\n\n"
            if visual_evidence:
                for ev in visual_evidence:
                    label = ev.get("label", "Evidence")
                    kind = ev.get("kind", "artifact")
                    loc = ev.get("path_or_url", "")
                    note = ev.get("note", "")
                    qa_report_content += f"- **{label}** ({kind})"
                    if loc:
                        qa_report_content += f": `{loc}`"
                    if note:
                        qa_report_content += f" — {note}"
                    qa_report_content += "\n"
            else:
                qa_report_content += "- No visual artifacts captured in this QA run.\n"
                if visual_capture_blockers:
                    qa_report_content += "\n### Capture blockers\n\n"
                    for blocker in visual_capture_blockers:
                        qa_report_content += f"- {blocker}\n"
            if failures:
                qa_report_content += "\n## Failures\n\n"
                for f in failures:
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

            qa_comment = _build_qa_pr_comment(
                score=perf.score,
                passed=qa_passed_flag,
                criteria_checked=int(criteria_checked) if isinstance(criteria_checked, int | float) else 0,
                criteria_passed=int(criteria_passed) if isinstance(criteria_passed, int | float) else 0,
                failures=failures,
                verification_steps=verification_steps,
                pre_fix_repro_steps=pre_fix_repro_steps,
                demo_setup_steps=demo_setup_steps,
                visual_capture_commands=visual_capture_commands,
                visual_capture_blockers=visual_capture_blockers,
                visual_evidence=visual_evidence,
                environment_error=env_error,
            )

            # Post QA evidence to the PR so humans can quickly validate behavior.
            pr_number = _extract_pr_number(perf.pr_url)
            if pr_number > 0:
                owner, repo = perf.score.owner_repo
                token = perf.score.effective_github_token
                try:
                    await post_pr_comment(owner, repo, pr_number, body=qa_comment, token=token)
                except Exception as exc:
                    log.warning("qa.evidence_comment_failed", error=str(exc), exc_info=True)
            else:
                log.warning("qa.evidence_comment_skipped_missing_pr_url", pr_url=perf.pr_url)

            issue_number = perf.score.issue_number
            if issue_number > 0:
                owner, repo = perf.score.owner_repo
                token = perf.score.effective_github_token
                try:
                    await post_issue_comment(owner, repo, issue_number, body=qa_comment, token=token)
                except Exception as exc:
                    log.warning(
                        "qa.evidence_issue_comment_failed",
                        issue_number=issue_number,
                        error=str(exc),
                        exc_info=True,
                    )

            # Check for environment failure before acceptance criteria
            if env_error:
                perf.state = "blocked"
                perf.open_questions = [env_error]
                return PerformerResponse(
                    status="blocked",
                    session_id=perf.session_id,
                    questions=[env_error],
                )

            if not failures:
                perf.state = "qa_passed"
                perf.qa_report = {
                    "criteria_checked": qa_output.get("criteria_checked", 0),
                    "criteria_passed": qa_output.get("criteria_passed", 0),
                    "new_tests_added": len(perf.qa_new_tests),
                    "verification_steps": verification_steps,
                    "pre_fix_repro_steps": pre_fix_repro_steps,
                    "demo_setup_steps": demo_setup_steps,
                    "visual_capture_commands": visual_capture_commands,
                    "visual_capture_blockers": visual_capture_blockers,
                    "visual_evidence": visual_evidence,
                    "visual_validation_required": visual_validation_required,
                }
                if qa_freshness_check:
                    perf.qa_report["qa_freshness_check"] = qa_freshness_check
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
            _fail_report: dict | None = {"qa_freshness_check": qa_freshness_check} if qa_freshness_check else None
            return PerformerResponse(
                status="qa_failed",
                session_id=perf.session_id,
                failures=perf.qa_failures,
                report=_fail_report,
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
            if not isinstance(docs_output, dict):
                return await _handle_backend_parse_failure(
                    perf, docs_raw, "docs", settings,
                    "could not be parsed as a JSON object",
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
        if backend_status.stop_reason == "max_tokens":
            return PerformerResponse(
                status="token_limit",
                session_id=perf.session_id,
                reason=backend_status.error_reason,
            )
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
                    perf.stand.git_env = _git_credential_vars(refreshed_token)
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
