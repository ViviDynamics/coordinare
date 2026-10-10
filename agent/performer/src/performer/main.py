"""Performer entrypoint — wire protocol message loop."""
from __future__ import annotations

import asyncio
import json as _json_module
import shlex
from pathlib import Path
import os
import re
import sys
import traceback
import uuid
from datetime import UTC, datetime
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any
from urllib.parse import urlparse

if TYPE_CHECKING:
    from performer.server.models import JobInitPayload, JobResult

import psutil
import structlog
from pydantic import ValidationError

from performer.backends import UnsupportedBackendError, get_backend
from performer.backends.base import BackendAdapter, BackendStatus
from performer.config import Settings, get_settings
# 164 T049: qa_postprocess.finalize_qa looks these collaborators up on THIS
# module at call time (import-cycle avoidance, and ~85 test patches target
# performer.main.<name>). Ruff sees no local use and would prune them.
from performer.cdn_upload import resolve_visual_evidence_urls  # noqa: F401
from performer.qa_capture import boot_and_capture_app_screenshot  # noqa: F401
# 164 T049: qa_postprocess.finalize_qa looks these collaborators up on THIS
# module at call time (import-cycle avoidance, and ~85 test patches target
# performer.main.<name>). Ruff sees no local use and would prune them.
from performer.github import GitHubAPIError, create_pull_request, get_check_run_logs, get_check_runs, list_pr_comments, post_issue_comment, post_pr_comment, post_pull_request_review, resolve_pr_review_threads, summarise_check_runs  # noqa: F401 (post_issue_comment: see T049 note above)  # noqa: F401 (post_issue_comment: see T049 note above)
from performer.io_utils import iter_lines_chunked
from performer.models import DIAGNOSTIC_ROLE, Performance, Score, Stand, _redact_secrets
from performer.test_results import _strip_ansi, _reported_failure_count, _match_env_signature, _env_signature_reason, _format_failure_excerpt, _TEST_FAILURE_MARKERS  # noqa: F401
from performer.protocol import (
    FAILURE_STATUSES,
    TERMINAL_STATUSES,
    PerformerMessage,
    PerformerMetrics,
    PerformerResponse,
)
from performer.workspace import commit_file  # noqa: F401  (late-bound by status_paths helpers)
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

