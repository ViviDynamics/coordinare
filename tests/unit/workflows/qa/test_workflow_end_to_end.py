"""T035/T036 — the six steps in sequence, and the evidence floor over the top."""
from __future__ import annotations

import contextlib
import json
from pathlib import Path
from typing import ClassVar

import pytest
from performer.workflows.base import WorkflowMetrics
from performer.workflows.models import ExecutedCheck
from performer.workflows.project_shape import ProjectShape
from performer.workflows.qa import QAWorkflow
from performer.workflows.qa.models import JudgeOutput, PlanCheck, TestPlan

CRIT = "Users can sign in with a workspace selected"


class _Stand:
    path = "/workspace"


class _Score:
    acceptance_criteria: ClassVar[list[str]] = [CRIT]
    pr_diff = "diff"
    description = "add workspace selector"
    base_branch = "main"
    role = "qa"


class _Toolkit:
    def __init__(self, *, plan, judge, exit_codes=None, dom_before=None, dom_after=None, shape=None):
        self.metrics = WorkflowMetrics()
        self.events = []
        self._plan, self._judge = plan, judge
        self._shape = shape or ProjectShape(
            project_name="app", summary="the app under test",
            start_command="bin/serve", boot_seconds=30, source_dirs=["src"],
        )
        self._exit_codes = exit_codes or {}
        self._dom_before, self._dom_after = dom_before or [], dom_after or []
        self._snapshots = 0
        self.ran: list[str] = []

    async def call_model(self, *, persona, schema, content, budget):
        # 367: the boot step asks what this project is and how it starts. This
        # fake dispatches on schema, so the new call needs a branch rather than
        # consuming the judge's reply -- which is what it did first, and the
        # symptom was JudgeOutput having no attribute 'cannot_determine'.
        #
        # The contract itself is pinned against the REAL Toolkit in
        # tests/unit/workflows/test_367_project_shape.py; this only keeps the
        # existing flows running.
        if schema is ProjectShape:
            return self._shape
        return self._plan if schema is TestPlan else self._judge

    async def run_command(self, cmd, *, cwd=None, timeout_s=300, plan_check_id=""):
        self.ran.append(cmd)
        code = 0
        if cmd.startswith("git merge-base"):
            return ExecutedCheck(plan_check_id="", command=cmd, exit_code=0,
                                 output_excerpt="abc123", passed=True)
        for prefix, c in self._exit_codes.items():
            if cmd.startswith(prefix):
                code = c
        return ExecutedCheck.from_result(cmd, code, "", plan_check_id=plan_check_id)

    def emit(self, event):
        self.events.append(event)

    async def dom_snapshot(self, url):
        # Base and head are two separate processes on two ports; which page you
        # get depends on which origin you ask.
        self._snapshots += 1
        return self._dom_before if ":9999" in url else self._dom_after




def _base_boot(worktree):
    """The merge-base app, on its OWN port.

    It must be a different process from the head app: if both snapshots come
    from the same server, before and after are the same page and no regression
    can ever be detected. That was defect 9.
    """
    from performer.workflows.qa.boot import AppBoot

    return AppBoot(
        env={"PORT": "9999"}, workspace=worktree, port_check=lambda _h, _p: True,
    )


def _serving_boot(workspace):
    """An AppBoot for an app that is already up, so flow/visual plans can run."""
    from performer.workflows.qa.boot import AppBoot

    return AppBoot(
        env={"PORT": "8000"}, workspace=workspace, port_check=lambda _h, _p: True,
    )


@pytest.mark.asyncio
async def test_a_clean_run_passes_with_evidence():
    tk = _Toolkit(
        plan=TestPlan(checks=[PlanCheck(id="c1", criterion=CRIT, kind="command", command="pytest -q")]),
        judge=JudgeOutput(criteria=[{"criterion": CRIT, "passed": True}], delta_matches_expected=True),
    )
    result = await QAWorkflow().run(_Stand(), _Score(), tk)

    assert result.report["passed"] is True
    assert result.report["criteria_passed"] == 1
    assert result.report["executed_checks"][0]["exit_code"] == 0
    assert result.findings == []
    assert tk.metrics.baseline_skipped is True, "command-only plan skips the second boot"


