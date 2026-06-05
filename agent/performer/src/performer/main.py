"""Performer entrypoint — wire protocol message loop."""
from __future__ import annotations

import asyncio
import json as _json_module
from pathlib import Path
import os
import re
import sys
import traceback
import uuid
from datetime import UTC, datetime
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

if TYPE_CHECKING:
    from performer.server.models import JobInitPayload, JobResult

import psutil
import structlog
from pydantic import ValidationError

from performer.backends import UnsupportedBackendError, get_backend
from performer.backends.base import BackendAdapter, BackendStatus
from performer.cdn_upload import resolve_visual_evidence_urls
from performer.config import Settings, get_settings
from performer.github import GitHubAPIError, create_pull_request, get_check_run_logs, get_check_runs, list_pr_comments, post_issue_comment, post_pr_comment, post_pull_request_review, resolve_pr_review_threads, summarise_check_runs
from performer.io_utils import iter_lines_chunked
from performer.models import DIAGNOSTIC_ROLE, Performance, Score, Stand, _redact_secrets
from performer.protocol import (
    FAILURE_STATUSES,
    TERMINAL_STATUSES,
    PerformerMessage,
    PerformerMetrics,
    PerformerResponse,
)
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


# ---------------------------------------------------------------------------
# 063 Cross-cutting (T026c) — Service inference for env_bootstrap
# ---------------------------------------------------------------------------


DEFAULT_INFERENCE_AGENT_VERSION = "claude-services-v1"
DEFAULT_INFERENCE_MODEL = "claude-sonnet-4-5-20250929"
# Sonnet 4.5 caps output at 64k. The Anthropic API rejects requests where
# max_tokens exceeds the model cap, so the default has to fit the default
# model. Operators can override via COORDINARE_INFERENCE_MAX_TOKENS when they
# point COORDINARE_INFERENCE_MODEL at something with a different cap.
DEFAULT_INFERENCE_MAX_TOKENS = 64_000
# Tool-call ceiling per agent attempt. Wandering models (e.g. Qwen on a large
# Rails repo) can burn the default of 50 without ever emitting submit_manifest.
# Operators can raise this via COORDINARE_INFERENCE_MAX_TOOL_CALLS when the
# repo + model combination needs more headroom.
DEFAULT_INFERENCE_MAX_TOOL_CALLS = 50