from performer.status_paths import (
    _terminal_status_response,
    architect_path,
    assessor_path,
    bootstrap_path,
    closing_record_path,
    documenting_path,
    implementing_path,
    intake_path,
    reviewer_path,
    security_path,
    sentinel_path,
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


async def _run_ci_check(
    stand_path: Path, label: str = "performer",
    lint_override: str | None = None, sub_roots: tuple[str, ...] = (),
) -> tuple[bool, str]:
    """Run the detected lint command in the workspace before committing.

    Returns ``(True, "")`` if lint passes or no linter detected, or
    ``(False, error_output)`` if lint fails.  The performer should either
    fix the issue or bail with an error.

    ``lint_override`` (409) is the operator-pinned command from
    LocalTestGateConfig.lint_command; ``sub_roots`` are the declared
    monorepo roots probed when the workspace root matches no convention.
    """
    try:
        from coordinare_ci_detection import detect
    except ImportError:
        # Performer may be deployed without the coordinare package installed
        # (standalone mode).  Fall back gracefully — the coordinare-side gate
        # provides the backstop.
        log.info("ci_check.coordinare_not_available", label=label)
        return True, ""

    lint_command = lint_override
    if not lint_command:
        result = detect(stand_path, sub_roots=sub_roots)
        if not result.lint_command and result.per_root:
            for root, root_result in result.per_root.items():
                if root_result.lint_command:
                    lint_command = f"cd {shlex.quote(root)} && {root_result.lint_command}"
                    break
        else:
            lint_command = result.lint_command
    if lint_command is None:
        log.info("ci_check.no_lint_detected", label=label)
        return True, ""

    log.info("ci_check.running", label=label, command=lint_command, stack="lint")
    run_result = await run_command(lint_command, stand_path, timeout=120)
    if run_result.success:
        log.info("ci_check.passed", label=label, command=lint_command, duration=run_result.duration_seconds)
        return True, ""

    # Stripped once, here, so every reader below -- the signature matcher, the
    # runner-summary precedence, the failure count, and the excerpt that reaches
    # the implementer -- sees the same plain text.
    error_output = _strip_ansi((run_result.stderr + "\n" + run_result.stdout).strip())
    log.warning(
        "ci_check.failed",
        label=label,
        command=lint_command,
        exit_code=run_result.exit_code,
        output_preview=error_output[:200],
    )
    return False, error_output


# ---------------------------------------------------------------------------
# 089 — Implementer local test gate (US1)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LocalTestResult:
    """Outcome of the local test-execution gate.

    ``passed`` is True when tests ran green, when no test command was detected,
    or when the coordinare package is unavailable (standalone pass-through) — in
    all of which cases the gate must not block the push.  ``command`` is the
    test command that ran (None when skipped/passed-through).  ``output`` is the
    truncated failure tail (empty on success).  ``env_blocked`` is True only when
    a failure is attributable to an environment signal (089 US2); ``env_reason``
    carries the human-readable cause in that case.  ``exit_code`` is the test
    command's exit status (None when skipped/passed-through), surfaced in the
    failure feedback so the implementer sees how the run terminated.
    """

    passed: bool
    command: str | None
    output: str
    duration_seconds: float
    env_blocked: bool = False
    env_reason: str | None = None
    exit_code: int | None = None
    #: True when the gate re-ran the suite before concluding a defect.
    retried: bool = False
    #: Attempt 1's output, carried only when the two attempts failed
    #: *differently*. A non-idempotent suite can fail two ways, and reporting
    #: only the second leaves the implementer with no sign the first differed.
    first_attempt_output: str | None = None
    #: 402: the run timed out AFTER this job's services had started and passed
    #: their health check. Still ``env_blocked`` (only an operator can raise the
    #: budget), but a budget finding, not a hung service: coordinare must not
    #: regenerate the env cache for it.
    budget_exceeded: bool = False


# Substrings (matched case-insensitively against the failure output) that
# attribute a local test failure to the *environment* rather than the diff.
# Deliberately conservative: ambiguous signatures that frequently indicate a
# real code defect — ModuleNotFoundError / ImportError (a forgotten dependency
# in the diff IS a defect), AssertionError, SyntaxError — are EXCLUDED so a
# genuine bug is never masked as an environment block.  These cover the failure
# modes that surface only in test stdout/stderr (a refused service socket, a
# port collision, a missing toolchain binary) and which spec-088's single-shot
# env signals do not always flag.
# Deliberately NOT here: "command not found". Review was right that it is the
# shell-level twin of ModuleNotFoundError, excluded a few lines above on the
# grounds that a forgotten dependency in the diff IS a defect. An implementer
# that calls a script it did not commit, or a CLI it did not add to the
# manifest, produces exactly this string and can fix it. It is genuinely
# ambiguous -- a toolchain binary missing from the cache produces it too -- and
# the gate's stated bias settles ambiguity the same way every time: never mask a
# real defect, remote CI is the backstop. The env-cache health signal still
# catches the cache case before the gate is reached.


# A test runner reporting *failed tests* (as opposed to erroring out before it
# could run them) is decisive evidence of a code defect, and it outranks any
# environment signature found in the same output.  Without this precedence a
# signature in incidental output masks a real red test: assertion diffs and
# fixture data routinely embed environment-shaped phrases — this repo's own
# suite has 20 files containing the literal "connection refused" (e.g.
# ``assert "connection refused" in result["system_error_reason"]``), so a
# genuine failure of one of those tests prints the phrase and would otherwise
# be held as env_blocked instead of being handed back to the implementer.
#
# Deliberately matches only *failures*, never *errors*: pytest reports a fixture
# that could not reach Postgres as an ERROR ("1 error"), and that IS the
# environment case this gate exists to catch, so error counts must not suppress
# the signature.  Zero-counts ("0 failed") are excluded via the [1-9] lead.
# Matched against a runner's own summary line or per-failure header — NOT against
# exception names. A bare "AssertionError" (or pytest's "E   assert …" detail) is
# not evidence of a failed test: it is equally what a *fixture* prints when it
# asserts that a service came up, e.g. "E assert wait_for_port(5432)" →
# "AssertionError: connection refused". Treating that as a code defect bounces
# the implementer on an environment problem it cannot fix — the very failure
# spec-089 US2 exists to prevent. Every runner ci_detection can select prints a
# non-zero failure COUNT in its summary, and truncate="tail" guarantees that
# summary survives, so the counts below are both sufficient and safe.


#: CSI/OSC escape sequences. A runner told to colour its output (``--color=yes``,
#: ``FORCE_COLOR``, a project's own pytest.ini) writes "\x1b[31m1 failed\x1b[0m,"
#: -- and every pattern below reads "failed" followed by a comma or " in ". The
#: escape sits between them, so the summary becomes invisible while the env
#: signatures, which are plain substrings, still match. That combination fails in
#: the direction this gate exists to prevent: a genuinely red suite is held as an
#: environment block and the implementer never hears about its own bug.


_RETRY_MAX_FAILING = 3

#: Counts as reported by the runners ci_detection can select. Anchored the same
#: way _TEST_FAILURE_MARKERS is, so prose cannot supply a count.


def _env_blocked_gate_response(perf: Any, test_result: LocalTestResult, *, timeout_seconds: int) -> PerformerResponse:
    """402: the env_blocked response for a failed local test gate.

    A budget finding travels in the structured ``report`` so coordinare decides
    whether to regenerate the env cache from a field, not by parsing prose. The
    field is absent for a genuine environment problem, which keeps today's
    behaviour (regen) for those.
    """
    perf.state = "env_blocked"
    report = None
    if test_result.budget_exceeded:
        report = {"local_test_gate": {
            "budget_exceeded": True,
            "timeout_seconds": timeout_seconds,
            "duration_seconds": test_result.duration_seconds,
        }}
    return PerformerResponse(
        status="env_blocked",
        session_id=perf.session_id,
        reason=test_result.env_reason,
        report=report,
    )


async def _run_test_check(
    stand_path: Path, *, timeout_seconds: int = 600, label: str = "performer",
    command_override: str | None = None, sub_roots: tuple[str, ...] = (),
) -> LocalTestResult:
    """Run the detected test command in the workspace before pushing.

    Mirrors :func:`_run_ci_check`: gracefully passes through when the coordinare
    package is unavailable (standalone mode) or when no test command is detected,
    so the coordinare-side remote CI gate remains the authoritative backstop.

    ``command_override`` (409) is the operator-pinned command from
    LocalTestGateConfig.command; ``sub_roots`` are the declared monorepo roots
    probed when the workspace root itself matches no convention.
    """
    command = command_override
    stack = ""
    if not command:
        try:
            from coordinare_ci_detection import CIDetectionResult, detect
        except ImportError:
            log.info("test_check.coordinare_not_available", label=label)
            return LocalTestResult(passed=True, command=None, output="", duration_seconds=0.0)
        result = detect(stand_path, sub_roots=sub_roots)
        # 409: a monorepo root with its own convention beats the workspace
        # root, which matches no convention at all by construction here.
        if result.per_root and not result.test_command:
            for root, root_result in result.per_root.items():
                if root_result.test_command:
                    result = CIDetectionResult(
                        lint_command=root_result.lint_command,
                        test_command=f"cd {shlex.quote(root)} && {root_result.test_command}",
                        stack=root_result.stack,
                        detected_from=f"sub-root: {root}",
                    )
                    break
        if result.test_command is None:
            log.info("test_check.no_test_detected", label=label, stack=result.stack)
            return LocalTestResult(passed=True, command=None, output="", duration_seconds=0.0)
        command = result.test_command
        stack = result.stack

    log.info("test_check.running", label=label, command=command, stack=stack)
    run_result = await run_command(
        command, stand_path, timeout=timeout_seconds, truncate="tail",
    )
    if run_result.success:
        log.info(
            "test_check.passed",
            label=label,
            command=command,
            duration=run_result.duration_seconds,
        )
        return LocalTestResult(
            passed=True,
            command=command,
            output="",
            duration_seconds=run_result.duration_seconds,
            exit_code=run_result.exit_code,
        )

    # Stripped once, here, so every reader below -- the signature matcher, the
    # runner-summary precedence, the failure count, and the excerpt that reaches
    # the implementer -- sees the same plain text.
    error_output = _strip_ansi((run_result.stderr + "\n" + run_result.stdout).strip())

    blocked = _test_check_command_not_found(command, error_output, run_result, label)
    if blocked is not None:
        return blocked
    blocked = await _test_check_env_signal_block(command, error_output, run_result, label)
    if blocked is not None:
        return blocked
    blocked = await _test_check_timeout_block(
        command, error_output, run_result, timeout_seconds, label
    )
    if blocked is not None:
        return blocked
    blocked = _test_check_broad_failure(command, error_output, run_result, label)
    if blocked is not None:
        return blocked
    return await _test_check_with_retry(
        stand_path, timeout_seconds, label, command, run_result, error_output
    )


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


def _test_check_command_not_found(
    command: str,
    error_output: str,
    run_result,
    label: str,
) -> LocalTestResult | None:
    """409/352: exit 127 is an environment hold, not a code defect."""

    # 409: exit 127 is the shell saying the command name does not exist. In
    # the 167 lane that is the unambiguous "the runner is not installed"
    # environment hold (352), and this gate now agrees. The env-signature
    # matcher deliberately excludes "command not found" as a code defect (an
    # implementer that calls a script it did not commit produces the same
    # string), so without this short-circuit a detected runner missing from
    # the image was reported as a code defect and burned the self-fix budget
    # on a problem no diff can fix. The gate is the local twin of the 167
    # lane's capture_baseline and holds the same way.
    if run_result.exit_code == 127:
        log.warning(
            "test_check.command_not_found",
            label=label,
            command=command,
            output=_format_failure_excerpt(error_output),
        )
        return LocalTestResult(
            passed=False,
            command=command,
            output=error_output,
            duration_seconds=run_result.duration_seconds,
            env_blocked=True,
            env_reason=(
                f"the test command {command!r} exited 127 (command not found): the "
                "runner is not installed in this environment. No code change can "
                "fix this inside the card."
            ),
            exit_code=run_result.exit_code,
        )


    return None


async def _test_check_env_signal_block(
    command: str,
    error_output: str,
    run_result,
    label: str,
) -> LocalTestResult | None:
    """089 US2: explicit env signals drain both consumers, then the output signature."""

    # 089 US2: a failure coinciding with a spec-088 env signal is an environment
    # block, not a code defect — consult both single-shot consumers (drain both,
    # don't short-circuit) only on the failure branch so a green run never
    # spuriously env-blocks.
    from performer.workspace import (
        consume_env_cache_health_failure,
        consume_services_start_failure,
    )

    services_failure = consume_services_start_failure()
    health_failed = consume_env_cache_health_failure()
    # Tiebreaker when no explicit spec-088 signal fired: inspect the failure
    # output itself for an environment signature (refused socket, port clash,
    # missing toolchain).  Without this, an env failure that surfaces only in
    # test output is relayed to the implementer as a code defect and spins it
    # on a problem it cannot fix.
    signature = None if (services_failure or health_failed) else _match_env_signature(error_output)
    if services_failure or health_failed or signature:
        if services_failure:
            env_reason = services_failure
        elif health_failed:
            env_reason = "env-cache health probe failed before local tests"
        else:
            env_reason = _env_signature_reason(signature, error_output)
        log.warning(
            "test_check.env_blocked",
            label=label,
            command=command,
            duration=run_result.duration_seconds,
            env_reason=env_reason,
            matched_signature=signature,
        )
        return LocalTestResult(
            passed=False,
            command=command,
            output=error_output,
            duration_seconds=run_result.duration_seconds,
            env_blocked=True,
            env_reason=env_reason,
            exit_code=run_result.exit_code,
        )


    return None


async def _test_check_timeout_block(
    command: str,
    error_output: str,
    run_result,
    timeout_seconds: int,
    label: str,
) -> LocalTestResult | None:
    """A timed-out suite is held as an environment block, never retried."""
    from performer.workspace import services_healthy_this_job

    # A timed-out run is not retried: it already consumed the full
    # timeout_seconds budget, so a second attempt would double the gate's
    # worst-case wall clock (2×600s by default) for a suite that is hanging
    # rather than flaking — and a hang reproduces by nature.
    #
    # It is held as an environment block rather than handed back as a defect.
    # Review's point, and it is the stronger reading: a suite that hangs to a
    # 600s deadline is more likely waiting on an unreachable service (a wedged
    # connection with a long socket timeout is the textbook case) than failing an
    # assertion, which returns quickly. And run_command's timeout path discards
    # both streams, so error_output here is only "Command timed out after 600s" —
    # _match_env_signature has nothing to match on and cannot rescue it. The case
    # with the strongest prior for being environmental is the one where the
    # classifier is guaranteed blind, so the prior has to decide it.
    #
    # A human HOLD is also the outcome an operator wants for a hung suite: it is
    # actionable by a person and not by an implementer re-reading its own diff.
    if run_result.timed_out:
        # 402: the prior above is right when nothing is known about the
        # services and wrong every time they are known to be up. website#107
        # was held as a hung service fourteen minutes after its own log said
        # "services-health: all ok"; its suite is ~18 minutes serial against a
        # 600s budget. Healthy services + timeout is a budget finding: still a
        # hold (an operator has to raise the budget), named honestly, and
        # flagged so coordinare does not regenerate a cache that is fine.
        healthy = services_healthy_this_job()
        if healthy:
            env_reason = (
                f"the test command did not finish within {timeout_seconds}s and was killed. "
                "This job's services were started and passed their health check, so "
                "this is not a hung service: the suite's honest runtime exceeds the "
                "local test gate's budget. Raise local_test_gate.timeout_seconds for "
                "this symphony or scope the command; there is nothing to regenerate."
            )
        else:
            env_reason = (
                f"the test command did not finish within {timeout_seconds}s and was killed. "
                "A suite that hangs to its deadline is usually waiting on a service that "
                "never came up; no test output survives a timeout, so this is a held "
                "environment blocker rather than a reported defect."
            )
        log.warning(
            "test_check.timed_out",
            label=label,
            command=command,
            duration=run_result.duration_seconds,
            timeout_seconds=timeout_seconds,
            services_healthy=healthy,
        )
        return LocalTestResult(
            passed=False,
            command=command,
            output=error_output,
            duration_seconds=run_result.duration_seconds,
            env_blocked=True,
            env_reason=env_reason,
            exit_code=run_result.exit_code,
            budget_exceeded=healthy,
        )

    return None


def _test_check_broad_failure(
    command: str,
    error_output: str,
    run_result,
    label: str,
) -> LocalTestResult | None:
    """409: a broad failure is a broken diff; skip the flake retry."""

    # No env signal and no env signature: this looks like a real code failure.
    # Re-run once before bouncing the implementer — a single flaky failure
    # (timing-sensitive test, transient resource) should not cost a self-fix
    # cycle.  A second green run clears the gate; a second red run confirms it.
    #
    # But only when the suite looks flaky rather than broken. Review's point:
    # the retry was unconditional, so the *expected* outcome paid the cost of
    # the rare one — every genuine red suite ran twice in full, ten extra
    # minutes on a ten-minute suite, to recover something uncommon by
    # definition. A handful of failing tests is a plausible flake; forty is a
    # broken diff, and re-running it is pure latency before feedback the
    # implementer could already have had.
    #
    # An unparseable count still retries: that is the conservative direction,
    # since it covers runners whose summary this cannot read.
    failing = _reported_failure_count(error_output)
    if failing is not None and failing > _RETRY_MAX_FAILING:
        log.info(
            "test_check.retry_skipped_broad_failure",
            label=label,
            command=command,
            failing=failing,
            threshold=_RETRY_MAX_FAILING,
        )
        log.warning(
            "test_check.failed",
            label=label,
            command=command,
            duration=run_result.duration_seconds,
            exit_code=run_result.exit_code,
            output_preview=error_output[:200],
        )
        return LocalTestResult(
            passed=False,
            command=command,
            output=error_output,
            duration_seconds=run_result.duration_seconds,
            env_blocked=False,
            exit_code=run_result.exit_code,
        )


    return None


async def _test_check_with_retry(
    stand_path: Path,
    timeout_seconds: int,
    label: str,
    command: str,
    run_result,
    error_output: str,
) -> LocalTestResult:
    """Second attempt with env re-check and attempt-differs reporting."""

    log.info(
        "test_check.retrying",
        label=label,
        command=command,
        first_exit_code=run_result.exit_code,
        first_output_preview=error_output[:200],
    )
    retry_result = await run_command(
        command, stand_path, timeout=timeout_seconds, truncate="tail",
    )
    if retry_result.success:
        log.info(
            "test_check.flake_recovered",
            label=label,
            command=command,
            duration=retry_result.duration_seconds,
        )
        return LocalTestResult(
            passed=True,
            command=command,
            output="",
            duration_seconds=run_result.duration_seconds + retry_result.duration_seconds,
            exit_code=retry_result.exit_code,
        )

    retry_output = _strip_ansi((retry_result.stderr + "\n" + retry_result.stdout).strip())
    total_duration = run_result.duration_seconds + retry_result.duration_seconds
    # An environment signature may surface only on the retry — re-check before
    # concluding a code defect.
    retry_signature = _match_env_signature(retry_output)
    if retry_signature:
        env_reason = _env_signature_reason(retry_signature, retry_output)
        log.warning(
            "test_check.env_blocked",
            label=label,
            command=command,
            duration=total_duration,
            env_reason=env_reason,
            matched_signature=retry_signature,
            on_retry=True,
        )
        return LocalTestResult(
            passed=False,
            command=command,
            output=retry_output,
            duration_seconds=total_duration,
            env_blocked=True,
            env_reason=env_reason,
            exit_code=retry_result.exit_code,
        )

    log.warning(
        "test_check.failed",
        label=label,
        command=command,
        duration=total_duration,
        exit_code=retry_result.exit_code,
        output_preview=retry_output[:200],
    )
    # Two red attempts that read differently means a non-idempotent suite, and
    # reporting only the second hides that from whoever has to act on it.
    differed = retry_output.strip() != error_output.strip()
    if differed:
        log.info(
            "test_check.attempts_differed",
            label=label,
            command=command,
            first_preview=error_output[:200],
            second_preview=retry_output[:200],
        )
    return LocalTestResult(
        passed=False,
        command=command,
        output=retry_output,
        duration_seconds=total_duration,
        env_blocked=False,
        exit_code=retry_result.exit_code,
        retried=True,
        first_attempt_output=error_output if differed else None,
    )


def _read_feedback_dispositions(stand_path: Path) -> list[dict]:
    """126: read the implementer's per-item feedback dispositions from the
    workspace's durable ``.coordinare/feedback_dispositions.json`` contract.

    Entries are ``{"id": "fb-N", "disposition": "addressed"|"disputed",
    "reason": str}``.  A file contract survives long sessions where inline
    instructions get lost (the same reasoning as ``.coordinare/score.json``).
    Any failure (no file / parse error / wrong shape) → ``[]`` — meaning
    "nothing disputed", which the coordinare floor treats fail-open.
    """
    try:
        path = Path(stand_path) / ".coordinare" / "feedback_dispositions.json"
        if not path.is_file():
            return []
        raw = _json_module.loads(path.read_text(encoding="utf-8"))
        if not isinstance(raw, list):
            return []
        out: list[dict] = []
        for entry in raw:
            if not isinstance(entry, dict):
                continue
            fb_id = str(entry.get("id") or "")
            disp = str(entry.get("disposition") or "")
            if not fb_id or disp not in ("addressed", "disputed"):
                continue
            out.append({
                "id": fb_id,
                "disposition": disp,
                "reason": str(entry.get("reason") or "")[:500],
            })
        return out
    except Exception as exc:  # pragma: no cover - defensive
        log.info("feedback_dispositions_unreadable", error=str(exc))
        return []


def _read_declared_services(stand_path: Path) -> list[dict]:
    """101: read the symphony's durably-declared services from the cloned repo's
    ``.coordinare/score.json`` (the same source service inference uses), as plain
    dicts for the service-readiness gate. Any failure (no file / parse error) →
    ``[]`` (no declared services → the gate is a no-op, behavior unchanged).
    """
    try:
        score = Path(stand_path) / ".coordinare" / "score.json"
        if not score.is_file():
            return []
        from coordinare_service_inference.schema import ServicesManifest

        manifest = ServicesManifest.model_validate_json(score.read_text(encoding="utf-8"))
        return [svc.model_dump() for svc in manifest.services]
    except Exception as exc:  # pragma: no cover - defensive
        log.info("env_bootstrap.declared_services_unreadable", error=str(exc))
        return []


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

    from coordinare_service_inference.manual_override import (
        apply_manual_override,
    )

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
    cfg = _service_inference_config()

    client, skip = _service_inference_client(cfg)
    if skip is not None:
        return skip

    return await _service_inference_run(cfg, client, stand_path, output_root)


def _service_inference_env(key: str) -> str:
    """826: expandvars-leak guard — treat a literal ``${VAR}`` as unset."""
    val = os.environ.get(key, "")
    if val.startswith("${") and val.endswith("}"):
        return ""
    return val


def _service_inference_config() -> dict[str, object]:
    """Resolve inference env overrides into a provider/client config dict."""
    from coordinare_service_inference.prompt import render_system_prompt

    provider = (_service_inference_env("COORDINARE_INFERENCE_PROVIDER") or "anthropic").strip().lower()

    agent_version = _service_inference_env("COORDINARE_INFERENCE_AGENT_VERSION") or DEFAULT_INFERENCE_AGENT_VERSION
    model = _service_inference_env("COORDINARE_INFERENCE_MODEL") or DEFAULT_INFERENCE_MODEL
    max_tokens_raw = _service_inference_env("COORDINARE_INFERENCE_MAX_TOKENS")
    try:
        max_tokens = int(max_tokens_raw) if max_tokens_raw else DEFAULT_INFERENCE_MAX_TOKENS
    except ValueError:
        log.warning(
            "service_inference.invalid_max_tokens_override",
            value=max_tokens_raw,
            fallback=DEFAULT_INFERENCE_MAX_TOKENS,
        )
        max_tokens = DEFAULT_INFERENCE_MAX_TOKENS
    max_tool_calls_raw = _service_inference_env("COORDINARE_INFERENCE_MAX_TOOL_CALLS")
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
    return {
        "provider": provider,
        "agent_version": agent_version,
        "model": model,
        "max_tokens": max_tokens,
        "max_tool_calls": max_tool_calls,
        "system_prompt": system_prompt,
    }


def _service_inference_client(
    cfg: dict[str, object],
) -> tuple[object, dict[str, object] | None]:
    """867: build the provider client. Returns (client, None) or (None, skip-dict)."""
    from coordinare_service_inference.claude_llm_client import (
        ClaudeServiceLLMClient,
    )

    provider = str(cfg["provider"])
    agent_version = str(cfg["agent_version"])
    model = str(cfg["model"])
    max_tokens = int(cfg["max_tokens"])  # type: ignore[arg-type]
    system_prompt = str(cfg["system_prompt"])
    if provider == "openai_compat":
        base_url = _service_inference_env("COORDINARE_INFERENCE_BASE_URL").strip()
        if not base_url:
            # Fail fast — silently falling back to Anthropic would leak traffic
            # to a provider the operator explicitly opted out of.
            log.warning("service_inference.openai_compat_missing_base_url")
            return None, {
                "inference_skipped_reason": "openai_compat_missing_base_url",
                "inference_agent_version": agent_version,
            }
        compat_api_key = _service_inference_env("COORDINARE_INFERENCE_API_KEY") or None
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
            return None, {"inference_skipped_reason": "no_api_key"}
        client = ClaudeServiceLLMClient.from_api_key(
            api_key=api_key,
            model=model,
            max_tokens=max_tokens,
            system_prompt=system_prompt,
        )
    else:
        log.warning("service_inference.unknown_provider", provider=provider)
        return None, {
            "inference_skipped_reason": f"unknown_provider:{provider}",
            "inference_agent_version": agent_version,
        }
    return client, None


async def _service_inference_run(
    cfg: dict[str, object],
    client: object,
    stand_path: Path,
    output_root: Path,
) -> dict[str, object]:
    """906: run inference; map every outcome onto response keys."""
    from coordinare_service_inference import (
        InferenceFailed,
        infer_services,
    )

    agent_version = str(cfg["agent_version"])
    max_tool_calls = int(cfg["max_tool_calls"])  # type: ignore[arg-type]
    step_timeout_raw = _service_inference_env("COORDINARE_INFERENCE_STEP_TIMEOUT")
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
        # Spec 092 US2: surface the agent-discovered test-env source PATH (never
        # values) so the coordinare can persist it and reload the same file for
        # later QA-runtime and performer contexts.
        "inference_test_env_source": result.manifest.test_env_source,
    }