@pytest.mark.asyncio
async def test_a_failing_check_fails_the_run_and_produces_a_brief():
    tk = _Toolkit(
        plan=TestPlan(checks=[PlanCheck(id="c1", criterion=CRIT, kind="command", command="pytest -q")]),
        judge=JudgeOutput(criteria=[{"criterion": CRIT, "passed": True}]),
        exit_codes={"pytest": 1},
    )
    result = await QAWorkflow().run(_Stand(), _Score(), tk)

    assert result.report["passed"] is False
    assert result.findings, "a failed run must hand back a repair brief"
    assert result.findings[0]["category"] == "unmet_criterion"
    assert result.findings[0]["evidence"]["exit_code"] == 1
    assert result.findings[0]["repro_command"] == "pytest -q"


@pytest.mark.asyncio
async def test_a_silent_regression_is_caught_even_when_the_criterion_passed():
    """The whole point. A visual plan, a criterion that passes, and an element
    that quietly disappeared between base and head."""
    tk = _Toolkit(
        plan=TestPlan(
            checks=[PlanCheck(id="c1", criterion=CRIT, kind="visual")],
            surfaces=["http://localhost:3000/"],
        ),
        judge=JudgeOutput(criteria=[{"criterion": CRIT, "passed": True}]),
        dom_before=[{"kind": "text_input", "label": "Email"},
                    {"kind": "password_input", "label": "Password"},
                    {"kind": "button", "label": "Continue"}],
        dom_after=[{"kind": "text_input", "label": "Email"},
                   {"kind": "dropdown", "label": "Workspace"},
                   {"kind": "button", "label": "Continue"}],
    )
    result = await QAWorkflow(
        boot_factory=_serving_boot, base_boot_factory=_base_boot,
    ).run(_Stand(), _Score(), tk)

    assert result.report["passed"] is False, "a regression must fail the run"
    regressions = [f for f in result.findings if f["category"] == "unexpected_regression"]
    assert regressions, "the disappeared element must be reported"
    assert tk.metrics.baseline_skipped is False



@pytest.mark.asyncio
async def test_coordinare_evidence_floor_still_gates_the_workflow_output():
    """FR-007: the workflow owns HOW, coordinare owns what counts as valid."""
    from coordinare.services.qa_verdict import qa_unsubstantiated_reason

    tk = _Toolkit(
        plan=TestPlan(checks=[PlanCheck(id="c1", criterion=CRIT, kind="command", command="pytest -q")]),
        judge=JudgeOutput(criteria=[{"criterion": CRIT, "passed": True}]),
    )
    result = await QAWorkflow().run(_Stand(), _Score(), tk)

    # A genuine pass with real evidence is substantiated.
    assert qa_unsubstantiated_reason(result.report) is None

    # A "pass" claiming criteria while passing none must NOT survive the floor.
    hollow = dict(result.report)
    hollow["criteria_passed"] = 0
    assert qa_unsubstantiated_reason(hollow) == "zero_criteria_passed (0 of 1)"

    # And a visual run with no captured evidence is refused.
    visual = dict(result.report)
    visual["visual_validation_required"] = True
    visual["visual_evidence"] = []
    assert qa_unsubstantiated_reason(visual) == "missing_visual_evidence"


