"""Quality pass of the implementer workflow (spec 167 FR-010, FR-015).

The detected lint command first, then every command the symphony declares on
the role's ``workflow_env`` (``QUALITY_COMMANDS``), in order, stopping at the
first non-zero exit. A failure is a repair turn carrying that tool's output,
then the milestone tests (a regression routes back to a green check), then
the whole set again from the top. At most ``budgets.quality_repairs``
repairs; a clean set with changes is committed as ``style(#n): satisfy <tool>``.
"""
from __future__ import annotations

import time

import structlog

from performer.workflows.implementer import commits as git
from performer.workflows.implementer.baseline import regressions, run_tests
from performer.workflows.implementer.driver import RunContext, _build_brief, run_turn
from performer.workflows.implementer.models import MilestonePlan, QualityAttempt

log = structlog.get_logger(__name__)

__all__ = ["QualityFailed", "quality_commands", "run_quality_phase"]


class QualityFailed(Exception):
    """A quality command stayed red after the repair budget (FR-010)."""

    def __init__(self, command: str, output: str) -> None:
        super().__init__(f"quality command failed after repairs: {command}")
        self.command = command
        self.output = output


def quality_commands(lint_command: str | None, declared: tuple[str, ...] | list[str]) -> list[str]:
    """The ordered set: detected lint first, then the declared commands, no duplicates."""
    out: list[str] = []
    for cmd in ([lint_command] if lint_command else []) + list(declared):
        cmd = (cmd or "").strip()
        if cmd and cmd not in out:
            out.append(cmd)
    return out


async def _run_set(ctx: RunContext, commands: list[str], attempt_number: int, attempts: list[QualityAttempt]) -> tuple[str, str] | None:
    """Run the set in order; return (command, output) of the first failure or None."""
    for cmd in commands:
        started = time.monotonic()
        check = await ctx.toolkit.run_command(cmd, cwd=ctx.workspace, timeout_s=ctx.test_timeout_s)
        attempts.append(
            QualityAttempt(
                command=cmd,
                attempt_number=attempt_number,
                exit_code=check.exit_code,
                output_tail=(check.output_excerpt or "")[-2000:],
                wall_time_ms=int((time.monotonic() - started) * 1000),
                passed=check.exit_code == 0,
            )
        )
        if check.exit_code != 0:
            from performer.infrastructure import InfrastructureBlocked
            from performer.test_results import _match_env_signature

            if cause := _match_env_signature(check.output_excerpt or ""):
                raise InfrastructureBlocked(cause)
            log.info("implementer.quality_failed", command=cmd, attempt=attempt_number, exit_code=check.exit_code)
            return cmd, check.output_excerpt or ""
    return None


def repair_milestone(ctx: RunContext) -> MilestonePlan:
    """The milestone a repair turn is framed against: the last one, or the card."""
    last = ctx.milestone_records[-1] if ctx.milestone_records else None
    goal = (last.goal if last else str(getattr(ctx.score, "title", "") or "the card"))[:256] or "the card"
    done_when = (last.done_when if last else "the checks exit zero")[:256] or "the checks exit zero"
    return MilestonePlan(index=last.index if last else 0, goal=goal, scope=".", done_when=done_when,
                         lane=ctx.lane, lane_source=ctx.lane_source)  # type: ignore[arg-type]


async def repair_turn(ctx: RunContext, *, persona_kind: str, values: dict[str, str], attempt_number: int, rerun_green, commit_message: str) -> bool:
    """One repair turn (quality or CI): turn, milestone tests, route back on a
    regression, else commit the repair. Returns True when a commit was made."""
    milestone = repair_milestone(ctx)
    brief = _build_brief(ctx, milestone, kind="repair", persona_kind=persona_kind, failure_excerpt=values.get("tool_output") or values.get("log_excerpt"), extra_values=values)
    start_sha = git.head_sha(ctx.workspace)
    result, _attempt, changed = await run_turn(ctx, brief, attempt_number=attempt_number)
    if result.exit_state != "done":
        await git._run_git(["git", "reset", "--hard", start_sha], ctx.workspace)
        return False
    if not changed:
        return False
    summary = await run_tests(ctx.toolkit, ctx.test_command, ctx.runner_kind, ctx.workspace, ctx.test_timeout_s)
    regs = regressions(ctx.baseline, summary)
    if regs or not summary.passed:
        log.info("implementer.repair_regressed", persona=persona_kind, regressions=regs[:5])
        await rerun_green(milestone.index, regs, summary)
        return True
    await git.commit_paths(ctx.workspace, sorted(changed), commit_message)
    return True


async def run_quality_phase(ctx: RunContext, commands: list[str], *, rerun_green) -> list[QualityAttempt]:
    """Run the quality set with the bounded repair loop (FR-010).

    ``rerun_green(index, regressions, summary)`` is the workflow's way back
    into a green check when a repair turn regresses a milestone (FR-015).
    """
    attempts: list[QualityAttempt] = []
    if not commands:
        log.info("implementer.quality_skipped", reason="no commands")
        return attempts
    failure = await _run_set(ctx, commands, 1, attempts)
    repairs = 0
    while failure is not None:
        cmd, output = failure
        if repairs >= ctx.budgets.quality_repairs:
            raise QualityFailed(cmd, output)
        repairs += 1
        tool = cmd.split()[0] if cmd.split() else cmd
        message = f"style(#{ctx.issue_number}): satisfy {tool}" if ctx.issue_number else f"style: satisfy {tool}"
        await repair_turn(
            ctx, persona_kind="REPAIR_QUALITY", values={"command": cmd, "tool_output": output[-6000:]},
            attempt_number=repairs, rerun_green=rerun_green, commit_message=message,
        )
        failure = await _run_set(ctx, commands, repairs + 1, attempts)
    return attempts
