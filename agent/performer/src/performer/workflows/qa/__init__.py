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

import inspect
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
from performer.workflows.qa.execute import (
    collect_visual_evidence,
    prepare_visual_capture,
    rewrite_command_placeholders,
    run_execute_step,
)
from performer.workflows.qa.boot import AppBoot, plan_needs_server
from performer.workflows.project_shape import ProjectShape, ProjectShapeUnknown, detect_shape, repo_tree
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


def _call_base_factory(factory, worktree, env, shape):
    """Call a base-boot factory with as much as it will accept.

    The factory is injectable, and existing tests supply one-argument and
    two-argument fakes. Passing the shape unconditionally would break them, and
    not passing it at all is the defect this exists to fix.
    """
    try:
        params = inspect.signature(factory).parameters
    except (TypeError, ValueError):
        params = {}
    if len(params) >= 3:
        return factory(worktree, env, shape)
    if _accepts_env(factory):
        return factory(worktree, env)
    return factory(worktree)


def _default_base_boot(worktree: Path, head_env: dict[str, str] | None = None, shape: "ProjectShape | None" = None) -> AppBoot:
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
    # 367: the baseline needs the same reading as the head app. It is the SAME
    # project at an older commit, so how to start it is the same answer -- and
    # without it start_command_for falls through to None and the baseline never
    # boots, which adversarial review found would break every visual or flow
    # run that does not set QA_APP_START_COMMAND.
    return AppBoot(env=env, workspace=worktree, shape=shape)


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
        # The visual evidence tree (a mkdtemp OUTSIDE the scratch — see the
        # execute step). Owned by the report when one is returned: post-
        # processing deletes it after uploading. If the run dies before a
        # report exists, _cleanup removes it instead (411 round-three review).
        self._capture_dir: Path | None = None
        self._capture_dir_reported = False
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
        capture_dir = getattr(self, "_capture_dir", None)
        if capture_dir is not None and not getattr(self, "_capture_dir_reported", False):
            # Aborted before a report: nothing downstream owns the tree.
            shutil.rmtree(capture_dir, ignore_errors=True)

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
            # environmental problem. With no criteria at all the verdict is
            # still defined — a refused run, never a vacuous pass (411 AC7).
            if exc.criteria:
                return self._undemonstrated(exc.criteria, str(exc), metrics)
            return self._no_criteria(metrics)
        finally:
            metrics.step_durations_ms["plan"] = int((time.monotonic() - started) * 1000)
        log.info("qa.plan", checks=len(plan.checks), surfaces=len(plan.surfaces))

        # --- 1b. boot ----------------------------------------------------
        # Before any flow or visual check: the model plans relative targets
        # (`goto /signin`), and Playwright rejects those outright. Resolving
        # them needs a base URL, and a base URL needs a serving app.
        base_url: str | None = None
        if plan_needs_server(plan, planned_base_url):
            # 367: how this project starts, and how long that takes, are the
            # model's to say. This replaced a Rails/Django/Node branch that
            # named its own three frameworks in the failure message, and a flat
            # 60s default chosen by reasoning about Rails migrations.
            #
            # Asked here rather than at intake, for two reasons: a plan that
            # never contacts the app never boots and so never needs it, and an
            # operator who set QA_APP_START_COMMAND has already answered the
            # question. Both skip the call entirely, so nothing pays for a
            # reading it will not use. A command-only plan whose checks curl
            # the app's health endpoint DOES need the boot — that is what
            # plan_needs_server asks (411).
            #
            # A repository the model cannot characterise is an environment
            # error. QA has always stopped rather than guessed, which is the
            # posture #367 asks the documenter to adopt too.
            if not str(boot.env.get("QA_APP_START_COMMAND") or "").strip():
                try:
                    boot.shape = await detect_shape(toolkit, workspace, await repo_tree(toolkit, workspace))
                except ProjectShapeUnknown as exc:
                    log.warning("qa.project_shape_unknown", reason=exc.reason[:200])
                    return self._environment_error(
                        "could not work out how to start this project: " + exc.reason[:300], metrics
                    )
            shape = getattr(boot, "shape", None)
            if shape is not None and not shape.start_command.strip():
                # The reading is an answer: this is a library or CLI, not a
                # bootable application. Running the checks anyway produces
                # either a fake pass or a 'connection refused' defect; the
                # honest verdict names the absence of a server (411 AC4).
                self._step(toolkit, "boot", detail="no server")
                return self._environment_error(
                    "the project shape has no server: not a bootable application "
                    "(library, CLI), so there is no server to exercise the app",
                    metrics,
                )
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
            # The command checks see the booted origin too: plan_needs_server
            # treats $BASE_URL/$PORT as app references, but without this
            # rewrite `curl $BASE_URL/api/status` ran with an empty URL and
            # failed for reasons the boot never caused (411 round-six review).
            rewrite_command_placeholders(plan, base_url)

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
                    # The reading goes with it: same project, older commit,
                    # same answer for how to start it. Passed positionally only
                    # when the factory accepts it, so injected two-argument
                    # fakes in tests keep working.
                    boot_base=lambda wt: _call_base_factory(self._base_boot_factory, wt, boot.env, getattr(boot, "shape", None)),
                )
            except Exception as exc:  # noqa: BLE001
                # A missing baseline makes the delta unknowable. Recorded so the
                # report cannot imply "nothing regressed" from a comparison that
                # never happened.
                baseline_error = f"baseline unavailable: {type(exc).__name__}: {exc}"
                log.warning("qa.baseline.unavailable", error=str(exc))
        metrics.step_durations_ms["baseline"] = int((time.monotonic() - started) * 1000)

        # --- 3. execute --------------------------------------------------
        self._step(toolkit, "execute", detail=f"{len(plan.checks)} checks")
        started = time.monotonic()
        # 411 review: needs_baseline() is true for flow checks too, but only
        # visual checks produce screenshots. Driving the visual-artifact floor
        # off needs_baseline() marked a flow-only plan as visual-validation
        # run and bounced it for evidence it never promised.
        visual_required = any(c.kind == "visual" for c in plan.checks)
        driver_path = str(self._scratch / "qa_flow_driver.py")
        # Visual checks capture their own screenshots: declaring
        # visual_validation_required without capturing anything failed
        # coordinare's evidence floor on every visual run (411 AC5). The
        # evidence dir deliberately sits OUTSIDE the scratch dir: run()'s
        # finally rmtree's the scratch before post-processing ever sees the
        # report, and the paths must still exist when it validates them.
        # It is created only when a visual check exists and the consumer
        # deletes the whole tree after uploading, so a long-lived performer
        # does not accumulate qa-visual-* trees in /tmp (411 review).
        capture_dir = (
            Path(tempfile.mkdtemp(prefix="qa-visual-")) if visual_required else None
        )
        self._capture_dir = capture_dir
        if capture_dir is not None:
            prepare_visual_capture(plan, capture_dir, base_url=base_url)
        executed = await run_execute_step(
            toolkit, plan, cwd=workspace, driver_path=driver_path
        )
        visual_evidence = (
            collect_visual_evidence(plan, capture_dir)
            if capture_dir is not None
            else []
        )
        metrics.step_durations_ms["execute"] = int((time.monotonic() - started) * 1000)

        # --- 4. observe --------------------------------------------------
        self._step(toolkit, "observe")
        started = time.monotonic()
        delta = VisualDelta()
        observe_failed = False
        if before:
            added_all, removed_all = [], []
            unobservable: list[str] = []
            for surface, before_obs in before.items():
                try:
                    after_obs = await self._observe_surface(toolkit, surface)
                except Exception as exc:  # noqa: BLE001
                    # The baseline observe is fail-closed; the post-change
                    # observe must be the same (411 AC6). A surface whose
                    # after-state cannot be read leaves regressions on it
                    # unknowable, so the run is not a pass either way.
                    log.warning(
                        "qa.observe.post_change_failed", surface=surface, error=str(exc)[:200]
                    )
                    unobservable.append(surface)
                    observe_failed = True
                    continue
                if not before_obs and not after_obs:
                    # Nothing observed is not nothing changed. A 404 rendering
                    # an empty shell compared [] to [] and read as "no
                    # regressions" (round-two review). Fail closed exactly
                    # like the exception path: the comparison is unknowable.
                    unobservable.append(surface)
                    observe_failed = True
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
        passed = overall_passed(verdicts, delta, model_verdict)
        if baseline_error and plan.needs_baseline():
            # With no baseline the delta is unknown, so nothing is known about
            # regressions. The criteria may be demonstrated; the RUN is not a
            # pass. Reporting one would let a consumer advance unverified work.
            passed = False
        if observe_failed:
            # Same fail-closed posture, one step later: the after-state of a
            # surface could not be read, so its regressions are unknowable.
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

        # 411 review: (moved to the execute step) only visual checks produce
        # screenshots; a flow-only plan never claims visual evidence.
        payload = report_step.build_report(
            verdicts,
            executed,
            delta,
            findings,
            passed=passed,
            visual_required=visual_required,
            visual_evidence=visual_evidence,
            environment_error=baseline_error,
            boot_check=boot_check,
            app_start_command=getattr(boot.shape, "start_command", None)
            if getattr(boot, "shape", None) is not None
            else None,
        )
        if capture_dir is not None:
            # Managed lifecycle for the evidence dir (411 review): the
            # consumer deletes it once it has uploaded the artifacts, so a
            # long-lived performer does not accumulate qa-visual-* trees in
            # /tmp.
            payload["visual_capture_dir"] = str(capture_dir)
            self._capture_dir_reported = True
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

    def _no_criteria(self, metrics) -> WorkflowResult:
        """Zero acceptance criteria is not a vacuous pass (411 AC7).

        A run with nothing to verify cannot demonstrate anything, and it is
        not an environment problem either — the app may be perfectly healthy.
        The verdict is defined and it is a refusal, naming the absence.
        """
        finding = Finding(
            category="unmet_criterion",
            severity="high",
            criterion="(no acceptance criteria stated)",
            expected="at least one acceptance criterion to verify",
            observed="the card states no acceptance criteria, so nothing can be demonstrated",
        )
        metrics.reached_green = False
        return WorkflowResult(
            report=report_step.build_report(
                [], [], VisualDelta(), [finding], passed=False, visual_required=False,
            ),
            findings=report_step.findings_payload([finding]),
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