@pytest.mark.asyncio
async def test_a_visual_plan_with_no_serving_app_reports_it_could_not_verify():
    """Found by the first live eval run: the model plans relative targets, and
    resolving them needs a serving app. Without one, the honest answer is
    "could not verify" -- not a verdict about a page nobody visited."""
    from performer.workflows.qa.boot import AppBoot

    tk = _Toolkit(
        plan=TestPlan(checks=[PlanCheck(id="c1", criterion=CRIT, kind="visual")],
                      surfaces=["/signin"]),
        judge=JudgeOutput(),
    )
    def dead(ws):  # no PORT -> never serves
        return AppBoot(env={}, workspace=ws)


    result = await QAWorkflow(boot_factory=dead).run(_Stand(), _Score(), tk)

    assert result.report["passed"] is False
    # The reason must be ACTIONABLE, not merely present. "never came up" once
    # covered a missing PORT, an unrecognised project, a crash and a slow boot
    # alike -- four fixes, one message.
    assert "PORT" in result.report["environment_error"]
    assert "workflow_env" in result.report["environment_error"]


@pytest.mark.asyncio
async def test_a_required_baseline_that_is_unavailable_cannot_report_a_pass():
    """Integrity rule, found by the live regression run.

    The report came back `passed: True` while also carrying a baseline error.
    That is contradictory: with no baseline the delta is unknown, so nothing is
    known about regressions, and a consumer reading `passed` would advance an
    unverified run. Same family as spec 120's false-pass problem.

    The criteria may well be demonstrated. The RUN still is not a pass.
    """
    from performer.workflows.qa.boot import AppBoot

    tk = _Toolkit(
        plan=TestPlan(checks=[PlanCheck(id="c1", criterion=CRIT, kind="visual")],
                      surfaces=["/signin"]),
        judge=JudgeOutput(criteria=[{"criterion": CRIT, "passed": True}]),
    )

    def _dead_base(worktree):  # base app never comes up
        return AppBoot(env={}, workspace=worktree)

    result = await QAWorkflow(
        boot_factory=_serving_boot, base_boot_factory=_dead_base,
    ).run(_Stand(), _Score(), tk)

    assert result.report["passed"] is False, (
        "a required baseline that could not be captured must not report a pass"
    )
    assert result.report["environment_error"]


@pytest.mark.asyncio
async def test_the_base_app_inherits_the_head_app_configuration():
    """It is the same application, on a different port from a different tree.
    Deriving base env from os.environ instead loses the start command."""
    from performer.workflows.qa import _default_base_boot

    head_env = {"PORT": "8000", "QA_APP_START_COMMAND": "python app.py", "FOO": "bar"}
    base = _default_base_boot(Path("/w/.base"), head_env)

    assert base.env["QA_APP_START_COMMAND"] == "python app.py"
    assert base.env["FOO"] == "bar"
    assert base.env["PORT"] != "8000", "the base app needs its own port"


@pytest.mark.asyncio
async def test_a_visual_run_carries_boot_proof_through_to_the_report():
    """Defect 16, end to end.

    Without app_boot_check backed by a real executed check, coordinare's 088
    floor drops a visual run's screenshots from the evidence count and folds it
    into the unsubstantiated gate -- a correct QA run rejected by its own side.
    """
    tk = _Toolkit(
        plan=TestPlan(checks=[PlanCheck(id="c1", criterion=CRIT, kind="visual")],
                      surfaces=["http://localhost:3000/"]),
        judge=JudgeOutput(criteria=[{"criterion": CRIT, "passed": True}]),
        dom_before=[{"kind": "button", "label": "Continue"}],
        dom_after=[{"kind": "button", "label": "Continue"}],
    )

    result = await QAWorkflow(
        boot_factory=_serving_boot, base_boot_factory=_base_boot,
    ).run(_Stand(), _Score(), tk)

    boot = result.report.get("app_boot_check")
    assert boot, "a visual run must carry boot proof"
    commands = [c["command"] for c in result.report["executed_checks"]]
    assert boot["command"] in commands, "boot proof must reference a check that ran"
    assert boot["exit_code"] == 0