def _extract_json(
    text: str, *, object_pairs_hook: Any = None,
    candidate_callback: Callable[[str, int, int], None] | None = None,
) -> dict | list | None:
    """Try to extract a JSON object from text that may contain prose.

    Strategies: (1) parse full text, (2) find ```json``` code fence,
    (3) find first { ... } or [ ... ] substring.
    """
    candidates = [("full", 0, len(text))]
    match = _CODE_FENCE_RE.search(text)
    if match:
        candidates.append(("fence", match.start(1), match.end(1)))
    for start_char, end_char in [('{', '}'), ('[', ']')]:
        start, end = text.find(start_char), text.rfind(end_char)
        if start >= 0 and end > start:
            candidates.append(("substring", start, end + 1))
    for kind, start, end in candidates:
        candidate = text[start:end]
        if kind == "fence":
            candidate = candidate.strip()
        try:
            value = _json_module.loads(candidate, object_pairs_hook=object_pairs_hook)
        except (ValueError, TypeError):
            continue
        if candidate_callback is not None:
            candidate_callback(kind, start, end)
        return value
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
    {"implementing", "reviewing", "security", "qa", "documenting"},
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




def _looks_like_url(value: str) -> bool:
    """Return True for HTTP(S) URLs."""
    return value.startswith(("http://", "https://"))







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
        origin="performer", role=r, display=display, harness=harness, model=model,
    )










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
        return await _parse_failure_retry_response(
            perf, stage_label, failure_reason, redacted_short,
            max_retries, is_json_contract_role,
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


async def _parse_failure_retry_response(
    perf: Performance,
    stage_label: str,
    failure_reason: str,
    redacted_short: str,
    max_retries: int,
    is_json_contract_role: bool,
) -> PerformerResponse:
    """(045) Schedule a parse-retry: repair prompt for JSON roles, else restart."""
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
                ),
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

