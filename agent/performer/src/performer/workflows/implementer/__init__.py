"""Implementer test-first workflow (spec 167).

The implementer runs a bounded cycle per blueprint milestone in the lane the
work kind selects (feature, bug, chore, refactor, tests), then the quality
pass, the spec-089 local gate, push, PR, and a wait for green CI with bounded
repair turns. Every check that fails after code is written sends a repair
turn with the exact output before the workflow continues. Nothing red is
ever pushed, and ``pr_opened`` is reported only on green CI.

Selected by ``workflow: implementer`` on the role and run behind the
WorkflowAdapter seam (spec 164). The report is
``{"implementer_run": RunRecord, "workflow_metrics": {...}}``; ``main.py``
maps its status onto the PerformerResponse the prose path returns.
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from performer.models import BackendEvent, BackendEventType
from performer.test_results import _env_signature_reason, _match_env_signature
from performer.workflows.base import WorkflowResult
from performer.workflows.implementer.baseline import (  # noqa: I001
    name_based_comparison,
    new_failures,
    NoTestRunner,
    capture_baseline,
    detect_lint_command,
    detect_test_command,
    run_tests,
)
from performer.workflows.implementer import commits as git
from performer.workflows.implementer.budgets import ImplementerBudgets
from performer.infrastructure import CIInfrastructureBlocked, InfrastructureBlocked
from performer.workflows.implementer.ci import CIFailed, CIPending, run_ci_phase
from performer.workflows.implementer.driver import MilestoneFailed, RunContext, _green_phase, run_milestone
from performer.workflows.implementer.models import MilestonePlan, PerMilestoneRecord, RunRecord
from performer.workflows.implementer.plan import LaneNotForImplementer, build_plan
from performer.workflows.implementer.quality import QualityFailed, quality_commands, run_quality_phase
from performer.workflows.implementer.resume import (
    apply_resume,
    present_paths,
    prior_run_paths,
    resume_state,
    scope_segments,
)
from performer.workflows.implementer.report import assemble_run_record

if TYPE_CHECKING:
    from performer.models import Score, Stand

log = structlog.get_logger(__name__)

__all__ = ["ImplementerWorkflow", "STATES"]

STATES: tuple[str, ...] = (
    "intake", "plan", "baseline", "resume", "milestone", "milestones", "quality", "local_gate", "push_pr",
    "ci_wait", "handoff",
)


class _EnvHold(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _PushFailed(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class _GateRejected(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class ImplementerWorkflow:
    """Orchestrates the test-first implementer workflow per spec 167."""

    name = "implementer"
    #: 343: ordered steps, the single source for the wire and the latch.
    steps = STATES
    STATES = STATES

    @staticmethod
    def _step(toolkit: Any, name: str, detail: str = "") -> None:
        emit = getattr(toolkit, "emit", None)
        if callable(emit):
            emit(BackendEvent(type=BackendEventType.progress, text=f"implementer.{name}", detail=detail))
        log.info(f"implementer.{name}", detail=detail)

    # -- default edges to the outside world (tests inject fakes) -----------

    @staticmethod
    def _default_edges(ctx: RunContext) -> None:
        from performer import github, workspace as ws

        score, stand = ctx.score, ctx.stand
        owner, repo = score.owner_repo
        token = score.effective_github_token

        async def push() -> None:
            remote_url = str(score.repo_url).rstrip("/")
            if not remote_url.endswith(".git"):
                remote_url += ".git"
            env = ws._git_credential_env(token)
            git_run, git_out = ws._stand_git_runners(stand, env)
            await ws._push_head_without_clobbering(git_run, git_out, remote=remote_url, branch=stand.branch, score=score)

        async def open_or_update_pr() -> tuple[str, str]:
            ctx.github_api_calls += 1
            return await github.create_pull_request(owner, repo, score, stand.branch, token)

        async def get_check_runs(sha: str) -> list[dict]:
            return await github.get_check_runs(owner, repo, sha, token)

        async def get_check_run_logs(run: dict) -> str:
            job_id = int(run.get("id") or 0)
            return await github.get_check_run_logs(owner, repo, job_id, token) if job_id else ""

        async def local_gate() -> tuple[str, str]:
            summary = await run_tests(ctx.toolkit, ctx.test_command, ctx.runner_kind, ctx.workspace, ctx.test_timeout_s)
            if summary.passed:
                return "pass", ""
            signature = _match_env_signature(summary.raw_tail)
            if signature:
                return "env", _env_signature_reason(signature, summary.raw_tail)
            # 387: the gate asks whether THIS CARD broke anything, not whether
            # the repository is perfect. Asserting absolute green meant a repo
            # with one pre-existing failing test could never ship a card, on a
            # condition no card caused and none can fix within its scope. A
            # failure already present at baseline is the repository's, and is
            # reported rather than charged to the card.
            introduced = new_failures(ctx.baseline, summary)
            if not introduced:
                already = len(ctx.baseline.test_names_failed or []) or ctx.baseline.fail_count or 0
                log.warning(
                    "implementer.local_gate_red_baseline",
                    already_failing=already,
                    detail="shipping onto a suite that was already red",
                )
                verified = name_based_comparison(ctx.baseline, summary)
                return "pass", (
                    f"{already} test(s) were already failing before this card; "
                    + ("none of the failures are new"
                       if verified else
                       "the runner printed no test names, so this was compared by "
                       "COUNT only and an equal count cannot prove the same tests failed")
                )
            return "fail", "new failures: " + ", ".join(introduced[:20]) + "\n" + summary.raw_tail[-1200:]

        async def sleep(seconds: float) -> None:
            import asyncio

            await asyncio.sleep(seconds)

        ctx.push = ctx.push if ctx.push is not RunContext.push else push
        if ctx.open_or_update_pr is None:
            ctx.open_or_update_pr = open_or_update_pr
        if ctx.get_check_runs is None:
            ctx.get_check_runs = get_check_runs
        if ctx.get_check_run_logs is None:
            ctx.get_check_run_logs = get_check_run_logs
        if ctx.local_gate is None:
            ctx.local_gate = local_gate
        if ctx.sleep is RunContext.sleep:
            ctx.sleep = sleep

    # -- resume (spec 171) -------------------------------------------------

    @staticmethod
    async def _resume(
        ctx: RunContext, plans: list[MilestonePlan], workspace: Path
    ) -> tuple[list[MilestonePlan], int | None]:
        """Drop the milestones a previous run of this card already did (171 FR-007).

        Reads the branch's own history once, keeps the paths this card's earlier
        commits touched on ``ctx.prior_paths`` (the red step reads it too), and
        returns the milestones still to run plus the index resumed from. A fresh
        branch has no such commits and returns the plan untouched, which is the
        pre-171 behaviour.
        """
        base = str(getattr(ctx.score, "base_branch", "") or "").strip() or "main"
        try:
            entries = await git.branch_commit_entries(workspace, [f"origin/{base}", base, "origin/main", "main"])
        except Exception as exc:  # noqa: BLE001 - run() has no generic handler, and resume is
            # only an optimisation: a history we cannot read must cost us the skip, never the run.
            log.warning("implementer.resume_history_unreadable", error=str(exc))
            entries = []
        ctx.prior_paths = prior_run_paths(entries, ctx.issue_number)
        if not ctx.prior_paths:
            log.info("implementer.resume", commits=len(entries), prior_paths=0, skipped=[], resumed_from=None)
            return list(plans), None

        states = [
            resume_state(
                plan,
                ctx.prior_paths,
                present_paths(workspace, scope_segments(plan.scope)),
                ctx.baseline.test_names,
                ctx.baseline.test_names_failed,
            )
            for plan in plans
        ]
        skipped, remaining = apply_resume(plans, states)
        for plan in skipped:
            ctx.milestone_records.append(
                PerMilestoneRecord(
                    index=plan.index,
                    goal=plan.goal,
                    done_when=plan.done_when,
                    implementation_successful=True,
                    satisfied_by="prior_run",
                )
            )
        resumed_from = remaining[0].index if (skipped and remaining) else None
        log.info(
            "implementer.resume",
            commits=len(entries),
            prior_paths=len(ctx.prior_paths),
            states=[f"{plan.index}:{state}" for plan, state in zip(plans, states, strict=True)],
            skipped=[plan.index for plan in skipped],
            relaned=[plan.index for plan in remaining if plan.lane_source == "resume"],
            resumed_from=resumed_from,
        )
        return remaining, resumed_from

    # -- the run -----------------------------------------------------------

    async def run(self, stand: "Stand", score: "Score", toolkit: Any, *, ctx_overrides: dict[str, Any] | None = None) -> WorkflowResult:
        started = time.monotonic()
        durations: dict[str, int] = {}
        metrics = getattr(toolkit, "metrics", None)
        plans: list[MilestonePlan] = []
        ctx: RunContext | None = None
        status, reason, next_focus = "env_blocked", "", None
        quality_attempts: list = []
        ci_attempts: list = []
        ci_infrastructure: dict | None = None
        resumed_from: int | None = None
        remaining: list[MilestonePlan] = []

        def timed(name: str, t0: float) -> None:
            ms = int((time.monotonic() - t0) * 1000)
            durations[name] = ms
            if metrics is not None:
                metrics.step_durations_ms[name] = ms

        try:
            # intake and plan
            self._step(toolkit, "intake")
            t = time.monotonic()
            budgets = ImplementerBudgets.from_env(getattr(score, "workflow_env", None) or {})
            brief = getattr(score, "implementation_brief", None) or None
            timed("intake", t)
            self._step(toolkit, "plan")
            t = time.monotonic()
            try:
                plans = build_plan(score, brief)
            except LaneNotForImplementer as exc:
                raise _EnvHold(f"work kind is not for the implementer: {exc}") from exc
            timed("plan", t)
            lane = plans[0].lane if plans else "feature"
            lane_source = plans[0].lane_source if plans else "unknown"
            log.info("implementer.lane", lane=lane, source=lane_source, milestones=len(plans))

            # baseline
            self._step(toolkit, "baseline")
            t = time.monotonic()
            workspace = Path(stand.path)
            try:
                test_command, runner_kind, _detected_from = detect_test_command(score, workspace)
                baseline = await capture_baseline(toolkit, score, workspace, timeout_s=budgets.test_timeout_s)
            except NoTestRunner as exc:
                raise _EnvHold(f"no test runner detected: {exc}") from exc
            timed("baseline", t)
            ctx = RunContext(
                toolkit=toolkit, stand=stand, score=score, budgets=budgets, runner_kind=runner_kind,
                test_command=test_command, baseline=baseline, plans=plans, lane=lane, lane_source=lane_source,
                issue_number=int(getattr(score, "issue_number", 0) or 0),
                test_timeout_s=budgets.test_timeout_s,
            )
            for key, value in (ctx_overrides or {}).items():
                setattr(ctx, key, value)
            self._default_edges(ctx)

            async def rerun_green(index: int, regs: list[str], summary) -> None:
                """FR-015: a repair regressed a milestone; back into its green check."""
                plan = next((p for p in plans if p.index == index), plans[-1] if plans else None)
                if plan is None:
                    return
                record = next((r for r in ctx.milestone_records if r.index == index), None)
                if record is None:
                    return
                await _green_phase(ctx, plan, record, [], summary)

            # resume (171): skip what a previous run of this card already did
            self._step(toolkit, "resume")
            t = time.monotonic()
            remaining, resumed_from = await self._resume(ctx, plans, workspace)
            timed("resume", t)

            # milestones
            self._step(toolkit, "milestones", f"{len(remaining)} of {len(plans)} in lane {lane}")
            t = time.monotonic()
            lint_command = detect_lint_command(score, workspace)
            commands = quality_commands(lint_command, budgets.quality_commands)
            quality_attempts = []
            for plan in remaining:
                self._step(toolkit, "milestone", f"{plan.index}: {plan.goal}")
                await run_milestone(ctx, plan)
                if lane == "repair" and commands:
                    # 169 FR-013: the repair lane runs the quality set after every turn,
                    # so a group that breaks lint is repaired before the next group.
                    quality_attempts.extend(await run_quality_phase(ctx, commands, rerun_green=rerun_green))
            timed("milestones", t)

            # quality
            self._step(toolkit, "quality")
            t = time.monotonic()
            if not (lane == "repair" and commands and remaining):
                quality_attempts.extend(await run_quality_phase(ctx, commands, rerun_green=rerun_green))
            timed("quality", t)

            # local gate (089, unchanged in meaning)
            self._step(toolkit, "local_gate")
            t = time.monotonic()
            assert ctx.local_gate is not None
            verdict, detail = await ctx.local_gate()
            timed("local_gate", t)
            if verdict == "env":
                raise _EnvHold(detail)
            if verdict != "pass":
                raise _GateRejected(f"local test gate failed after green milestones: {detail}")

            # push and PR
            self._step(toolkit, "push_pr")
            t = time.monotonic()
            try:
                await ctx.push()
            except Exception as exc:  # noqa: BLE001 - a rejected push is a bounded failure, not a crash
                raise _PushFailed(f"push failed: {exc}") from exc
            assert ctx.open_or_update_pr is not None
            try:
                ctx.pr_url, ctx.pr_node_id = await ctx.open_or_update_pr()
            except Exception as exc:  # noqa: BLE001 - the PR API is infrastructure
                raise _EnvHold(f"could not open or update the pull request: {exc}") from exc
            timed("push_pr", t)

            # CI wait
            self._step(toolkit, "ci_wait")
            t = time.monotonic()
            ci_attempts = await run_ci_phase(ctx, head_sha=git.head_sha(ctx.workspace), quality=commands, rerun_green=rerun_green)
            timed("ci_wait", t)

            self._step(toolkit, "handoff", ctx.pr_url or "")
            status, reason = "pr_opened", "all checks green"
        except MilestoneFailed as exc:
            status, reason, next_focus = "partial_progress", exc.reason, exc.goal
        except QualityFailed as exc:
            status, reason = "partial_progress", f"quality command {exc.command} failed after repairs: {exc.output[-1500:]}"
            next_focus = remaining[-1].goal if remaining else None
        except CIFailed as exc:
            status, reason = "partial_progress", f"CI checks {', '.join(exc.check_names)}: {exc.reason}\n{exc.excerpt[-1500:]}"
            next_focus = remaining[-1].goal if remaining else None
        except CIInfrastructureBlocked as exc:
            status, reason = "env_blocked", str(exc)
            ci_infrastructure = {"head_sha": exc.head_sha, "check_names": exc.check_names, "cause": str(exc)}
        except (CIPending, InfrastructureBlocked) as exc:
            status, reason = "env_blocked", str(exc)
        except _PushFailed as exc:
            status, reason = "partial_progress", exc.reason
            next_focus = remaining[-1].goal if remaining else None
        except _EnvHold as exc:
            status, reason = "env_blocked", exc.reason
        except _GateRejected as exc:
            status, reason = "changes_requested", exc.reason

        total_ms = int((time.monotonic() - started) * 1000)
        completed = sum(1 for r in (ctx.milestone_records if ctx else []) if r.implementation_successful)
        record: RunRecord = assemble_run_record(
            status=status,
            reason=reason,
            milestones_planned=len(plans),
            milestones_completed=completed,
            resumed_from_milestone=resumed_from,
            next_focus_milestone=next_focus,
            per_milestone=list(ctx.milestone_records) if ctx else [],
            quality_attempts=quality_attempts,
            ci_attempts=ci_attempts,
            scope_reverts=list(ctx.scope_reverts) if ctx else [],
            phase_durations_ms=durations,
            total_duration_ms=total_ms,
            # ctx is None when the run fails before a context exists (no test
            # runner, a role that never reaches the implementer), and those
            # paths salvage nothing by definition.
            work_salvaged=bool(getattr(ctx, "work_salvaged", False)),
        )
        record = record.model_copy(update={
            "turn_count": len(ctx.turn_attempts) if ctx else 0,
            "model_calls": int(getattr(metrics, "model_calls", 0) or 0),
            "github_api_calls": ctx.github_api_calls if ctx else 0,
        })
        log.info(
            "implementer.run_completed",
            status=status,
            lane=ctx.lane if ctx else None,
            milestones_planned=len(plans),
            milestones_completed=completed,
            resumed_from=resumed_from,
            turns=record.turn_count,
            quality_attempts=len(quality_attempts),
            ci_attempts=len(ci_attempts),
            total_ms=total_ms,
            pr_url=ctx.pr_url if ctx else None,
        )
        report = {
            "implementer_run": record.model_dump(),
            "ci_infrastructure": ci_infrastructure,
            "pr_url": ctx.pr_url if ctx else None,
            "pr_node_id": ctx.pr_node_id if ctx else None,
            "workflow_metrics": {
                "step_durations_ms": dict(durations),
                "agent_turns": int(getattr(metrics, "agent_turns", 0) or 0),
                "turn_durations_ms": list(getattr(metrics, "turn_durations_ms", []) or []),
                "commands_run": int(getattr(metrics, "commands_run", 0) or 0),
            },
        }
        return WorkflowResult(report=report)