@pytest.mark.asyncio
async def test_the_app_and_worktree_are_released_even_when_a_step_raises():
    """Adversarial review, critical.

    A raise between boot and the report used to leak the app server -- holding
    PORT across subsequent runs -- and the baseline worktree, which wedges later
    git operations in that workspace. Cleanup belongs in finally, not on the
    happy path.
    """
    released = {"boot": False, "worktree": False}

    class _ExplodingToolkit(_Toolkit):
        async def dom_snapshot(self, url):
            raise RuntimeError("browser died mid-run")

        async def run_command(self, cmd, *, cwd=None, timeout_s=300, plan_check_id=""):
            if "worktree remove" in cmd:
                released["worktree"] = True
            return await super().run_command(
                cmd, cwd=cwd, timeout_s=timeout_s, plan_check_id=plan_check_id,
            )

    def _tracked_boot(workspace):
        from performer.workflows.qa.boot import AppBoot

        boot = AppBoot(env={"PORT": "8000"}, workspace=workspace,
                       port_check=lambda _h, _p: True)
        original = boot.shutdown

        def _shutdown():
            released["boot"] = True
            original()

        boot.shutdown = _shutdown
        return boot

    tk = _ExplodingToolkit(
        plan=TestPlan(checks=[PlanCheck(id="c1", criterion=CRIT, kind="visual")],
                      surfaces=["http://localhost:3000/"]),
        judge=JudgeOutput(),
    )

    # The raise is the point; the cleanup is what is asserted.
    with contextlib.suppress(Exception):
        await QAWorkflow(
            boot_factory=_tracked_boot, base_boot_factory=_base_boot,
        ).run(_Stand(), _Score(), tk)

    assert released["boot"], "a launched app server must not leak its PORT"
    assert released["worktree"], "a baseline worktree must not be left behind"


@pytest.mark.asyncio
async def test_the_run_reports_whether_it_reached_green():
    """T047 / the spec's success metric.

    Rounds-to-green is per ISSUE, so one run can only contribute its ordinal
    and whether it went green -- coordinare aggregates. Emitting it here is what
    makes the metric measurable instead of inferred from board history later.
    """
    passing = _Toolkit(
        plan=TestPlan(checks=[PlanCheck(id="c1", criterion=CRIT, kind="command", command="pytest -q")]),
        judge=JudgeOutput(criteria=[{"criterion": CRIT, "passed": True}]),
    )
    result = await QAWorkflow().run(_Stand(), _Score(), passing)
    assert result.metrics.reached_green is True
    assert result.metrics.round_number >= 1

    failing = _Toolkit(
        plan=TestPlan(checks=[PlanCheck(id="c1", criterion=CRIT, kind="command", command="pytest -q")]),
        judge=JudgeOutput(criteria=[{"criterion": CRIT, "passed": True}]),
        exit_codes={"pytest": 1},
    )
    result = await QAWorkflow().run(_Stand(), _Score(), failing)
    assert result.metrics.reached_green is False


@pytest.mark.asyncio
async def test_the_default_boot_env_includes_the_env_cache_activation():
    """Second review round, by hand.

    The legacy QA path composes its capture env as {**os.environ, **stand.cache_env}
    (main.py, _cap_env). cache_env is the delta from sourcing the env cache's
    activate.sh -- it is where PORT, the toolchain PATH and the service vars
    live. The workflow's default boot used bare os.environ and never saw any of
    it: in production every visual plan would have died with "never came up".
    """
    captured = {}

    class _CacheStand:
        path = "/workspace"
        cache_env: ClassVar[dict[str, str]] = {"PORT": "4567", "PATH": "/devenv/.rbenv/bin:/usr/bin", "RAILS_ENV": "test"}

    def _spy_factory(workspace, env):
        captured.update(env)
        from performer.workflows.qa.boot import AppBoot

        return AppBoot(env=env, workspace=workspace, port_check=lambda _h, _p: True)

    tk = _Toolkit(
        plan=TestPlan(checks=[PlanCheck(id="c1", criterion=CRIT, kind="command", command="x")]),
        judge=JudgeOutput(criteria=[{"criterion": CRIT, "passed": True}]),
    )
    await QAWorkflow(boot_factory=_spy_factory).run(_CacheStand(), _Score(), tk)

    assert captured.get("PORT") == "4567", "cache_env must reach the boot env"
    assert captured.get("RAILS_ENV") == "test"
    assert "/devenv/.rbenv/bin" in captured.get("PATH", ""), "the cached toolchain must be on PATH"