async def _run_service_inference(
    stand_path: Path,
    env_cache_path: str,
) -> dict[str, object]:
    """Drop ``services.json`` + start/stop/health scripts into the env-cache.

    Tries the deterministic manual-override path first; if no ``.coordinare/score.json``
    is present, falls back to the LLM agent when ``ANTHROPIC_API_KEY`` is set.

    Returns a dict suitable for splatting onto :class:`PerformerResponse`:
    ``inference_skipped_reason`` / ``inference_agent_version`` /
    ``inference_attempts`` / ``inference_succeeded`` / ``inference_services``.
    """
    if not env_cache_path:
        return {"inference_skipped_reason": "no_env_cache_path"}

    from coordinare_service_inference import (
        InferenceFailed,
        infer_services,
    )
    from coordinare_service_inference.claude_llm_client import (
        ClaudeServiceLLMClient,
    )
    from coordinare_service_inference.manual_override import (
        apply_manual_override,
    )
    from coordinare_service_inference.prompt import render_system_prompt

    output_root = Path(env_cache_path)

    # 1) Manual override path: deterministic, no creds needed.
    try:
        override = apply_manual_override(stand_path, output_root, run_validation=False)
    except Exception as exc:  # pragma: no cover — defensive
        log.warning("service_inference.manual_override_error", error=str(exc))
        override = None

    if override is not None and override.applied:
        services = (
            [s.name for s in override.manifest.services]
            if override.manifest is not None
            else []
        )
        log.info(
            "service_inference.manual_override_applied",
            services=services,
        )
        return {
            "inference_skipped_reason": "manual_override",
            "inference_agent_version": "manual-override",
            "inference_attempts": 1,
            "inference_succeeded": True,
            "inference_services": services,
        }

    # 2) LLM path: provider-selected via COORDINARE_INFERENCE_PROVIDER
    #    (`anthropic` default, or `openai_compat` for LiteLLM/vLLM/Ollama/etc).
    # Empty values are treated as unset. Coordinare forwards these vars via
    # config.yaml ${VAR} placeholders; if a var is unset on the host,
    # os.path.expandvars leaves the literal `${...}` string. The coordinare's
    # performer_lifecycle drops those before docker -e, but treat them as unset
    # here too in case an older coordinare or alternate launch path lets one through.
    def _env(key: str) -> str:
        val = os.environ.get(key, "")
        if val.startswith("${") and val.endswith("}"):
            return ""
        return val

    provider = (_env("COORDINARE_INFERENCE_PROVIDER") or "anthropic").strip().lower()

    agent_version = _env("COORDINARE_INFERENCE_AGENT_VERSION") or DEFAULT_INFERENCE_AGENT_VERSION
    model = _env("COORDINARE_INFERENCE_MODEL") or DEFAULT_INFERENCE_MODEL
    max_tokens_raw = _env("COORDINARE_INFERENCE_MAX_TOKENS")
    try:
        max_tokens = int(max_tokens_raw) if max_tokens_raw else DEFAULT_INFERENCE_MAX_TOKENS
    except ValueError:
        log.warning(
            "service_inference.invalid_max_tokens_override",
            value=max_tokens_raw,
            fallback=DEFAULT_INFERENCE_MAX_TOKENS,
        )
        max_tokens = DEFAULT_INFERENCE_MAX_TOKENS
    max_tool_calls_raw = _env("COORDINARE_INFERENCE_MAX_TOOL_CALLS")
    try:
        max_tool_calls = (
            int(max_tool_calls_raw) if max_tool_calls_raw else DEFAULT_INFERENCE_MAX_TOOL_CALLS
        )
    except ValueError:
        log.warning(
            "service_inference.invalid_max_tool_calls_override",
            value=max_tool_calls_raw,
            fallback=DEFAULT_INFERENCE_MAX_TOOL_CALLS,
        )
        max_tool_calls = DEFAULT_INFERENCE_MAX_TOOL_CALLS
    system_prompt = render_system_prompt(agent_version)

    if provider == "openai_compat":
        base_url = _env("COORDINARE_INFERENCE_BASE_URL").strip()
        if not base_url:
            # Fail fast — silently falling back to Anthropic would leak traffic
            # to a provider the operator explicitly opted out of.
            log.warning("service_inference.openai_compat_missing_base_url")
            return {
                "inference_skipped_reason": "openai_compat_missing_base_url",
                "inference_agent_version": agent_version,
            }
        compat_api_key = _env("COORDINARE_INFERENCE_API_KEY") or None
        from coordinare_service_inference.openai_compat_llm_client import (
            OpenAICompatServiceLLMClient,
        )
        client = OpenAICompatServiceLLMClient.from_config(
            base_url=base_url,
            api_key=compat_api_key,
            model=model,
            max_tokens=max_tokens,
            system_prompt=system_prompt,
        )
    elif provider == "anthropic":
        api_key = os.environ.get("ANTHROPIC_API_KEY", "")
        if not api_key:
            log.info("service_inference.no_api_key")
            return {"inference_skipped_reason": "no_api_key"}
        client = ClaudeServiceLLMClient.from_api_key(
            api_key=api_key,
            model=model,
            max_tokens=max_tokens,
            system_prompt=system_prompt,
        )
    else:
        log.warning("service_inference.unknown_provider", provider=provider)
        return {
            "inference_skipped_reason": f"unknown_provider:{provider}",
            "inference_agent_version": agent_version,
        }

    step_timeout_raw = _env("COORDINARE_INFERENCE_STEP_TIMEOUT")
    try:
        step_timeout = (
            float(step_timeout_raw)
            if step_timeout_raw
            else float(get_settings().SERVICE_INFERENCE_STEP_TIMEOUT)
        )
    except ValueError:
        step_timeout = float(get_settings().SERVICE_INFERENCE_STEP_TIMEOUT)

    try:
        result = await infer_services(
            project_root=stand_path,
            output_root=output_root,
            agent_version=agent_version,
            llm_client=client,
            max_tool_calls=max_tool_calls,
            step_timeout_seconds=step_timeout,
        )
    except InferenceFailed as exc:
        log.warning(
            "service_inference.failed",
            rejected_path=str(exc.rejected_path),
            attempts=len(exc.attempts),
        )
        return {
            "inference_skipped_reason": None,
            "inference_agent_version": agent_version,
            "inference_attempts": len(exc.attempts),
            "inference_succeeded": False,
            "inference_services": [],
        }
    except Exception as exc:  # noqa: BLE001 — failsafe, don't crash bootstrap
        log.warning(
            "service_inference.unexpected_error",
            error_type=type(exc).__name__,
            error=str(exc),
            traceback=traceback.format_exc(),
        )
        return {
            "inference_skipped_reason": f"unexpected_error: {type(exc).__name__}",
            "inference_agent_version": agent_version,
        }

    services = [s.name for s in result.manifest.services]
    log.info(
        "service_inference.succeeded",
        attempts=result.attempts,
        services=services,
        agent_version=agent_version,
    )
    return {
        "inference_skipped_reason": None,
        "inference_agent_version": agent_version,
        "inference_attempts": result.attempts,
        "inference_succeeded": True,
        "inference_services": services,
    }


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


# 072: roles for which the trailing ``{"status": "partial_progress"}`` JSON
# sentinel is honored. Architect/assessing/closing/env_bootstrap are short
# single-turn roles where checkpointing does not apply — emitting a
# sentinel from those roles is treated as prose and ignored.
#
# IMPORTANT: must stay in sync with ``SENTINEL_STAGES`` in
# ``src/coordinare/graph/nodes/monitor_performer.py``. The set is duplicated
# across processes because there is no shared library between coordinare
# and the performer container; add roles to both sides together.
SENTINEL_ROLES: frozenset[str] = frozenset(
    {"implementing", "reviewing", "security", "qa", "documenting"}
)


def _extract_trailing_partial_progress(text: str) -> dict | None:
    """Detect a trailing partial_progress JSON sentinel in implementer output.

    Looks for the last ``{...}`` block in ``text`` and returns it only when it
    parses as a JSON object with ``status == "partial_progress"``. Returns None
    in all other cases so prose with stray braces never triggers the escape
    hatch.
    """
    if not text:
        return None
    end = text.rfind("}")
    if end < 0:
        return None
    depth = 0
    start = -1
    for i in range(end, -1, -1):
        ch = text[i]
        if ch == "}":
            depth += 1
        elif ch == "{":
            depth -= 1
            if depth == 0:
                start = i
                break
    if start < 0:
        return None
    try:
        obj = _json_module.loads(text[start:end + 1])
    except (ValueError, TypeError):
        return None
    if isinstance(obj, dict) and obj.get("status") == "partial_progress":
        return obj
    return None