def _backfill_terminal_failure_reason(resp: PerformerResponse) -> PerformerResponse:
    """Guarantee a terminal FAILURE response carries an operator-visible reason.

    The performer serialises its summary with ``model_dump_json(exclude_none=True)``,
    so a ``None`` ``reason`` is dropped entirely — collapsing the coordinare's
    diagnostics to a bare status token (and ultimately a silent card-block). For
    any failing status with no explicit reason, backfill from the next-best
    diagnostic field so every failure summary explains itself, regardless of
    which role return produced it. Non-failure or already-reasoned responses are
    returned unchanged.
    """
    if resp.status not in FAILURE_STATUSES or resp.reason:
        return resp
    fallback_reason = (
        resp.inference_skipped_reason
        or f"performer returned terminal status '{resp.status}' with no reason"
    )
    return resp.model_copy(update={"reason": fallback_reason})


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

    # 036/151: Apply the GitHub REST + GraphQL URL overrides from the dispatch so
    # the performer's client connects to the same instance (GitHub Enterprise) or,
    # in the board-sim bench, the loopback fake. https-any / http-loopback-only.
    from performer.config import apply_github_url_override
    apply_github_url_override(score.github_api_url, "GITHUB_API_URL", settings)
    apply_github_url_override(score.github_graphql_url, "GITHUB_GRAPHQL_URL", settings)

    stand: Stand = await clone_repository(score)
    orchestration_proxy = await _launch_dispatch_proxy(
        score, backend_name, model_name, stand, settings,
    )
    try:
        # 164: a role with a configured workflow runs that workflow, presented
        # as a BackendAdapter so every downstream mechanism (Performance, the
        # monitor loop, role post-processing, cleanup) is unchanged. An absent
        # or empty name takes the pre-164 single-backend path exactly (FR-005).
        workflow_name = (getattr(score, "workflow", "") or "").strip()
        if workflow_name:
            from performer.workflows.adapter import WorkflowAdapter

            backend = WorkflowAdapter(workflow_name, gateway=(
                settings.LITELLM_PROXY_BASE_URL or "", settings.LITELLM_PROXY_AUTH_TOKEN or "",
            ))
        else:
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
    await _snapshot_pr_comment_baseline(perf, pr_url, score)
    log.info("dispatch accepted", session_id=session_id)
    return PerformerResponse(
        status="accepted",
        session_id=session_id,
        backend=backend_name,
        model=model_name,
    ), perf