@pytest.mark.asyncio
async def test_cache_env_overrides_the_process_env_like_the_legacy_path():
    """{**os.environ, **cache_env}: the cache wins, exactly as _cap_env does."""
    import os

    captured = {}

    class _CacheStand:
        path = "/workspace"
        cache_env: ClassVar[dict[str, str]] = {"PORT": "9001"}

    def _spy_factory(workspace, env):
        captured.update(env)
        from performer.workflows.qa.boot import AppBoot

        return AppBoot(env=env, workspace=workspace, port_check=lambda _h, _p: True)

    tk = _Toolkit(
        plan=TestPlan(checks=[PlanCheck(id="c1", criterion=CRIT, kind="command", command="x")]),
        judge=JudgeOutput(criteria=[{"criterion": CRIT, "passed": True}]),
    )
    os.environ["PORT"] = "1111"
    try:
        await QAWorkflow(boot_factory=_spy_factory).run(_CacheStand(), _Score(), tk)
    finally:
        del os.environ["PORT"]

    assert captured["PORT"] == "9001", "cache_env must override the process env"


@pytest.mark.asyncio
async def test_operator_workflow_env_wins_over_the_cache_env():
    """The operator channel for the app boot settings. They are stating intent
    about THIS project's app, so their value beats the cache's."""
    captured = {}

    class _CacheStand:
        path = "/workspace"
        cache_env: ClassVar[dict[str, str]] = {"PORT": "4567"}

    class _OperatorScore(_Score):
        workflow_env: ClassVar[dict[str, str]] = {"PORT": "3000", "QA_APP_START_COMMAND": "bin/rails s -p 3000"}

    def _spy_factory(workspace, env):
        captured.update(env)
        from performer.workflows.qa.boot import AppBoot

        return AppBoot(env=env, workspace=workspace, port_check=lambda _h, _p: True)

    tk = _Toolkit(
        plan=TestPlan(checks=[PlanCheck(id="c1", criterion=CRIT, kind="command", command="x")]),
        judge=JudgeOutput(criteria=[{"criterion": CRIT, "passed": True}]),
    )
    await QAWorkflow(boot_factory=_spy_factory).run(_CacheStand(), _OperatorScore(), tk)

    assert captured["PORT"] == "3000"
    assert captured["QA_APP_START_COMMAND"] == "bin/rails s -p 3000"


@pytest.mark.asyncio
async def test_every_step_emits_a_durable_event_in_order():
    """FR-008, finally enforced. Round-two review found zero emit() calls in
    the workflow: an operator polling get_status() at 3am saw progress="running"
    regardless of step, and drain_events() returned nothing."""
    tk = _Toolkit(
        plan=TestPlan(checks=[PlanCheck(id="c1", criterion=CRIT, kind="visual")],
                      surfaces=["http://localhost:3000/"]),
        judge=JudgeOutput(criteria=[{"criterion": CRIT, "passed": True}]),
        dom_before=[{"kind": "button", "label": "Continue"}],
        dom_after=[{"kind": "button", "label": "Continue"}],
    )
    await QAWorkflow(boot_factory=_serving_boot, base_boot_factory=_base_boot).run(
        _Stand(), _Score(), tk,
    )
    steps = [e.text for e in tk.events if e.text.startswith("qa.")]
    assert steps == [
        "qa.plan", "qa.boot", "qa.baseline", "qa.execute", "qa.observe", "qa.judge", "qa.report",
    ], steps


