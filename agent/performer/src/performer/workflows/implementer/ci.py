"""CI wait of the implementer workflow (spec 167 FR-013, FR-014, FR-015).

Poll the PR head's check runs until they conclude or the wait budget runs
out. A failing check is a repair turn carrying the failing job's log excerpt,
then the milestone tests (a regression routes back to a green check), then
the quality set, a push and another poll. At most ``budgets.ci_repairs``
repairs, and the same set of failing check names on two consecutive polls is
no progress and stops early. Checks pending past the wait budget are an
environment hold, not a code failure.
"""
from __future__ import annotations

import time

import structlog

from performer.infrastructure import CIInfrastructureBlocked, check_infrastructure_reason
from performer.workflows.implementer.cycle import no_progress_check
from performer.workflows.implementer.driver import RunContext
from performer.workflows.implementer.models import CIAttempt
from performer.workflows.implementer.quality import _run_set, repair_turn

log = structlog.get_logger(__name__)

__all__ = ["CIPending", "CIFailed", "classify_check_runs", "run_ci_phase"]


class CIPending(Exception):
    """Checks stayed pending past the wait budget (environment hold, FR-014)."""

    def __init__(self, check_names: list[str]) -> None:
        self.check_names = check_names
        super().__init__(f"CI checks pending past the wait budget: {', '.join(check_names) or 'unknown'}")


class CIFailed(Exception):
    """CI repairs exhausted or no progress between polls (FR-013)."""

    def __init__(self, check_names: list[str], reason: str, excerpt: str = "") -> None:
        self.check_names = check_names
        self.reason = reason
        self.excerpt = excerpt
        super().__init__(f"CI failed: {reason}")


_FAIL = {"failure", "cancelled", "timed_out", "action_required", "startup_failure", "stale"}


def classify_check_runs(check_runs: list[dict]) -> tuple[str, list[dict], list[str]]:
    """Pure: (verdict, failed_runs, pending_names) from GitHub check runs.

    ``fail`` when any completed run has a failing conclusion, else ``pending``
    when any run has not completed (or has no conclusion), else ``pass``.
    """
    failed: list[dict] = []
    pending: list[str] = []
    for run in check_runs or []:
        status = str(run.get("status") or "")
        conclusion = run.get("conclusion")
        name = str(run.get("name") or "check")
        if status != "completed" or conclusion is None:
            pending.append(name)
        elif str(conclusion) in _FAIL:
            failed.append(run)
    if failed:
        return "fail", failed, pending
    if pending:
        return "pending", [], pending
    return "pass", [], []


async def _poll_until_settled(ctx: RunContext, head_sha: str, attempts: list[CIAttempt], attempt_number: int) -> tuple[str, list[dict], list[str]]:
    """Poll until the checks conclude or the wait budget is spent."""
    assert ctx.get_check_runs is not None, "ci phase needs get_check_runs"
    deadline = time.monotonic() + ctx.budgets.ci_wait_s
    started = time.monotonic()
    verdict, failed, pending = "pending", [], []
    while True:
        runs = await ctx.get_check_runs(head_sha)
        ctx.github_api_calls += 1
        verdict, failed, pending = classify_check_runs(runs)
        if verdict != "pending":
            break
        if time.monotonic() >= deadline:
            break
        await ctx.sleep(ctx.poll_interval_s)
    attempts.append(
        CIAttempt(
            attempt_number=attempt_number,
            failing_checks=[str(r.get("name") or "check") for r in failed],
            repair_needed=verdict == "fail",
            wall_time_ms=int((time.monotonic() - started) * 1000),
        )
    )
    return verdict, failed, pending


async def _excerpt(ctx: RunContext, failed: list[dict]) -> str:
    parts: list[str] = []
    for run in failed[:3]:
        text = ""
        if ctx.get_check_run_logs is not None:
            try:
                text = await ctx.get_check_run_logs(run)
                ctx.github_api_calls += 1
            except Exception as exc:  # noqa: BLE001 - the summary is the fallback
                log.warning("implementer.ci_log_fetch_failed", error=str(exc))
        if not text:
            output = run.get("output") or {}
            text = f"{output.get('title') or ''}\n{output.get('summary') or ''}\n{output.get('text') or ''}".strip()
        parts.append(f"## {run.get('name') or 'check'}\n{text[-4000:]}")
    return "\n\n".join(parts)[-8000:]


async def run_ci_phase(ctx: RunContext, *, head_sha: str, quality: list[str], rerun_green) -> list[CIAttempt]:
    """Wait for green CI on the PR head, repairing red checks within the caps."""
    attempts: list[CIAttempt] = []
    repairs = 0
    prior_failing: list[str] = []
    sha = head_sha
    while True:
        verdict, failed, pending = await _poll_until_settled(ctx, sha, attempts, attempts.__len__() + 1)
        names = sorted(str(r.get("name") or "check") for r in failed)
        if verdict == "pass":
            log.info("implementer.ci_green", polls=len(attempts), repairs=repairs)
            return attempts
        if verdict == "pending":
            raise CIPending(pending)
        infrastructure = check_infrastructure_reason(failed)
        if infrastructure:
            attempts[-1].repair_needed = False
            raise CIInfrastructureBlocked(infrastructure, sha, names)
        excerpt = await _excerpt(ctx, failed)
        attempts[-1].log_excerpt = excerpt[-2000:]
        if prior_failing and no_progress_check(prior_failing, names):
            raise CIFailed(names, "no progress: the same checks failed on two consecutive polls", excerpt)
        if repairs >= ctx.budgets.ci_repairs:
            raise CIFailed(names, f"{ctx.budgets.ci_repairs} CI repairs exhausted", excerpt)
        repairs += 1
        prior_failing = names
        message = f"fix(#{ctx.issue_number}): {names[0]}" if ctx.issue_number else f"fix: {names[0]}"
        committed = await repair_turn(
            ctx, persona_kind="REPAIR_CI", values={"check_name": ", ".join(names), "log_excerpt": excerpt[-6000:]},
            attempt_number=repairs, rerun_green=rerun_green, commit_message=message,
        )
        if committed and quality:
            quality_failure = await _run_set(ctx, quality, repairs, [])
            if quality_failure is not None:
                log.warning("implementer.ci_repair_quality_failed", command=quality_failure[0])
        await ctx.push()
        from performer.workflows.implementer import commits as git

        sha = git.head_sha(ctx.workspace)