async def _launch_dispatch_proxy(
    score: Score,
    backend_name: str,
    model_name: str | None,
    stand: Stand,
    settings: Settings,
) -> object | None:
    """080/078: launch the in-container dual-model proxy before the CLI starts.

    No-op (returns None) when the dispatch carries no orchestration block.
    On failure the stand is cleaned up and the error re-raised.
    """
    orchestration_proxy = None
    try:
        from performer.proxy.launch import maybe_launch_proxy
        from performer.proxy.routing import RoutingTable

        # 078: load the self-hosted routing table from its mounted/baked YAML
        # path (SELFHOSTED_ROUTING_CONFIG). When the path is empty (the default)
        # no table is loaded, so the layer stays a byte-for-byte no-op for every
        # backend. A non-empty-but-broken table fails fast here at job start.
        routing_table = None
        routing_config_path = (settings.SELFHOSTED_ROUTING_CONFIG or "").strip()
        if routing_config_path:
            routing_table = RoutingTable.from_yaml_file(routing_config_path)

        orchestration_proxy = await maybe_launch_proxy(
            score.orchestration,
            backend_name,
            routing_table=routing_table,
            model=model_name,
            health_check=routing_table is not None,
            health_timeout=settings.SELFHOSTED_HEALTH_TIMEOUT,
            capture_dir=(settings.LITELLM_PROXY_CAPTURE_DIR or "").strip() or None,
        )
    except Exception as exc:
        log.warning("dual_model_proxy.launch_failed", error=str(exc))
        cleanup_stand(stand)
        raise
    return orchestration_proxy


async def _snapshot_pr_comment_baseline(
    perf: Performance,
    pr_url: str | None,
    score: Score,
) -> None:
    """072: snapshot pre-turn PR comment count for bot_pr_comment_delta."""
    if not pr_url:
        return
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
                owner, repo, int(job_id), token, max_chars=slice_size,
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
            owner, repo, perf.pr_head_sha, perf.score.effective_github_token,
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
            # 511: the coordinare's artefact write-through needs the branch
            # name; it is always known here and never derived from git state.
            pushed_branch=perf.stand.branch,
            # 126: the implementer's per-item feedback dispositions ride the
            # terminal success so the coordinare floor can adjudicate them.
            feedback_dispositions=_read_feedback_dispositions(perf.stand.path),
        )

    if verdict == "pending":
        return PerformerResponse(
            status="working",
            session_id=perf.session_id,
            progress="CI checks in progress...",
        )

    # Infrastructure failures never enter the model repair/no-progress loop.
    from performer.infrastructure import check_infrastructure_reason

    if cause := check_infrastructure_reason(failed):
        perf.state = "env_blocked"
        return PerformerResponse(status="env_blocked", session_id=perf.session_id, reason=cause,
                                 pr_url=perf.pr_url, pr_node_id=perf.pr_node_id,
                                 report={"ci_infrastructure": {"head_sha": perf.pr_head_sha,
                                         "check_names": [str(run.get("name") or "check") for run in failed], "cause": cause}})

    # verdict == "fail"
    return await _dispatch_check_failure_repair(perf, settings, failed, owner, repo)


async def _dispatch_check_failure_repair(
    perf: Performance,
    settings: Settings | None,
    failed: list[dict[str, Any]],
    owner: str,
    repo: str,
) -> PerformerResponse:
    """Fail path: no-progress/budget gates, then relay the failure to the backend."""
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
            f"CI checks failed with no progress across {perf.check_no_progress_streak + 1} attempts: {names}",
        ]
        perf.state = "blocked"
        perf.open_questions = questions
        return PerformerResponse(
            status="blocked",
            session_id=perf.session_id,
            questions=questions,
            reason=questions[0],
        )

    if perf.check_attempt >= max_attempts:
        questions = [f"CI checks failed after {perf.check_attempt} fix attempt(s): {names}"]
        perf.state = "blocked"
        perf.open_questions = questions
        return PerformerResponse(
            status="blocked",
            session_id=perf.session_id,
            questions=questions,
            reason=questions[0],
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
        f"CI checks failed. Fix the following:\n\n{failure_msg}{tool_hint}",
    )
    perf.state = "working"
    return PerformerResponse(
        status="working",
        session_id=perf.session_id,
        progress=f"Fixing CI failures (attempt {perf.check_attempt})...",
    )


_DOC_ROOT_FILES = frozenset({"AGENTS.md", "CLAUDE.md", "CHANGELOG.md", "README.md"})
_MAX_DOC_PAGE_CHARS = 40_000  # cap on current-page content inlined into a write prompt


def _safe_doc_page_path(path: object) -> bool:
    """124(C): a page path from the MODEL's plan is untrusted. Accept only
    repo-relative documenter targets — under ``docs/`` or a known root doc file —
    and reject absolute paths / ``..`` traversal, so a hallucinated or malicious
    path can never read or overwrite files outside the documenter's surface."""
    if not isinstance(path, str) or not path:
        return False
    if os.path.isabs(path) or ".." in Path(path).parts:
        return False
    parts = Path(path).parts
    if parts and parts[0] == "docs":
        return True
    return path in _DOC_ROOT_FILES