@pytest.mark.asyncio
async def test_a_command_only_plan_skips_the_boot_and_baseline_events():
    tk = _Toolkit(
        plan=TestPlan(checks=[PlanCheck(id="c1", criterion=CRIT, kind="command", command="x")]),
        judge=JudgeOutput(criteria=[{"criterion": CRIT, "passed": True}]),
    )
    await QAWorkflow().run(_Stand(), _Score(), tk)
    steps = [e.text for e in tk.events if e.text.startswith("qa.")]
    assert "qa.boot" not in steps and "qa.baseline" not in steps
    assert steps[0] == "qa.plan" and steps[-1] == "qa.report"


@pytest.mark.asyncio
async def test_run_artifacts_never_land_inside_the_cloned_repository():
    """Round-two review, critical. .qa_flow_driver.py was written into the
    workspace root and never removed, and the baseline worktree was created
    inside it. Both showed as untracked; a later `git add .` would have
    committed harness internals into the PR."""
    seen = {}

    class _Spy(_Toolkit):
        async def run_command(self, cmd, *, cwd=None, timeout_s=300, plan_check_id=""):
            if "worktree add" in cmd:
                seen["worktree"] = cmd.split()[4]
            if "qa_flow_driver.py" in cmd:
                seen["driver"] = next(p for p in cmd.split() if p.endswith("qa_flow_driver.py"))
            return await super().run_command(
                cmd, cwd=cwd, timeout_s=timeout_s, plan_check_id=plan_check_id,
            )

    tk = _Spy(
        plan=TestPlan(
            checks=[PlanCheck(id="c1", criterion=CRIT, kind="flow",
                              steps=[{"action": "goto", "target": "/"}])],
            surfaces=["http://localhost:3000/"],
        ),
        judge=JudgeOutput(criteria=[{"criterion": CRIT, "passed": True}]),
        dom_before=[{"kind": "button", "label": "Continue"}],
        dom_after=[{"kind": "button", "label": "Continue"}],
    )
    wf = QAWorkflow(boot_factory=_serving_boot, base_boot_factory=_base_boot)
    await wf.run(_Stand(), _Score(), tk)

    workspace = Path(_Stand.path).resolve()
    for label in ("worktree", "driver"):
        assert label in seen, f"{label} was never used"
        assert not Path(seen[label]).resolve().is_relative_to(workspace), (
            f"{label} at {seen[label]} is inside the repository"
        )
    assert not Path(wf._scratch).exists(), "the scratch dir must be removed after the run"


@pytest.mark.asyncio
async def test_an_empty_plan_on_a_card_with_criteria_is_a_fail_not_an_environment_error():
    """Round-two review (verified on resume), via the cosmetic_noop fixture.

    Criteria exist and the planner could derive nothing to check them. That is
    "not demonstrated", which is a FAIL with one unmet_criterion per criterion --
    not an environmental problem. Reporting environment_error would let a
    no-op change dodge a verdict."""
    tk = _Toolkit(plan=TestPlan(checks=[]), judge=JudgeOutput())
    result = await QAWorkflow().run(_Stand(), _Score(), tk)

    assert result.report["passed"] is False
    assert "environment_error" not in result.report
    assert result.report["criteria_checked"] == 1
    assert [f["category"] for f in result.findings] == ["unmet_criterion"]
    assert CRIT in result.findings[0]["criterion"]


@pytest.mark.asyncio
async def test_an_empty_plan_with_no_criteria_at_all_is_a_refusal_not_an_env_error():
    """411 AC7: zero criteria is a defined verdict — a refused run, never a
    vacuous pass and never an environment problem (the app may be healthy)."""
    class _NoCriteria(_Score):
        acceptance_criteria: ClassVar[list[str]] = []

    tk = _Toolkit(plan=TestPlan(checks=[]), judge=JudgeOutput())
    result = await QAWorkflow().run(_Stand(), _NoCriteria(), tk)
    assert result.report["passed"] is False
    assert not result.report.get("environment_error"), (
        "zero criteria is not an environment failure"
    )
    assert "unmet_criterion" in [f["category"] for f in result.findings]