def _count_bot_comments(comments: list[dict[str, Any]]) -> int:
    """Count comments authored by a bot account (``user.type == "Bot"``).

    072 FR-072-6: the per-role zero-progress guardrail wants to know whether
    the *agent* (or any bot) surfaced something this turn. Human comments
    posted by operators between dispatch and turn-end must not inflate the
    delta, or the guardrail will incorrectly treat a silent reviewer turn
    as having "real signal" and skip re-dispatch.

    Bot detection relies on GitHub returning ``user.type == "Bot"`` for the
    authoring account. This is true for GitHub Apps (the standard performer
    deployment) and the ``github-actions[bot]`` workflow identity. PAT-based
    deployments — where the performer authenticates as a regular user
    account — will NOT match here; in that case all comments look "human"
    and the delta stays 0, so the guardrail trips on a silent turn rather
    than getting confused by the agent's own comments. That is the
    conservative default and matches the production deployment we ship.
    """
    n = 0
    for c in comments:
        user = c.get("user") if isinstance(c, dict) else None
        if isinstance(user, dict) and user.get("type") == "Bot":
            n += 1
    return n


async def _compute_pr_comment_delta(perf: Performance) -> int:
    """Return new bot-authored PR comments since dispatch, or 0 if delta cannot be computed.

    072: surfaces a "did this turn produce any visible bot PR activity"
    signal so the coordinare per-role guardrail can distinguish a silent
    reviewer/qa turn (delta == 0, route back to dispatching) from a turn
    that surfaced something real (delta > 0, honor the blocked verdict).
    """
    if perf.pr_comments_at_start is None or not perf.pr_url:
        return 0
    pr_number = _extract_pr_number(perf.pr_url)
    if not pr_number:
        return 0
    try:
        owner, repo = perf.score.owner_repo
        current = await list_pr_comments(
            owner, repo, pr_number, token=perf.score.effective_github_token,
        )
    except Exception as exc:
        log.warning("pr_comment_delta.list_failed", error=str(exc))
        return 0
    return max(0, _count_bot_comments(current) - perf.pr_comments_at_start)


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


_ROLE_DISPLAY = {
    "assessing": "Assessor",
    "architecting": "Architect",
    "implementing": "Implementer",
    "reviewing": "Reviewer",
    "closing_review": "Closer",
    "security": "Security",
    "qa": "QA",
    "documenting": "Tech Writer",
    "env_bootstrap": "Env Bootstrap",
}


def _attribution_header(
    *,
    origin: str,
    role: str,
    display: str,
    harness: str,
    model: str,
) -> str:
    """Standardized attribution header for every PR/issue comment (077).

    Emits a hidden machine-readable marker followed by a human-readable blockquote
    line, so a reader (or a tool) can always tell *who* commented, with *which*
    agent harness, driving *which* model backend. Kept byte-for-byte in sync with
    the coordinare-side builder (``coordinare.graph.attribution.attribution_header``)
    — both units post as the same GitHub app, so this header is the only
    disambiguator. ``origin`` is ``performer`` or ``coordinare``; ``role`` is the
    raw lifecycle stage key (e.g. ``reviewing``) for the marker; ``display`` is the
    human label (e.g. ``Reviewer``).
    """
    marker = (
        f"<!-- coordinare-attribution origin={origin} role={role} "
        f"harness={harness} model={model} -->"
    )
    icon = "🎼" if origin == "coordinare" else "🤖"
    if origin == "coordinare":
        line = f"> {icon} **Coordinare** · re: {display} (`{harness}` · `{model}`)"
    else:
        line = f"> {icon} **{display}** · harness `{harness}` · model `{model}`"
    return f"{marker}\n{line}"


def _persona_tag(score: Score, role: str | None = None) -> str:
    """Attribution header for performer bot comments — which persona/harness/model
    produced it.

    Every bot comment posts as the same GitHub app ('vivi-coordinare'), so without
    this you can't tell the reviewer from the closer from QA, nor which backend
    drove it (the whole point of a multi-backend round). ``role`` overrides
    ``score.role`` — pass ``perf.role`` (the authoritative running stage) when
    available, since ``score.role`` can carry a stale default.
    """
    r = role or score.role or "performer"
    display = _ROLE_DISPLAY.get(r, r)
    harness = score.backend or "?"
    model = score.model or "?"
    return _attribution_header(
        origin="performer", role=r, display=display, harness=harness, model=model
    )


_QA_ENV_FAILURE_PATTERNS = re.compile(
    r"pg::|connectionbad|could not connect|connection refused|econnrefused|"
    r"read-?only file system|sqlite3|not on \$?path|command not found|not installed|"
    r"no such file|headless|chromium|chrome|browser|display|selenium|webdriver|"
    r"postgres|pg_ctl|initdb|database (is )?(unavailable|not running|down)|"
    r"no screenshot|no artifacts|visual artifacts|could not (start|launch|run)",
    re.IGNORECASE,
)