def _doc_write_dispatch_error(
    perf: Performance, page: dict, exc: Exception,
) -> PerformerResponse:
    """124(C, review): a per-page write dispatch (backend.start) failed. Fail the
    documenting job fast with a clean terminal error + consistent perf state,
    rather than propagating an exception out of handle_status (which would leave
    the job half-set at doc_phase='writing' with no status response)."""
    log.error("start_doc_write_failed", page=(page or {}).get("path"), error=str(exc))
    perf.state = "error"
    perf.error_reason = (
        f"Failed to dispatch documentation page write "
        f"({(page or {}).get('path')}): {exc}"
    )
    return PerformerResponse(
        status="error", session_id=perf.session_id, reason=perf.error_reason,
    )


async def _start_doc_write(
    perf: Performance, page: dict, settings: Settings | None,
) -> None:
    """124(C): dispatch a fresh, minimal single-page WRITE run on the same backend.

    The write Score carries ``doc_write_target`` (routing hermes to a small,
    diff-free per-page prompt) plus the page's CURRENT content so the model
    updates rather than reinvents it. Reuses the dispatched model + token budget.
    Because ``start()`` resets the backend status to running, the poll loop simply
    revisits this handler when the write completes.
    """
    current = ""
    rel = page["path"]
    try:
        base = perf.stand.path.resolve()
        target = (perf.stand.path / rel).resolve()
        # Containment (defense in depth — the queue is already path-filtered):
        # never read outside the checkout, even via a residual traversal/symlink.
        if target.is_relative_to(base) and target.is_file():
            current = target.read_text(encoding="utf-8", errors="replace")[:_MAX_DOC_PAGE_CHARS]
    except (OSError, ValueError):
        current = ""
    write_score = perf.score.model_copy(update={
        "doc_write_target": {
            "path": rel, "intent": page.get("intent", ""), "current": current,
        },
        "pr_diff": "",  # keep the per-page context small — the whole point of C
    })
    await perf.backend.start(
        perf.stand, write_score,
        model=perf.score.model or None,
        effort=perf.score.effort or None,
        temperature=perf.score.temperature,
        max_tokens=perf.score.max_tokens,
    )


async def _commit_doc_batch(perf: Performance) -> PerformerResponse:
    """124(C): batch-commit the accumulated page writes + planned deletions in one
    commit, then return the terminal ``docs_committed`` (or an error)."""
    issue_num = perf.score.issue_number
    batch_msg = (
        f"docs(#{issue_num}): update wiki and card documentation"
        if issue_num else "docs: update wiki and card documentation"
    )
    valid_files = [
        f for f in perf.doc_files_pending
        if isinstance(f, dict) and f.get("path") and isinstance(f.get("content"), str)
    ]
    if valid_files or perf.doc_deletions:
        try:
            committed = await commit_files(
                perf.stand, valid_files, batch_msg, deletions=perf.doc_deletions,
                score=perf.score,  # 165: a documenter side run is held to its tree
            )
            perf.docs_files_modified.extend(committed)
        except Exception as exc:
            log.error("docs_batch_commit_failed", error=str(exc))
            perf.state = "error"
            perf.error_reason = f"Failed to batch-commit doc files: {exc}"
            return PerformerResponse(
                status="error", session_id=perf.session_id, reason=perf.error_reason,
            )
    perf.state = "docs_committed"
    # 124(US2): the symphony-init dispatch (doc_mode="init") is CARDLESS — there is
    # no existing PR, so open a seed PR here (commit_files already pushed the
    # branch) and hand its node id back so the coordinare's WikiInitService can
    # auto-merge it. Best-effort: a PR-open failure still returns docs_committed
    # (the branch carries the wiki); the operator can open the PR manually.
    pr_url = pr_node_id = None
    if getattr(perf.score, "doc_mode", "update") == "init" and perf.docs_files_modified:
        try:
            owner, repo = perf.score.owner_repo
            pr_url, pr_node_id = await create_pull_request(
                owner, repo, perf.score, perf.stand.branch,
                perf.score.effective_github_token,
            )
            perf.pr_url, perf.pr_node_id = pr_url, pr_node_id
        except Exception as exc:  # noqa: BLE001 — never fail the doc commit on PR-open
            log.error("wiki_init.pr_open_failed", error=str(exc))
    return PerformerResponse(
        status="docs_committed", session_id=perf.session_id,
        files_modified=perf.docs_files_modified, pr_url=pr_url, pr_node_id=pr_node_id,
    )


async def _advance_doc_writes(
    perf: Performance, docs_raw: str, settings: Settings | None,
) -> PerformerResponse:
    """124(C): a per-page WRITE run finished — accumulate its file, then dispatch
    the next queued page or batch-commit when the queue drains. A page whose
    output can't be parsed is logged and skipped (the rest still commit) rather
    than failing the whole documenting job."""
    parsed = _extract_json(docs_raw) if isinstance(docs_raw, str) else docs_raw
    entry: list[dict] = []
    if isinstance(parsed, dict):
        files = parsed.get("files")
        if isinstance(files, list):
            entry = [
                f for f in files
                if isinstance(f, dict) and f.get("path") and isinstance(f.get("content"), str)
            ]
        elif parsed.get("path") and isinstance(parsed.get("content"), str):
            entry = [{"path": parsed["path"], "content": parsed["content"]}]
    current_page = perf.doc_write_queue[0]["path"] if perf.doc_write_queue else "?"
    if entry:
        perf.doc_files_pending.extend(entry)
    else:
        log.warning("docs.write_page_unparsed", page=current_page)
    if perf.doc_write_queue:
        perf.doc_write_queue.pop(0)
    if perf.doc_write_queue:
        nxt = perf.doc_write_queue[0]
        perf.parse_retry_count = 0
        try:
            await _start_doc_write(perf, nxt, settings)
        except Exception as exc:
            return _doc_write_dispatch_error(perf, nxt, exc)
        return PerformerResponse(
            status="working", session_id=perf.session_id,
            progress=f"Documenting: writing {nxt['path']}",
        )
    return await _commit_doc_batch(perf)


async def handle_status(
    msg: PerformerMessage,
    perf: Performance | None,
    settings: Settings | None = None,
) -> PerformerResponse:
    """Return the current session state."""
    _guard = await _status_session_guard(msg, perf, settings)
    if _guard is not None:
        return _guard

    # Return a stable response for terminal/parked states without re-running
    # backend logic.  Without this guard, a coordinare poll arriving after
    # _poll_check_runs sets perf.state = "blocked" would fall through to
    # backend.get_status(), see "done", and re-execute the push/PR-open path.
    _settled = await _terminal_status_response(perf, settings)
    if _settled is not None:
        return _settled

    backend_status: BackendStatus = perf.backend.get_status()

    if backend_status.state == "done":
        # 023: QA performer path — validate acceptance criteria, commit new tests, pass or fail.
        if perf.role == "qa":
            # 164 T049: QA post-processing lives in performer.qa_postprocess.
            # Late import: that module looks collaborators up on this one.
            from performer.qa_postprocess import finalize_qa

            return await finalize_qa(perf, backend_status, settings)

        _dispatched = await _role_dispatch_response(perf, backend_status, settings)
        if _dispatched is not None:
            return _dispatched

        _lint_response = await _pre_push_lint_response(perf)
        if _lint_response is not None:
            return _lint_response

        gate_cfg = perf.score.local_test_gate
        if gate_cfg and gate_cfg.get("enabled"):
            _gate_response = await _local_test_gate_response(perf, gate_cfg)
            if _gate_response is not None:
                return _gate_response

        return await _push_and_open_pr(perf)

    return await _non_done_response(perf, backend_status)