@pytest.mark.asyncio
async def test_a_surface_that_renders_nothing_on_both_sides_is_reported_not_compared():
    """Round-two review (verified on resume): a planned surface that yields zero
    elements before AND after -- a 404 that renders an empty shell, a wrong
    route -- compared [] to [] and read as 'no regressions'. Nothing observed is
    not the same as nothing changed."""
    tk = _Toolkit(
        plan=TestPlan(checks=[PlanCheck(id="c1", criterion=CRIT, kind="visual")],
                      surfaces=["http://localhost:3000/missing"]),
        judge=JudgeOutput(criteria=[{"criterion": CRIT, "passed": True}]),
        dom_before=[], dom_after=[],
    )
    result = await QAWorkflow(boot_factory=_serving_boot, base_boot_factory=_base_boot).run(
        _Stand(), _Score(), tk,
    )
    cats = [f["category"] for f in result.findings]
    assert "step_unavailable" in cats, "an unobservable surface must be surfaced, not silently passed"
    assert any("missing" in f.get("observed", "") for f in result.findings)


# --- 367: a repository nobody can characterise stops the run --------------

@pytest.mark.asyncio
async def test_a_repository_the_model_cannot_characterise_is_an_environment_error():
    """The floor #367 asks both workflows to keep, taken from QA's own posture.

    QA was the only workflow in the #364 inventory that failed loudly rather
    than degrading, and the issue is explicit that this is the part worth
    keeping when the judgement moves to the model. Proceeding with no start
    command means booting nothing and reporting whatever the checks say about a
    page that never came up -- a verdict, produced from an empty picture.

    This survived a mutation run in which the stop was replaced by
    ``boot.shape = None``: all 291 tests passed while QA guessed.
    """
    class _Unknowable(_Toolkit):
        async def call_model(self, *, persona, schema, content, budget):
            if schema is ProjectShape:
                return ProjectShape(cannot_determine="no manifest and no layout I recognise")
            return await super().call_model(persona=persona, schema=schema, content=content, budget=budget)

    tk = _Unknowable(
        plan=TestPlan(checks=[PlanCheck(id="c1", criterion=CRIT, kind="visual")], surfaces=["/signin"]),
        judge=JudgeOutput(criteria=[{"criterion": CRIT, "passed": True}]),
    )
    result = await QAWorkflow(boot_factory=_serving_boot, base_boot_factory=_base_boot).run(_Stand(), _Score(), tk)

    assert result.report["passed"] is not True, "a repository nobody understood must not produce a pass"
    assert result.report.get("could_not_verify") or result.report.get("environment_error"), result.report
    blob = json.dumps(result.report)
    assert "no manifest and no layout I recognise" in blob, "the operator needs the model's own reason"


@pytest.mark.asyncio
async def test_an_explicit_start_command_survives_a_reading_that_gave_up():
    """The override is the operator's answer, so the stop must not overrule it.

    They are stating intent about THIS project. QA skips the reading entirely
    when the override is set, so this run never asks and never stops.
    """
    asked = {"n": 0}

    class _Counting(_Toolkit):
        async def call_model(self, *, persona, schema, content, budget):
            if schema is ProjectShape:
                asked["n"] += 1
                return ProjectShape(cannot_determine="I have no idea what this is")
            return await super().call_model(persona=persona, schema=schema, content=content, budget=budget)

    def _override_boot(workspace, env=None):
        from performer.workflows.qa.boot import AppBoot
        return AppBoot(env={"PORT": "8000", "QA_APP_START_COMMAND": "bin/serve"},
                       workspace=workspace, port_check=lambda _h, _p: True)

    tk = _Counting(
        plan=TestPlan(checks=[PlanCheck(id="c1", criterion=CRIT, kind="visual")], surfaces=["/signin"]),
        judge=JudgeOutput(criteria=[{"criterion": CRIT, "passed": True}]),
    )
    result = await QAWorkflow(boot_factory=_override_boot, base_boot_factory=_base_boot).run(_Stand(), _Score(), tk)

    assert asked["n"] == 0, "an operator who answered already must not pay for a reading"
    assert result.report.get("environment_error") is None