def _qa_failure_is_environmental(f: dict) -> bool:
    """077: True if a QA failure reflects the container's inability to VERIFY
    (no DB/browser/binary on PATH, read-only fs, capture blocked) rather than a
    real code defect. Environmental limits are advisory — they must not block the
    lifecycle; only genuine defects do."""
    if str(f.get("type", "")).lower() in {"visual-capture", "environment", "env"}:
        return True
    blob = " ".join(
        str(f.get(k, "")) for k in ("message", "actual", "expected", "criterion", "test")
    )
    return bool(_QA_ENV_FAILURE_PATTERNS.search(blob))


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
        _persona_tag(score),
        "",
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
            is_url = loc.lower().startswith(("http://", "https://"))
            is_image = kind == "screenshot" or Path(loc).suffix.lower() in {
                ".png", ".jpg", ".jpeg", ".gif", ".webp",
            }
            line = f"- **{label}** ({kind})"
            if loc:
                if is_url and is_image:
                    line += f": [{loc}]({loc})\n\n  ![{label}]({loc})"
                elif is_url:
                    line += f": [{loc}]({loc})"
                else:
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
    lenient_fallback: Callable[[], Awaitable[PerformerResponse]] | None = None,
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
    # 077: retries exhausted. For roles that can SAFELY interpret prose (assessor
    # → default-sufficient; reviewer → changes-requested, never auto-approve), fall
    # back to that instead of hard-failing the stage — a weak/limited model that
    # emits a correct verdict as prose shouldn't park the card in the Blocked
    # column. The fallback is responsible for redacting any text it surfaces.
    if lenient_fallback is not None:
        log.warning(
            "backend.parse_lenient_fallback",
            stage=stage_label,
            role=perf.role,
            failure_reason=failure_reason,
            output_preview=redacted_short,
        )
        return await lenient_fallback()
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


# 065 Fix 3: advisory dedup. PR #133 oscillated because the security pass
# re-emitted the same OWASP-class advisory every cycle with slightly
# different wording. Fingerprint on OWASP code (or slugified category) +
# severity so re-runs detect prior advisories regardless of phrasing drift.
_OWASP_CODE_RE = re.compile(r"owasp\s*a\d+", re.IGNORECASE)
_ADVISORY_HEADER_RE = re.compile(r"^\[Advisory - Security\]\s*\*\*(.+?)\*\*\s*\((.+?)\)")


def _advisory_fingerprint(category: str, severity: str) -> str:
    cat = (category or "").strip().lower()
    sev = (severity or "").strip().lower()
    m = _OWASP_CODE_RE.search(cat)
    key = re.sub(r"\s+", "", m.group(0)) if m else re.sub(r"[^a-z0-9]", "", cat)[:40]
    return f"{key}|{sev}"


def _parse_advisory_header(body: str) -> tuple[str, str] | None:
    m = _ADVISORY_HEADER_RE.match(body or "")
    return (m.group(1), m.group(2)) if m else None


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


async def _stop_orchestration_proxy(perf: Performance) -> None:
    """080: best-effort teardown of the per-job dual-model proxy (if any)."""
    proxy = getattr(perf, "orchestration_proxy", None)
    if proxy is not None:
        try:
            await proxy.stop()
        except Exception:  # pragma: no cover — best-effort; container is ephemeral
            pass


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
    # 080: for a non-single mode, launch the in-container dual-model proxy and
    # point this backend's provider base URL at it BEFORE the CLI starts. No-op
    # (returns None) when the dispatch carries no orchestration block.
    orchestration_proxy = None
    try:
        from performer.proxy.launch import maybe_launch_proxy

        orchestration_proxy = await maybe_launch_proxy(score.orchestration, backend_name)
    except Exception as exc:
        log.warning("dual_model_proxy.launch_failed", error=str(exc))
        cleanup_stand(stand)
        raise
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
        if orchestration_proxy is not None:
            await orchestration_proxy.stop()
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
        orchestration_proxy=orchestration_proxy,
        role=role,
        pr_url=pr_url,
        pr_node_id=pr_node_id,
    )
    try:
        perf.head_at_start = await get_head_sha(stand)
    except Exception as exc:
        log.warning("dispatch.head_at_start_capture_failed", error=str(exc))
    # 072: snapshot pre-turn PR comment count for bot_pr_comment_delta.
    if pr_url:
        try:
            owner_repo = score.owner_repo
            pr_number = _extract_pr_number(pr_url)
            if pr_number:
                pre_comments = await list_pr_comments(
                    owner_repo[0], owner_repo[1], pr_number,
                    token=score.effective_github_token,
                )
                perf.pr_comments_at_start = _count_bot_comments(pre_comments)
        except Exception as exc:
            log.warning("dispatch.pr_comments_at_start_failed", error=str(exc))
    log.info("dispatch accepted", session_id=session_id)
    return PerformerResponse(
        status="accepted",
        session_id=session_id,
        backend=backend_name,
        model=model_name,
    ), perf


def _format_check_failures(failed_runs: list[dict[str, Any]]) -> str:
    """Format failed check run details for relay to the backend.

    Returns the Check Run's structured ``output`` (title/summary/text). The
    actual workflow log is fetched on-demand by the model via the
    ``performer-fetch-ci-log`` CLI shim — relay only provides the summary
    so we don't pay log API cost on every poll.
    """
    parts = []
    for run in failed_runs:
        name = run.get("name", "unknown")
        output = run.get("output") or {}
        title = output.get("title") or ""
        summary = output.get("summary") or ""
        text = (output.get("text") or "")[:4000]
        part = f"### {name}"
        if title:
            part += f"\n{title}"
        if summary:
            part += f"\n{summary}"
        if text:
            part += f"\n{text}"
        parts.append(part)
    return "\n\n".join(parts)