async def _status_session_guard(
    msg: PerformerMessage,
    perf: Performance | None,
    settings: Settings | None,
) -> PerformerResponse | None:
    """Reject stale/foreign sessions and enforce the FR-015 wall-clock timeout."""
    if perf is None or msg.session_id != perf.session_id:
        # 412 round 46: a stale request while another session is still being
        # served is an error for that request only -- the run loop keeps
        # serving the active session, so the transport must not clear and
        # reap the live process. active_session separates the no-session
        # exit (process-exiting) from the mismatch (not).
        return PerformerResponse(
            status="session_expired",
            session_id=msg.session_id,
            reason="No active performance with that session ID",
            active_session=perf is not None,
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
    return None


async def _role_dispatch_response(
    perf: Performance,
    backend_status: BackendStatus,
    settings: Settings | None,
) -> PerformerResponse | None:
    """Dispatch a done state to the role path; None falls through to the shared push tail."""
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

    if perf.role == "architecting":
        return await architect_path(perf, backend_status, settings)

    if perf.role == "assessing":
        return await assessor_path(perf, backend_status, settings)

    # 021/173: the intake/reviewer roles map a verdict and return terminal
    # statuses. This branch MUST come before the shared tail below: that tail
    # lints, pushes the branch and opens a pull request for any role that
    # reaches it, and these roles clone a repository they never modify. A
    # missing branch here does not fail loudly, it opens pull requests.
    if perf.role in ("advocate", "curator"):
        return await intake_path(perf, backend_status, settings)
    if perf.role == "closing_review":
        _closing_response = await closing_record_path(perf, backend_status, settings)
        if _closing_response is not None:
            return _closing_response
    if perf.role in ("reviewing", "closing_review"):
        return await reviewer_path(perf, backend_status, settings)

    if perf.role == "security":
        return await security_path(perf, backend_status, settings)

    if perf.role == "documenting":
        return await documenting_path(perf, backend_status, settings)

    if perf.role == "env_bootstrap":
        return await bootstrap_path(perf, backend_status, settings)

    if perf.role == "implementing":
        _impl_response = await implementing_path(perf, backend_status, settings)
        if _impl_response is not None:
            return _impl_response

    if perf.role in SENTINEL_ROLES:
        _sentinel_response = await sentinel_path(perf, backend_status, settings)
        if _sentinel_response is not None:
            return _sentinel_response

    return None


async def _pre_push_lint_response(perf: Performance) -> PerformerResponse | None:
    """043/409: run the lint gate before pushing; None when the gate passes."""
    lint_gate_cfg = perf.score.local_test_gate or {}
    ci_ok, ci_error = await _run_ci_check(
        perf.stand.path,
        label=perf.role,
        lint_override=str(lint_gate_cfg["lint_command"]) if lint_gate_cfg.get("lint_command") else None,
        sub_roots=tuple(lint_gate_cfg.get("roots") or ()),
    )
    if not ci_ok:
        log.warning("pre_push_ci_failed", role=perf.role, error_preview=ci_error[:200])
        perf.state = "changes_requested"
        perf.review_comments = [{"body": f"Lint failed before push:\n{ci_error[:500]}"}]
        return PerformerResponse(
            status="changes_requested",
            session_id=perf.session_id,
            comments=[{"body": f"Lint failed before push:\n{ci_error[:500]}"}],
        )
    return None


async def _local_test_gate_response(
    perf: Performance,
    gate_cfg: dict[str, Any],
) -> PerformerResponse | None:
    """089 US1/US2/US3: local test gate before push; None when the gate passes."""
    # 089 US1: implementer local test gate — run the detected test command
    # before pushing so code that fails its own tests never reaches the
    # remote CI gate (a cheap pre-filter; local pass ≠ remote pass).  Opt-in
    # and coordinare-delivered via score.local_test_gate; when absent or
    # disabled the gate is dormant and this path is byte-identical (SC-005).
    # The config is delivered only on implementer dispatches (the coordinare
    # injects card_context["local_test_gate"] solely for role == implementer),
    # and this default push path is reached only by the implementer/default
    # flow — every other role returns earlier — so config presence alone is a
    # sufficient and robust gate without a brittle role-string comparison.
    # 409: the operator's pinned command and monorepo roots ride the
    # same gate config; both default to detection-only behaviour.
    test_result = await _run_test_check(
        perf.stand.path,
        timeout_seconds=int(gate_cfg.get("timeout_seconds", 600)),
        label=perf.role,
        command_override=str(gate_cfg["command"]) if gate_cfg.get("command") else None,
        sub_roots=tuple(gate_cfg.get("roots") or ()),
    )
    if test_result.env_blocked:
        # 089 US2: an env-cache signal coincided with the failure — hold
        # the card on the same stage (env-blocked, like qa_env_blocked),
        # never re-dispatch the agent to fix fine code, never push.
        log.warning(
            "pre_push_local_tests_env_blocked",
            role=perf.role,
            command=test_result.command,
            env_reason=test_result.env_reason,
            budget_exceeded=test_result.budget_exceeded,
        )
        return _env_blocked_gate_response(
            perf, test_result,
            timeout_seconds=int(gate_cfg.get("timeout_seconds", 600)),
        )
    if not test_result.passed:
        log.warning(
            "pre_push_local_tests_failed",
            role=perf.role,
            command=test_result.command,
            output_preview=test_result.output[:200],
        )
        cmd_label = test_result.command or "(unknown command)"
        code_label = (
            ""
            if test_result.exit_code is None
            else f" (exit code {test_result.exit_code})"
        )
        excerpt = _format_failure_excerpt(test_result.output)
        body = (
            f"Local tests failed before push.\n"
            f"Command: `{cmd_label}`{code_label}\n\n"
            f"```\n{excerpt}\n```"
        )
        perf.state = "changes_requested"
        perf.review_comments = [{"body": body}]
        # 089 US3: carry the current HEAD so the coordinare can key the
        # per-head local_fix_counter and reset the self-fix budget when
        # the agent commits a fix (a new HEAD SHA).
        _head_after = await get_head_sha(perf.stand)
        return PerformerResponse(
            status="changes_requested",
            session_id=perf.session_id,
            comments=[{"body": body}],
            local_test_failed=True,
            head_after=_head_after,
        )
    return None


async def _push_and_open_pr(perf: Performance) -> PerformerResponse:
    """Default path: push the branch and open the pull request."""
    owner, repo = perf.score.owner_repo
    await push_branch(perf.stand, perf.score)
    pr_url, pr_node_id = await create_pull_request(
        owner, repo, perf.score, perf.stand.branch, perf.score.effective_github_token,
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


async def _non_done_response(
    perf: Performance,
    backend_status: BackendStatus,
) -> PerformerResponse:
    """Blocked / error / working tails for a backend that has not finished."""
    # 509: terminal responses must carry whatever is still buffered (e.g. the
    # relay-feedback delivery event emitted at start() before _launch) --
    # run-loop cleanup discards the buffer right after the terminal response,
    # so a fast failure would strand it and the session record would show
    # zero events again.
    terminal_events: list[dict] | None = None
    if backend_status.state != "working":
        terminal_events = [e.model_dump() for e in perf.backend.drain_events()]
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
            events=terminal_events,
        )

    if backend_status.state == "error":
        perf.state = "error"
        perf.error_reason = backend_status.error_reason
        if backend_status.stop_reason == "max_tokens":
            return PerformerResponse(
                status="token_limit",
                session_id=perf.session_id,
                reason=backend_status.error_reason,
                events=terminal_events,
            )
        return PerformerResponse(
            status="error",
            session_id=perf.session_id,
            reason=backend_status.error_reason,
            events=terminal_events,
        )

    # working — attach metrics and drain buffered events
    metrics = collect_metrics(perf.backend, backend_status)
    events = [e.model_dump() for e in perf.backend.drain_events()]
    # 343: a workflow-backed session self-describes its step sequence. Read via
    # getattr so every non-workflow backend keeps working untouched.
    _steps = getattr(perf.backend, "workflow_steps", None)
    return PerformerResponse(
        status="working",
        session_id=perf.session_id,
        progress=backend_status.progress,
        metrics=metrics,
        events=events,
        workflow_steps=list(_steps) if _steps else None,
    )


async def handle_relay_feedback(
    msg: PerformerMessage,
    perf: Performance | None,
) -> PerformerResponse:
    """Deliver feedback to the running backend."""
    if perf is None or msg.session_id != perf.session_id:
        # 412 round 46: see handle_status -- active_session marks the
        # mismatch case so the transport keeps the live process.
        return PerformerResponse(
            status="session_expired",
            session_id=msg.session_id,
            reason="No active performance with that session ID",
            active_session=perf is not None,
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
            await _watchdog_fired(perf, settings)
            break
        except Exception:  # pragma: no cover
            break

        line = raw.decode(errors="replace").strip()
        if not line:
            continue

        msg, invalid = _parse_message(line)
        if invalid is not None:
            _write_response(invalid)
            continue

        try:
            if msg.action == "health":
                resp = handle_health(settings)

            elif msg.action == "dispatch":
                # Note: no per-dispatch timeout here — AGENT_TIMEOUT is the
                # end-to-end session budget enforced by the watchdog above.
                # A separate clone/setup timeout lives inside _run_git().
                resp, new_perf = await _dispatch_action(msg, settings, perf)
                if new_perf is not None:
                    perf = new_perf

            elif msg.action == "status":
                resp = await _status_action(msg, settings, perf)

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

        if _loop_should_exit(resp, msg, perf):
            break

    # Cleanup regardless of outcome
    if perf is not None:
        try:
            await perf.backend.stop()
        except Exception:  # pragma: no cover
            pass
        await _stop_orchestration_proxy(perf)
        cleanup_stand(perf.stand)


def _parse_message(
    line: str,
) -> tuple[PerformerMessage, None] | tuple[None, PerformerResponse]:
    """Validate a wire line; (msg, None) on success, (None, error-response) on bad JSON."""
    try:
        return PerformerMessage.model_validate_json(line), None
    except Exception as exc:
        # Log exception type only — exc text may contain sensitive input values
        # (e.g. github_token surfaced by pydantic ValidationError).
        log.error("invalid message", exc_type=type(exc).__name__)
        return None, PerformerResponse(
            status="error", reason=f"invalid message: {type(exc).__name__}",
        )


def _loop_should_exit(
    resp: PerformerResponse,
    msg: PerformerMessage,
    perf: Performance | None,
) -> bool:
    """Terminal states exit the loop. 412 round 34: "blocked" joins the break
    set -- the transport treats it as process-exiting (the session is capped
    and the next dispatch is fresh), so the loop must actually exit and run
    the graceful cleanup below instead of idling until the transport's SIGTERM
    reaps it uncleanly."""
    if resp.status in ("pr_opened", "plan_committed", "approved", "nothing_to_review", "changes_requested", "security_passed", "nothing_to_scan", "not_applicable", "security_failed", "qa_passed", "qa_failed", "qa_env_blocked", "env_blocked", "docs_committed", "error", "blocked") and msg.action != "health":
        return True
    # session_expired with no active session means nothing will ever start
    return resp.status == "session_expired" and perf is None


async def _watchdog_fired(perf: Performance | None, settings: Settings) -> None:
    """Watchdog trip: park a mid-flight session as error and stop its backend."""
    if perf is not None and perf.state not in ("pr_opened", "plan_committed", "approved", "nothing_to_review", "changes_requested", "security_passed", "nothing_to_scan", "not_applicable", "security_failed", "qa_passed", "qa_failed", "qa_env_blocked", "env_blocked", "docs_committed", "error"):
        perf.state = "error"
        perf.error_reason = (
            f"watchdog: session exceeded {settings.AGENT_TIMEOUT:.0f}s"
        )
        try:
            await perf.backend.stop()
        except Exception:  # pragma: no cover
            pass


async def _dispatch_action(
    msg: PerformerMessage,
    settings: Settings,
    perf: Performance | None,
) -> tuple[PerformerResponse, Performance | None]:
    """2518: dispatch — clone the repo, start the backend, map setup failures.

    Returns the previous ``perf`` unchanged on error so run_loop keeps the
    stale-session semantics of the original inline branch.
    """
    try:
        return await handle_dispatch(msg, settings)
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
            sorted({str(e["loc"][0]) for e in exc.errors() if e["loc"]}),
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
    return resp, perf


async def _status_action(
    msg: PerformerMessage,
    settings: Settings,
    perf: Performance | None,
) -> PerformerResponse:
    """2551: status — refresh the GitHub token if fresh, then handle_status."""
    # Refresh GitHub token if the coordinare sent a fresh one
    refreshed_token = msg.payload.get("github_token")
    if refreshed_token and perf is not None and perf.score is not None:
        perf.score.github_token = refreshed_token
        perf.stand.git_env = _git_credential_vars(refreshed_token)
        log.debug("token_refreshed", session_id=perf.session_id)
    try:
        return await handle_status(msg, perf, settings)
    except (BranchConflictError, GitHubAPIError, WorkspaceSetupError) as exc:
        if perf:
            perf.state = "error"
            perf.error_reason = str(exc)
        return PerformerResponse(
            status="error",
            session_id=msg.session_id,
            reason=str(exc),
        )


def _write_response(resp: PerformerResponse) -> None:  # pragma: no cover
    # Spec 063 Phase 4 (T023): drain the env-cache health-failure flag set by
    # workspace._run_env_cache_health_check, so the very next outbound response
    # carries the signal even when callers built the response without it.
    from performer.workspace import consume_env_cache_health_failure
    if consume_env_cache_health_failure():
        resp = resp.model_copy(update={"env_cache_health_failed": True})
    sys.stdout.write(resp.model_dump_json(exclude_none=True) + "\n")
    sys.stdout.flush()


async def _poll_job_status(
    perf: Performance,
    settings: Settings | None,
) -> PerformerResponse:
    """2710: poll handle_status to a terminal response, then stop the backend.

    US5 / Spec 073 Phase 9: pick up secrets PATCHed mid-job by the coordinare
    (refreshed GitHub App token before the 1h expiry boundary), mirroring the
    stdio `action == "status"` refresh.
    """
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
                # A backend that only learns its token count at the terminal
                # result event (claude_code) never reports it on a `working`
                # response, so stamp metrics on the terminal response — this is
                # what coordinare's check_status returns (JobResult.summary).
                if resp.metrics is None:
                    resp = resp.model_copy(update={"metrics": collect_metrics(perf.backend)})
                break
    finally:
        try:
            await perf.backend.stop()
        except Exception:
            pass
        await _stop_orchestration_proxy(perf)
        cleanup_stand(perf.stand)
    return resp


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
                    f"dispatch failed: {type(exc).__name__}: {exc}",
                ),
                error_code="dispatch_error",
            )

        resp = await _poll_job_status(perf, settings)

        if resp is None:
            return JobResult(success=False, summary="no status response", error_code="internal_error")

        # Spec 063 Phase 4 (T023): drain env-cache health-failure flag into
        # the final HTTP-job summary so coordinare's check_status can route it.
        from performer.workspace import consume_env_cache_health_failure
        if consume_env_cache_health_failure():
            resp = resp.model_copy(update={"env_cache_health_failed": True})

        success = resp.status not in FAILURE_STATUSES
        # Terminal FAILURE with no explicit reason would lose its only diagnostic
        # when exclude_none drops `reason`; backfill before serialising.
        resp = _backfill_terminal_failure_reason(resp)
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
