"""The QA role workflow (spec 164).

Six steps, each small enough to finish and verify on its own:

    plan -> baseline -> execute -> observe -> judge -> report

Organising principle: THE MODEL PLANS AND WITNESSES, CODE DECIDES AND VERIFIES.
Anything the DOM, an exit code, or a file on disk can answer deterministically
is taken from there; the model gets only the irreducibly fuzzy parts.

The whole sequence runs inside the performer. Nothing calls coordinare mid-run
(FR-001) — coordinare dispatches a task and receives one response, exactly as it
did before this workflow existed.
"""
from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

import structlog

from performer.models import BackendEvent, BackendEventType
from performer.workflows.base import WorkflowResult
from performer.workflows.qa import report as report_step
from performer.workflows.qa.judge import build_findings, overall_passed, reconcile
from performer.workflows.qa.models import CriterionVerdict, Finding, JudgeOutput, TestPlan, VisualDelta
from performer.workflows.qa.observe import diff_observations
from performer.workflows.qa.baseline import cleanup_worktree, run_baseline_step
from performer.qa_capture import app_base_url
from performer.workflows.qa.boot import AppBoot
from performer.workflows.qa.plan import effective_criteria, EmptyPlan, run_plan_step

if TYPE_CHECKING:
    from performer.models import Score, Stand

log = structlog.get_logger(__name__)

STEPS = ("plan", "baseline", "execute", "observe", "judge", "report")


def _accepts_env(factory) -> bool:
    """True when *factory* takes the head environment as a second argument."""
    import inspect

    try:
        return len(inspect.signature(factory).parameters) >= 2
    except (TypeError, ValueError):  # builtins / C callables
        return False


def _default_base_boot(worktree: Path, head_env: dict[str, str] | None = None) -> AppBoot:
    """Boot the merge-base app on its own free port.

    Inherits the HEAD app's environment rather than the process environment: it
    is the same application, so it needs the same start command, seed command
    and settings -- only the port and the tree differ. Deriving from os.environ
    loses any configuration the caller supplied, and the base app then never
    comes up.
    """
    import socket

    # Bind-then-close-then-spawn is a TOCTOU: another process can take the port
    # in the gap. Accepted rather than engineered around, because the failure is
    # LOUD -- the base app exits with EADDRINUSE, AppBoot._exit_code() catches
    # it, and the run reports environment_error naming the exit code. A wrong
    # verdict is what this design refuses; a rare honest retry is fine.
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    env = dict(head_env if head_env is not None else os.environ)
    env["PORT"] = str(port)
    return AppBoot(env=env, workspace=worktree)