async def _format_check_failures_with_logs(
    failed_runs: list[dict[str, Any]],
    *,
    owner: str,
    repo: str,
    token: str,
    min_output_chars: int,
    max_total_chars: int,
) -> str:
    """Like ``_format_check_failures`` but auto-inlines workflow log tails.

    For each failed run whose ``output.text`` is shorter than
    ``min_output_chars``, fetch the GitHub Actions job log and append a
    ``**Log tail (job <id>)**`` block. Total inlined log content across all
    runs is capped at ``max_total_chars`` via equal fair-share slicing with
    an 800-char floor so the first failure doesn't consume the whole budget.
    Fetch errors degrade silently to the no-log body — never raises.
    """
    base = _format_check_failures(failed_runs)
    if max_total_chars <= 0 or not failed_runs:
        return base

    candidates: list[tuple[int, dict[str, Any]]] = []
    for run in failed_runs:
        output = run.get("output") or {}
        text = output.get("text") or ""
        if len(text) < min_output_chars:
            candidates.append((run.get("id"), run))  # type: ignore[arg-type]
    if not candidates:
        return base

    remaining = max_total_chars
    blocks: list[str] = []
    for i, (job_id, run) in enumerate(candidates):
        if not job_id:
            continue
        slots_left = len(candidates) - i
        slice_size = max(800, remaining // slots_left)
        try:
            tail = await get_check_run_logs(
                owner, repo, int(job_id), token, max_chars=slice_size
            )
        except Exception as exc:
            log.warning("ci_log_inline.fetch_failed", job_id=job_id, error=str(exc))
            tail = ""
        if not tail:
            continue
        name = run.get("name", "unknown")
        blocks.append(f"**Log tail (job {job_id}, check {name})**:\n```\n{tail}\n```")
        remaining = max(0, remaining - len(tail))
        if remaining <= 0:
            break

    if not blocks:
        return base
    return base + "\n\n" + "\n\n".join(blocks)


def _failure_signature(failed_runs: list[dict[str, Any]]) -> str:
    """Stable signature of a failure set: same checks failing the same way
    across attempts produces the same string.
    """
    items = []
    for run in failed_runs:
        name = run.get("name", "")
        conclusion = run.get("conclusion", "")
        output = run.get("output") or {}
        title = output.get("title", "") or ""
        items.append(f"{name}|{conclusion}|{title}")
    items.sort()
    return "\n".join(items)


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
        head_after: str | None = None
        try:
            head_after = await get_head_sha(perf.stand)
        except Exception as exc:
            log.warning("pr_opened.head_after_failed", error=str(exc))
        return PerformerResponse(
            status="pr_opened",
            session_id=perf.session_id,
            pr_url=perf.pr_url,
            pr_node_id=perf.pr_node_id,
            head_before=perf.head_at_start,
            head_after=head_after,
        )

    if verdict == "pending":
        return PerformerResponse(
            status="working",
            session_id=perf.session_id,
            progress="CI checks in progress...",
        )

    # verdict == "fail"
    max_attempts = settings.CHECK_MAX_ATTEMPTS if settings is not None else 3
    no_progress_limit = settings.CHECK_NO_PROGRESS_LIMIT if settings is not None else 2

    # 065 Fix 14: no-progress detection — same checks failing identically
    # means the model isn't making progress; bail before burning the full
    # max_attempts budget.
    signature = _failure_signature(failed)
    if signature == perf.last_check_failure_signature:
        perf.check_no_progress_streak += 1
    else:
        perf.check_no_progress_streak = 0
        perf.last_check_failure_signature = signature

    names = ", ".join(r.get("name", "unknown") for r in failed)

    if perf.check_no_progress_streak >= no_progress_limit:
        questions = [
            f"CI checks failed with no progress across {perf.check_no_progress_streak + 1} attempts: {names}"
        ]
        perf.state = "blocked"
        perf.open_questions = questions
        return PerformerResponse(
            status="blocked",
            session_id=perf.session_id,
            questions=questions,
        )

    if perf.check_attempt >= max_attempts:
        questions = [f"CI checks failed after {perf.check_attempt} fix attempt(s): {names}"]
        perf.state = "blocked"
        perf.open_questions = questions
        return PerformerResponse(
            status="blocked",
            session_id=perf.session_id,
            questions=questions,
        )

    perf.check_attempt += 1
    min_output_chars = settings.CI_LOG_INLINE_MIN_OUTPUT_CHARS if settings is not None else 200
    max_total_chars = settings.CI_LOG_INLINE_MAX_CHARS if settings is not None else 6000
    if max_total_chars > 0:
        failure_msg = await _format_check_failures_with_logs(
            failed,
            owner=owner,
            repo=repo,
            token=perf.score.effective_github_token,
            min_output_chars=min_output_chars,
            max_total_chars=max_total_chars,
        )
    else:
        failure_msg = _format_check_failures(failed)
    failing_names = [r.get("name", "unknown") for r in failed]
    tool_hint = (
        "\n\n**To read the actual workflow logs for any failing check, run:**\n"
        "```\nperformer-fetch-ci-log --check '<check name>'\n```\n"
        "Examples: " + ", ".join(f"`performer-fetch-ci-log --check '{n}'`"
                                 for n in failing_names[:3])
        + "\nUse `performer-fetch-ci-log --list` to see all failing checks."
    )
    await perf.backend.relay_feedback(
        f"CI checks failed. Fix the following:\n\n{failure_msg}{tool_hint}"
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

    backend_status: BackendStatus = perf.backend.get_status()

    if backend_status.state == "done":
        # 077: diagnostic/benchmark probe — return the agent's output verbatim
        # with NO lifecycle scaffolding (no commit/push/PR, no verify, no
        # service-inference). Short-circuits before every role branch so a
        # viability probe ("can this backend drive a browser on this model?")
        # isn't distorted by a role's commit/PR/JSON contract.
        if perf.role == DIAGNOSTIC_ROLE:
            perf.state = "diagnostic_complete"
            return PerformerResponse(
                status="diagnostic_complete",
                session_id=perf.session_id,
                progress=(backend_status.output or "").strip()[:4000],
            )

        # 020: Architect path — commit plan file instead of opening a PR.
        # Plan content can arrive two ways:
        #   1. inline — backend sets BackendStatus.output to the plan text
        #      (used when the model emits an assistant message containing it)
        #   2. workspace — backend writes {folder}/plan.md (and optional
        #      tasks.md) directly via tool calls (e.g. codex apply_patch).
        #      Detected after the fact by reading the file back.
        # Fix 13 (065): codex populates `output` only from assistant text; when
        # the model writes the plan via apply_patch, output is empty. Probe
        # the workspace before declaring the plan empty.
        if perf.role == "architecting":
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
                ws_plan = perf.stand.path / folder / "plan.md"
                ws_tasks = perf.stand.path / folder / "tasks.md"
                if ws_plan.is_file():
                    try:
                        plan_content = ws_plan.read_text(encoding="utf-8").strip()
                    except OSError as exc:
                        log.warning("architect.read_workspace_plan_failed", path=str(ws_plan), error=str(exc))
                        plan_content = ""
                    if plan_content and ws_tasks.is_file():
                        try:
                            tasks_content = ws_tasks.read_text(encoding="utf-8").strip()
                        except OSError as exc:
                            log.warning("architect.read_workspace_tasks_failed", path=str(ws_tasks), error=str(exc))
                    if plan_content:
                        plan_source = "workspace"

            if not plan_content:
                perf.state = "error"
                perf.error_reason = "Backend produced an empty architecture plan"
                return PerformerResponse(
                    status="error",
                    session_id=perf.session_id,
                    reason="Backend produced an empty architecture plan",
                )

            log.info("architect.plan_source", source=plan_source, session_id=perf.session_id)

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
                async def _assessor_lenient_sufficient() -> PerformerResponse:
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
                    lenient_fallback=_assessor_lenient_sufficient,
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

            # 077 (pipeline-tolerant): split environmental incapability (couldn't
            # run tests / no browser / no DB / missing binary) from real code
            # defects. QA blocks ONLY on defects it actually found; environmental
            # limits are advisory so a card isn't trapped by a container that
            # can't verify it. A FAILED verdict must mean "checked and broken",
            # not "couldn't check".
            defect_failures = [f for f in failures if not _qa_failure_is_environmental(f)]
            env_limited = bool(env_error) or len(defect_failures) < len(failures)
            qa_passed_flag = not defect_failures

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

            # Bug 16.2: upload any container-local screenshots to GitHub's
            # user-attachments CDN so the PR/issue comment renders embeddable
            # images instead of container-local /tmp/... paths.
            try:
                owner_for_upload, repo_for_upload = perf.score.owner_repo
                upload_issue_no = perf.score.issue_number or _extract_pr_number(perf.pr_url)
                visual_evidence = await resolve_visual_evidence_urls(
                    visual_evidence,
                    workspace_root=perf.stand.path,
                    github_token=perf.score.effective_github_token,
                    org=owner_for_upload,
                    repo=repo_for_upload,
                    issue_number=upload_issue_no,
                )
            except Exception as exc:
                log.warning("qa.visual_evidence_upload_failed", error=str(exc))

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

            # 077: env_error and environmental failures are ADVISORY — they no
            # longer hard-block QA. The lifecycle only stops for real defects
            # (defect_failures). If QA couldn't verify due to the container's
            # limits, it passes DEGRADED with the limitation recorded.
            if not defect_failures:
                if env_limited:
                    log.warning(
                        "qa.env_limited_advisory_pass",
                        session_id=perf.session_id,
                        env_error=(env_error or "")[:200],
                        env_failure_count=len(failures) - len(defect_failures),
                    )
                perf.state = "qa_passed"
                perf.qa_report = {
                    "criteria_checked": qa_output.get("criteria_checked", 0),
                    "criteria_passed": qa_output.get("criteria_passed", 0),
                    "env_limited": env_limited,
                    "environment_error": env_error or None,
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

            # Real code defects exist → changes_requested / block (as before).
            # Only defect_failures count; environmental limits were advisory above.
            max_cycles = settings.QA_MAX_CYCLES if settings else 3
            perf.qa_cycle += 1
            if perf.qa_cycle >= max_cycles:
                summary = f"QA: {len(defect_failures)} acceptance criterion failure(s) after {perf.qa_cycle} fix attempt(s)"
                perf.state = "blocked"
                perf.open_questions = [summary]
                return PerformerResponse(
                    status="blocked",
                    session_id=perf.session_id,
                    questions=[summary],
                )
            perf.qa_failures = [f for f in defect_failures if isinstance(f, dict)]
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

        # 060/Option A: env_bootstrap path — backend ran install commands
        # into the mounted cache_mount_path. There is no PR to open and no
        # commit to push; just report a terminal success status. Failures
        # surface as backend_status.state == "error" and route through the
        # generic error path elsewhere in this function.
        if perf.role == "env_bootstrap":
            # 077 (Tier 2): confirm the install actually worked before reporting
            # success. The agent wrote verify.sh asserting every documented
            # dependency is present + runnable; a non-zero exit means a silent
            # install failure (e.g. apt-get located no package), so we FAIL the
            # bootstrap here. The coordinare's on_bootstrap_complete(success=False)
            # path then clears readme_sha and retries — instead of marking a
            # broken cache "ready". A missing verify.sh is treated as a degraded
            # (legacy) bootstrap: logged, not failed.
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

            inference_timeout = get_settings().SERVICE_INFERENCE_TIMEOUT
            try:
                perf.inference_state = await asyncio.wait_for(
                    _run_service_inference(
                        perf.stand.path, perf.score.env_cache_path
                    ),
                    timeout=inference_timeout,
                )
            except asyncio.TimeoutError:
                # 076 (live QA #150): service_inference is a best-effort,
                # secondary probe — the dev-env install already ran into the
                # mounted cache above.  A timeout here MUST NOT fail the whole
                # bootstrap: doing so leaves the cache un-ready and coordinare
                # re-dispatches the bootstrap forever (observed: 3 attempts, 0
                # successes, ~30-45 min each on a slow backend, gating the
                # symphony indefinitely).  Mirror _run_service_inference's own
                # internal failsafe (which returns inference_succeeded=False on
                # InferenceFailed / unexpected errors) — only the external
                # wait_for timeout escaped it.  Report terminal success with the
                # inference skipped, so the cache is marked ready and the
                # degraded inference surfaces on the dashboard.
                log.warning(
                    "service_inference.timeout",
                    job_id=perf.session_id,
                    timeout_seconds=inference_timeout,
                    detail=(
                        "inference timed out; treating bootstrap as complete "
                        "(install already succeeded, inference is best-effort)"
                    ),
                )
                perf.state = "env_bootstrap_complete"
                return PerformerResponse(
                    status="env_bootstrap_complete",
                    session_id=perf.session_id,
                    inference_succeeded=False,
                    inference_skipped_reason="timeout",
                )
            perf.state = "env_bootstrap_complete"
            return PerformerResponse(
                status="env_bootstrap_complete",
                session_id=perf.session_id,
                **perf.inference_state,
            )

        # 070: implementer partial_progress escape hatch. If the backend
        # emitted a trailing ``{"status": "partial_progress", ...}`` sentinel,
        # push whatever was committed, post a status comment on the PR (when
        # one exists), and hand control back to the coordinare with
        # status="partial_progress" so the next turn resumes from next_focus.
        if perf.role in SENTINEL_ROLES:
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
        head_after_blocked: str | None = None
        try:
            head_after_blocked = await get_head_sha(perf.stand)
        except Exception as exc:
            log.warning("blocked.head_after_failed", error=str(exc))
        bot_comment_delta = await _compute_pr_comment_delta(perf)
        return PerformerResponse(
            status="blocked",
            session_id=perf.session_id,
            questions=backend_status.questions,
            head_before=perf.head_at_start,
            head_after=head_after_blocked,
            bot_pr_comment_delta=bot_comment_delta,
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

    def _watchdog_remaining() -> float | None:
        """Per-chunk timeout: AGENT_TIMEOUT minus elapsed session time."""
        if perf is None:
            return None
        elapsed = (datetime.now(UTC) - perf.started_at).total_seconds()
        remaining = settings.AGENT_TIMEOUT - elapsed
        # Clamp positive so wait_for actually raises on the read() call rather
        # than rejecting the timeout value itself.
        return max(remaining, 0.001)

    line_iter = iter_lines_chunked(reader, timeout=_watchdog_remaining)

    while True:
        try:
            raw = await line_iter.__anext__()
        except StopAsyncIteration:
            break
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
        await _stop_orchestration_proxy(perf)
        cleanup_stand(perf.stand)


def _write_response(resp: PerformerResponse) -> None:  # pragma: no cover
    # Spec 063 Phase 4 (T023): drain the env-cache health-failure flag set by
    # workspace._run_env_cache_health_check, so the very next outbound response
    # carries the signal even when callers built the response without it.
    from performer.workspace import consume_env_cache_health_failure
    if consume_env_cache_health_failure():
        resp = resp.model_copy(update={"env_cache_health_failed": True})
    sys.stdout.write(resp.model_dump_json(exclude_none=True) + "\n")
    sys.stdout.flush()


async def _perform_job(payload: "JobInitPayload") -> "JobResult":  # pragma: no cover
    """Bridge between the spec-056 HTTP job protocol and handle_dispatch/handle_status.

    Called by the job runner inside the performer HTTP server. Builds a
    PerformerMessage from the JobInitPayload, drives handle_dispatch to clone
    and start the backend, then polls handle_status until the session reaches a
    terminal state. The final PerformerResponse is serialised as JSON into
    JobResult.summary so that the coordinare-side check_status() can reconstruct
    the rich response dict that monitor_performer.py expects.
    """
    from performer.server.models import JobResult  # local import — server subpackage

    settings = get_settings()

    # Snapshot env values that will be overwritten so we can restore them
    # after the job (None means the key was absent before injection).
    _pre_job_env: dict[str, str | None] = {
        _k: os.environ.get(_k) for _k in payload.secrets
    }
    for _secret_name, _secret_val in payload.secrets.items():
        os.environ[_secret_name] = _secret_val.get_secret_value()

    try:
        # Prefer the payload value; fall back to any env-baseline GITHUB_TOKEN
        # (e.g. injected at container startup via the env fallback source).
        github_token = (
            payload.secrets["GITHUB_TOKEN"].get_secret_value()
            if "GITHUB_TOKEN" in payload.secrets
            else os.environ.get("GITHUB_TOKEN", "")
        )

        # Build Score-compatible dict: metadata carries title/description/etc.
        score_dict: dict = {
            **payload.metadata,
            "repo_url": str(payload.repo_url),
            "branch": payload.branch,
            "role": payload.role,
            "backend": payload.backend,
            "github_token": github_token,
        }
        if payload.persona:
            score_dict["persona_instructions"] = payload.persona
        if payload.env_cache_path:
            score_dict["env_cache_path"] = payload.env_cache_path

        dispatch_msg = PerformerMessage(action="dispatch", payload=score_dict)
        try:
            _accepted_resp, perf = await handle_dispatch(dispatch_msg, settings)
        except Exception as exc:
            log.exception("perform_job.dispatch_failed", exc_type=type(exc).__name__)
            return JobResult(
                success=False,
                summary=_redact_secrets(
                    f"dispatch failed: {type(exc).__name__}: {exc}"
                ),
                error_code="dispatch_error",
            )

        from performer.server.job_runner import (  # local import — server subpackage
            _progress_cb_var,
            _refreshed_secrets_var,
        )

        status_msg = PerformerMessage(action="status", session_id=perf.session_id)
        resp = None
        # Track the last applied github_token so we only re-inject when it
        # actually changed — avoids spamming git_env rebuilds every 2s tick.
        _last_applied_token: str | None = None
        try:
            while True:
                await asyncio.sleep(2.0)
                # US5 / Spec 073 Phase 9: pick up any secrets PATCHed mid-job
                # by the coordinare (refreshed GitHub App token before the
                # 1h expiry boundary). Mirror the stdio refresh at the
                # `action == "status"` branch above.
                _refreshed = _refreshed_secrets_var.get()
                if _refreshed:
                    _new_token = _refreshed.get("github_token")
                    if (
                        _new_token
                        and _new_token != _last_applied_token
                        and perf is not None
                        and perf.score is not None
                    ):
                        perf.score.github_token = _new_token
                        perf.stand.git_env = _git_credential_vars(_new_token)
                        _last_applied_token = _new_token
                        log.debug(
                            "token_refreshed_http",
                            session_id=perf.session_id,
                        )
                resp = await handle_status(status_msg, perf, settings)
                _progress_cb = _progress_cb_var.get()
                if _progress_cb is not None and resp.events:
                    _progress_cb(
                        [e if isinstance(e, dict) else e.model_dump(exclude_none=True) for e in resp.events],
                        resp.metrics if isinstance(resp.metrics, dict) else (resp.metrics.model_dump() if resp.metrics is not None else None),
                    )
                if resp.status in TERMINAL_STATUSES:
                    break
        finally:
            try:
                await perf.backend.stop()
            except Exception:
                pass
            await _stop_orchestration_proxy(perf)
            cleanup_stand(perf.stand)

        if resp is None:
            return JobResult(success=False, summary="no status response", error_code="internal_error")

        # Spec 063 Phase 4 (T023): drain env-cache health-failure flag into
        # the final HTTP-job summary so coordinare's check_status can route it.
        from performer.workspace import consume_env_cache_health_failure
        if consume_env_cache_health_failure():
            resp = resp.model_copy(update={"env_cache_health_failed": True})

        success = resp.status not in FAILURE_STATUSES
        summary = resp.model_dump_json(exclude_none=True)
        return JobResult(success=success, summary=summary, error_code=None if success else resp.status)
    finally:
        # Restore env to its pre-job state: remove keys that were absent before
        # injection, or put back the original value for keys that were present.
        # This preserves env-baseline secrets (e.g. GITHUB_TOKEN baked into the
        # container image) for subsequent jobs and pre-flight checks.
        for _k, _prev_val in _pre_job_env.items():
            if _prev_val is None:
                os.environ.pop(_k, None)
            else:
                os.environ[_k] = _prev_val


def _run_server(port: int) -> None:  # pragma: no cover
    """Boot the FastAPI HTTP server (spec 056, T025).

    Runs as PID 1 inside the performer container. Auth token is read from
    ``PERFORMER_AUTH_TOKEN`` (absent → auth disabled).
    """
    import atexit
    import signal

    import uvicorn

    from performer.server import create_app_from_env
    from performer.workspace import stop_all_env_cache_services

    # Spec 063 T007: stop any services started in env-caches during this
    # process so daemons (redis/postgres/…) do not leak past container exit.
    atexit.register(stop_all_env_cache_services)
    _prev_term = signal.getsignal(signal.SIGTERM)
    _prev_int = signal.getsignal(signal.SIGINT)

    def _shutdown_handler(signum, frame):  # type: ignore[no-untyped-def]
        stop_all_env_cache_services()
        # Re-raise the default behavior so uvicorn still exits cleanly.
        if signum == signal.SIGTERM and callable(_prev_term):
            _prev_term(signum, frame)
        elif signum == signal.SIGINT and callable(_prev_int):
            _prev_int(signum, frame)

    signal.signal(signal.SIGTERM, _shutdown_handler)
    signal.signal(signal.SIGINT, _shutdown_handler)

    app = create_app_from_env(executor=_perform_job)
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")


def main() -> None:  # pragma: no cover
    import argparse

    parser = argparse.ArgumentParser(prog="performer")
    parser.add_argument(
        "--serve",
        action="store_true",
        help="Run as HTTP server (spec 056) instead of stdio dispatch loop.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8088,
        help="Port for --serve mode (default: 8088).",
    )
    args, _ = parser.parse_known_args()

    if args.serve:
        _run_server(args.port)
        return
    asyncio.run(run_loop())