class QAWorkflow:
    """Sequences the six QA steps."""

    name = "qa"
    #: 343: ordered steps, the single source for the wire and the latch.
    steps = STEPS

    def __init__(self, boot_factory=None, base_boot_factory=None) -> None:
        """*boot_factory* builds the AppBoot for a run.

        Injectable so steps stay testable on the host without a serving app --
        the workflow otherwise reads PORT from the process environment, which a
        test cannot vary without mutating global state.
        """
        # The default composes env exactly as the legacy QA capture path does
        # (main.py, _cap_env): {**os.environ, **stand.cache_env}. cache_env is
        # the delta from sourcing the env cache's activate.sh, and it is where
        # PORT, the toolchain PATH and the service vars live. Bare os.environ
        # sees none of it, and every visual plan then dies as "never came up".
        self._boot_factory = boot_factory or (
            lambda workspace, env: AppBoot(env=env, workspace=workspace)
        )
        # The base app runs from the merge-base worktree on its OWN port: it
        # must be a different process from the head app, or before and after are
        # the same page and no regression can ever be detected.
        self._base_boot_factory = base_boot_factory or _default_base_boot

    async def run(self, stand: "Stand", score: "Score", toolkit: Any) -> WorkflowResult:
        """Adversarial review, critical: every launched resource is released.

        Previously a raise anywhere between boot and the report leaked the app
        server (holding PORT across runs) and the baseline worktree (wedging
        later git operations). Cleanup now lives in finally, not on the happy
        path.
        """
        self._boot = None
        self._worktree = None
        self._workspace = Path(getattr(stand, "path", ".") or ".")
        # Run artifacts live OUTSIDE the cloned repository. Round-two review
        # found .qa_flow_driver.py written into the workspace root and never
        # removed, and the .qa_baseline worktree created inside it: both showed
        # as untracked files, and a later `git add .` by the implementer would
        # have committed harness internals into the PR.
        self._scratch = Path(tempfile.mkdtemp(prefix="qa-run-"))
        try:
            return await self._run(stand, score, toolkit)
        finally:
            await self._cleanup(toolkit)

    @staticmethod
    def _step(toolkit, name: str, detail: str = "") -> None:
        """FR-008: visibility without control.

        Round-two review found zero emit() calls anywhere in the workflow -- step
        transitions existed only as ephemeral log lines, so an operator polling
        get_status() saw progress="running" no matter which step was executing,
        and drain_events() returned nothing. These are the durable events the
        monitor loop stores and the dashboard shows.
        """
        emit = getattr(toolkit, "emit", None)
        if callable(emit):
            emit(BackendEvent(type=BackendEventType.progress, text=f"qa.{name}", detail=detail))

    async def _cleanup(self, toolkit) -> None:
        boot = getattr(self, "_boot", None)
        if boot is not None:
            try:
                boot.shutdown()
            except Exception as exc:  # noqa: BLE001
                log.warning("qa.cleanup.boot_shutdown_failed", error=str(exc))
        worktree = getattr(self, "_worktree", None)
        if worktree is not None:
            try:
                await cleanup_worktree(
                    toolkit, workspace=self._workspace, worktree_dir=worktree
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("qa.cleanup.worktree_failed", error=str(exc))
        scratch = getattr(self, "_scratch", None)
        if scratch is not None:
            import shutil

            shutil.rmtree(scratch, ignore_errors=True)

    async def _run(self, stand: "Stand", score: "Score", toolkit: Any) -> WorkflowResult:
        metrics = toolkit.metrics
        workspace = Path(getattr(stand, "path", ".") or ".")

        # --- 1. plan -----------------------------------------------------
        self._step(toolkit, "plan")
        started = time.monotonic()
        # The base URL is knowable from PORT without booting, and the planner
        # needs it: otherwise it invents a conventional port and its own check
        # fails against an app that is running perfectly well.
        # Layering, lowest to highest: process env, the env cache's activation
        # delta (PORT, toolchain PATH, service vars), then the operator's
        # explicit workflow_env from role config. The operator wins: they are
        # stating intent about THIS project's app.
        boot_env = {
            **os.environ,
            **(getattr(stand, "cache_env", None) or {}),
            **(getattr(score, "workflow_env", None) or {}),
        }
        boot = (
            self._boot_factory(workspace, boot_env)
            if _accepts_env(self._boot_factory)
            else self._boot_factory(workspace)
        )
        self._boot = boot
        planned_base_url = app_base_url(boot.env)
        try:
            plan = await run_plan_step(toolkit, score, base_url=planned_base_url)
        except EmptyPlan as exc:
            # Criteria present but nothing derivable to check them is "not
            # demonstrated": a FAIL with one unmet finding per criterion, not an
            # environmental problem. Only with no criteria at all is there
            # genuinely no verdict to give (R7 fail-closed).
            if exc.criteria:
                return self._undemonstrated(exc.criteria, str(exc), metrics)
            return self._environment_error(str(exc), metrics)
        finally:
            metrics.step_durations_ms["plan"] = int((time.monotonic() - started) * 1000)
        log.info("qa.plan", checks=len(plan.checks), surfaces=len(plan.surfaces))

        # --- 1b. boot ----------------------------------------------------
        # Before any flow or visual check: the model plans relative targets
        # (`goto /signin`), and Playwright rejects those outright. Resolving
        # them needs a base URL, and a base URL needs a serving app.
        base_url: str | None = None
        if plan.needs_baseline():
            self._step(toolkit, "boot", detail=planned_base_url or "")
            base_url = await boot.ensure_serving(toolkit)
            if base_url is None:
                reason = getattr(boot, "failure_reason", None) or (
                    "the application under test never came up"
                )
                return self._environment_error(
                    f"{reason} No flow or visual criterion could be exercised.",
                    metrics,
                )
            AppBoot.rewrite_targets(plan, base_url)

        # --- 2. baseline -------------------------------------------------
        metrics.baseline_skipped = not plan.needs_baseline()
        started = time.monotonic()
        before: dict[str, list] = {}
        baseline_error: str | None = None
        worktree_dir = self._scratch / "baseline"
        self._worktree = worktree_dir
        if plan.needs_baseline():
            self._step(toolkit, "baseline")
            try:
                merge_base = await self._merge_base(toolkit, workspace, score)
                before = await run_baseline_step(
                    toolkit, plan,
                    workspace=workspace,
                    merge_base=merge_base,
                    worktree_dir=worktree_dir,
                    base_url=base_url,
                    boot_base=lambda wt: self._base_boot_factory(wt, boot.env)
                    if _accepts_env(self._base_boot_factory)
                    else self._base_boot_factory(wt),
                )
            except Exception as exc:  # noqa: BLE001
                # A missing baseline makes the delta unknowable. Recorded so the
                # report cannot imply "nothing regressed" from a comparison that
                # never happened.
                baseline_error = f"baseline unavailable: {type(exc).__name__}: {exc}"
                log.warning("qa.baseline.unavailable", error=str(exc))
        metrics.step_durations_ms["baseline"] = int((time.monotonic() - started) * 1000)

        # --- 3. execute --------------------------------------------------
        from performer.workflows.qa.execute import run_execute_step

        self._step(toolkit, "execute", detail=f"{len(plan.checks)} checks")
        started = time.monotonic()
        driver_path = str(self._scratch / "qa_flow_driver.py")
        executed = await run_execute_step(
            toolkit, plan, cwd=workspace, driver_path=driver_path
        )
        metrics.step_durations_ms["execute"] = int((time.monotonic() - started) * 1000)

        # --- 4. observe --------------------------------------------------
        self._step(toolkit, "observe")
        started = time.monotonic()
        delta = VisualDelta()
        if before:
            added_all, removed_all = [], []
            unobservable: list[str] = []
            for surface, before_obs in before.items():
                after_obs = await self._observe_surface(toolkit, surface)
                if not before_obs and not after_obs:
                    # Nothing observed is not nothing changed. A 404 rendering
                    # an empty shell compared [] to [] and read as "no
                    # regressions" (round-two review).
                    unobservable.append(surface)
                    continue
                added, removed = diff_observations(before_obs, after_obs)
                added_all.extend(added)
                removed_all.extend(removed)
            delta = VisualDelta(added=added_all, removed=removed_all)
        metrics.step_durations_ms["observe"] = int((time.monotonic() - started) * 1000)

        # --- 5. judge ----------------------------------------------------
        criteria, _criteria_source = effective_criteria(score)  # 165: the same list the plan used
        self._step(toolkit, "judge")
        started = time.monotonic()
        model_verdict = await self._judge(toolkit, plan, executed, delta, criteria)
        metrics.step_durations_ms["judge"] = int((time.monotonic() - started) * 1000)

        verdicts = reconcile(model_verdict, plan, executed, criteria)
        findings = build_findings(verdicts, delta, executed, model_verdict)
        for surface in locals().get("unobservable", []) or []:
            findings.append(Finding(
                category="step_unavailable",
                severity="medium",
                expected=f"observable elements at {surface}",
                observed=f"{surface} rendered no observable elements before or after; "
                         "the surface could not be compared",
            ))
        if getattr(boot, "adopted_existing_server", False):
            metrics.adopted_existing_server = True
        passed = overall_passed(verdicts, delta)
        if baseline_error and plan.needs_baseline():
            # With no baseline the delta is unknown, so nothing is known about
            # regressions. The criteria may be demonstrated; the RUN is not a
            # pass. Reporting one would let a consumer advance unverified work.
            passed = False

        # --- 6. report ---------------------------------------------------
        self._step(toolkit, "report", detail="passed" if passed else "failed")
        boot.shutdown()

        # The boot health check is real evidence and must appear in
        # executed_checks: the 088 floor rejects an app_boot_check whose command
        # did not actually run.
        boot_check = getattr(boot, "boot_check", None)
        if boot_check is not None:
            executed = [boot_check, *executed]

        # The spec's success metric is rounds-to-green. A run can only report
        # its own ordinal and whether it went green; coordinare aggregates.
        metrics.reached_green = passed
        metrics.round_number = max(1, int(getattr(score, "attempt", 1) or 1))

        payload = report_step.build_report(
            verdicts,
            executed,
            delta,
            findings,
            passed=passed,
            visual_required=plan.needs_baseline(),
            environment_error=baseline_error,
            boot_check=boot_check,
        )
        return WorkflowResult(
            report=payload,
            findings=report_step.findings_payload(findings),
            metrics=metrics,
        )

    async def _merge_base(self, toolkit, workspace: Path, score) -> str:
        """Resolve the commit the change branched from.

        Tries the remote-tracking ref first: in a real clone ``origin/<base>``
        is authoritative, and a stale local branch would silently compare
        against the wrong commit. Falls back to a local ``<base>`` for a
        repository with no origin (a generated fixture, a local-only clone).

        Raises when neither resolves. Guessing a base would produce a confident,
        wrong delta, which is worse than reporting the baseline unavailable.
        """
        base = (getattr(score, "base_branch", "") or "main").strip()
        attempts: list[str] = []
        for ref in (f"origin/{base}", base):
            result = await toolkit.run_command(
                f"git merge-base HEAD {ref}", cwd=workspace
            )
            attempts.append(f"{ref} (exit {result.exit_code})")
            if result.passed and (result.output_excerpt or "").strip():
                return result.output_excerpt.strip().splitlines()[0]
        raise RuntimeError(
            "could not resolve a base ref to compare against; tried "
            + ", ".join(attempts)
        )

    async def _observe_surface(self, toolkit, surface: str) -> list:
        """Read the post-change state of one surface from the DOM."""
        from performer.workflows.models import Observation

        elements = await toolkit.dom_snapshot(surface)
        return [
            Observation(kind=e.get("kind", "other"), position=i, label=e.get("label"))
            for i, e in enumerate(elements)
        ]

    async def _judge(self, toolkit, plan: TestPlan, executed, delta, criteria) -> JudgeOutput:
        from performer.workflows.budget import Budget
        from performer.workflows.qa.personas import JUDGE

        content = [{
            "type": "text",
            "text": (
                "Acceptance criteria:\n"
                + "\n".join(f"- {c}" for c in criteria)
                + "\n\nExecuted checks:\n"
                + "\n".join(
                    f"- [{c.plan_check_id}] {c.command} -> exit {c.exit_code}"
                    for c in executed
                )
                + "\n\nRemoved elements (present before, absent after):\n"
                + ("\n".join(f"- {o.kind} {o.label!r}" for o in delta.removed) or "- none")
            ),
        }]
        return await toolkit.call_model(
            persona=JUDGE, schema=JudgeOutput, content=content, budget=Budget.for_step("judge")
        )

    def _undemonstrated(self, criteria: list[str], reason: str, metrics) -> WorkflowResult:
        """Criteria exist; nothing could be derived to check them. A fail."""
        findings = [
            Finding(
                category="unmet_criterion",
                severity="high",
                criterion=c,
                expected=c,
                observed=f"no check could be derived to demonstrate this criterion ({reason})",
            )
            for c in criteria
        ]
        verdicts = [CriterionVerdict(criterion=c, passed=False, note="no derivable check") for c in criteria]
        metrics.reached_green = False
        return WorkflowResult(
            report=report_step.build_report(
                verdicts, [], VisualDelta(), findings, passed=False, visual_required=False,
            ),
            findings=report_step.findings_payload(findings),
            metrics=metrics,
        )

    def _environment_error(self, reason: str, metrics) -> WorkflowResult:
        return WorkflowResult(
            report=report_step.build_report(
                [], [], VisualDelta(), [],
                passed=False, visual_required=False, environment_error=reason,
            ),
            findings=[],
            metrics=metrics,
        )
