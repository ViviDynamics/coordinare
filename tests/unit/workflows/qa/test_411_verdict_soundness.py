"""411 — QA verdict soundness, coordinare-side half.

The workflow computes an honest verdict; these pin the seams that make the
verdict mean something: the plan schema (a check that cannot run is a schema
error, not a pass), the plan-binding normalisation, the boot gate (boot when a
check needs the app, not only when visuals do), the visual-capture seam, and
the HTTP assertion action.

The performer-side half — the post-processing gates over the workflow's own
verdict — lives in agent/performer/tests/unit/test_411_qa_verdict_integrity.py.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import ClassVar

import pytest
from performer.workflows.models import ExecutedCheck
from performer.workflows.project_shape import ProjectShape
from performer.workflows.qa import QAWorkflow
from performer.workflows.qa.boot import AppBoot, resolve_target
from performer.workflows.qa.execute import (
    _evidence_name,
    collect_visual_evidence,
    prepare_visual_capture,
    rewrite_command_placeholders,
    run_execute_step,
)
from performer.workflows.qa.models import FlowStep, JudgeOutput, PlanCheck, TestPlan
from performer.workflows.qa.plan import run_plan_step
from pydantic import ValidationError

CRIT = "Users can sign in with a workspace selected"


# --- AC2: a check that cannot run is a schema error, not a pass ------------


def test_a_command_check_without_a_command_is_a_schema_error():
    """`check.command or "true"` made a malformed plan PASS: a command the
    model never wrote cannot demonstrate anything, and exit 0 from a command
    that was never written demonstrates the opposite of diligence."""
    with pytest.raises(ValidationError):
        PlanCheck(id="c1", criterion=CRIT, kind="command", command="   ")


def test_a_flow_check_without_steps_is_a_schema_error():
    """A flow with zero steps ran the driver on nothing, got exit 0, and the
    criterion read as demonstrated. The schema must refuse it."""
    with pytest.raises(ValidationError):
        PlanCheck(id="c1", criterion=CRIT, kind="flow", steps=[])


def test_a_command_check_with_a_command_still_constructs():
    PlanCheck(id="c1", criterion=CRIT, kind="command", command="pytest -q")


def test_a_flow_check_with_steps_still_constructs():
    PlanCheck(id="c1", criterion=CRIT, kind="flow",
              steps=[FlowStep(action="goto", target="/signin")])


def test_a_bare_visual_check_is_not_a_schema_error():
    """Visual checks observe pages; the harness appends the screenshot step
    itself (411 AC5), so a bare visual check is a plan to look at a page,
    not a malformed one."""
    PlanCheck(id="c1", criterion=CRIT, kind="visual")


# --- AC3: binding uses the judge's normalisation ---------------------------


@pytest.mark.asyncio
async def test_criterion_binding_survives_whitespace_and_case():
    """plan.py bound checks with exact string equality while the judge
    normalises (' '.join(text.split()).casefold()). A plan quoting the
    criterion with different spacing dropped every check as 'unbound', the
    plan failed closed, and the run reported a failure that described a
    perfectly good plan."""
    class _Score:
        acceptance_criteria: ClassVar[list[str]] = [CRIT]
        pr_diff = "diff"
        description = "add workspace selector"

    class _TK:
        async def call_model(self, *, persona, schema, content, budget):
            return TestPlan(checks=[
                PlanCheck(id="c1", criterion="  users   CAN sign in with a WORKSPACE selected ",
                          kind="command", command="pytest"),
            ])

    result = await run_plan_step(_TK(), _Score())

    assert [c.id for c in result.checks] == ["c1"], (
        "the criterion is the judge's normalisation of the same text; the "
        "check must bind"
    )


@pytest.mark.asyncio
async def test_a_card_with_no_criteria_refuses_before_the_model_answers():
    """411 round-eight review: with acceptance_criteria=[] the binder's
    `if criteria` guard skipped binding entirely, so any planner output was
    accepted and executed — arbitrary checks running under an explicit
    zero-criteria refusal (AC7). The refusal fires before the model."""
    from performer.workflows.qa.plan import EmptyPlan

    class _Score:
        acceptance_criteria: ClassVar[list[str]] = []
        pr_diff = ""
        description = "no criteria stated"

    class _TK:
        async def call_model(self, **_kw):
            raise AssertionError(
                "the zero-criteria refusal must fire before any model call"
            )

    with pytest.raises(EmptyPlan) as exc_info:
        await run_plan_step(_TK(), _Score())

    assert exc_info.value.criteria == [], (
        "empty criteria route to the zero-criteria refusal"
    )


# --- AC4: boot when a check needs the app, not only when visuals do --------


def _plan_with_command(command: str) -> TestPlan:
    return TestPlan(checks=[PlanCheck(id="c1", criterion=CRIT, kind="command", command=command)])


def test_a_visual_plan_needs_the_server():
    from performer.workflows.qa.boot import plan_needs_server

    plan = TestPlan(
        checks=[PlanCheck(id="c1", criterion=CRIT, kind="visual")],
        surfaces=["http://localhost:3000/"],
    )
    assert plan_needs_server(plan, "http://127.0.0.1:8000/")


def test_a_command_check_that_curls_the_app_needs_the_server():
    """`curl http://localhost:8000/health` against nothing connects to
    nothing. The check fails, the criterion reads as unmet, and the verdict
    says the code is broken when the truth is there was no server to ask."""
    from performer.workflows.qa.boot import plan_needs_server

    plan = _plan_with_command("curl http://127.0.0.1:8000/health")
    assert plan_needs_server(plan, "http://127.0.0.1:8000/")


def test_a_command_check_using_the_base_url_placeholder_needs_the_server():
    from performer.workflows.qa.boot import plan_needs_server

    plan = _plan_with_command("curl $BASE_URL/api/status")
    assert plan_needs_server(plan, "http://127.0.0.1:8000/")


def test_a_pure_command_check_does_not_need_the_server():
    from performer.workflows.qa.boot import plan_needs_server

    assert not plan_needs_server(
        _plan_with_command("pytest -q"), "http://127.0.0.1:8000/"
    )


def test_a_command_mentioning_localhost_in_a_path_does_not_need_the_server():
    """411 review: the reference is structural, not a substring. A test file
    named test_localhost.py tests the code, not the server."""
    from performer.workflows.qa.boot import plan_needs_server

    assert not plan_needs_server(
        _plan_with_command("pytest tests/test_localhost.py && echo 127.0.0.1"),
        "http://127.0.0.1:8000/",
    )


def test_an_empty_plan_does_not_need_the_server():
    from performer.workflows.qa.boot import plan_needs_server

    assert not plan_needs_server(TestPlan(checks=[]), "http://127.0.0.1:8000/")


@pytest.mark.asyncio
async def test_the_health_path_is_configurable():
    """A 404 on / is not 'not serving'. The health check hits a path the
    operator can set, and the default accepts ANY HTTP response: curl exit 0
    means the app answered, which is what 'up' means for a wrong path."""
    ran: list[str] = []

    class _TK:
        async def run_command(self, cmd, **kw):
            ran.append(cmd)

            class R:
                passed = True
                exit_code = 0
                output_excerpt = ""
                command = cmd

            return R()

    boot = AppBoot(
        env={"PORT": "8000", "QA_APP_HEALTH_PATH": "/healthz"},
        workspace=Path("/w"), port_check=lambda h, p: True,
    )
    url = await boot.ensure_serving(_TK())

    assert url == "http://127.0.0.1:8000/"
    curl = next(c for c in ran if c.startswith("curl"))
    assert "/healthz" in curl, "the configured health path must be used"


@pytest.mark.asyncio
async def test_the_default_health_check_does_not_demand_a_2xx():
    """curl without -f exits 0 for ANY HTTP response. A 404 on the wrong
    health path is an app that is up and answering -- exactly what a boot
    proof needs, and what `-fsS` used to reject."""
    ran: list[str] = []

    class _TK:
        async def run_command(self, cmd, **kw):
            ran.append(cmd)

            class R:
                passed = True
                exit_code = 0
                output_excerpt = ""
                command = cmd

            return R()

    boot = AppBoot(
        env={"PORT": "8000"}, workspace=Path("/w"), port_check=lambda h, p: True,
    )
    await boot.ensure_serving(_TK())

    curl = next(c for c in ran if c.startswith("curl"))
    assert " -f" not in f" {curl}", "any HTTP response is boot proof; -f demands 2xx"


# --- AC4: an explicit no-server verdict for library/CLI projects ------------


class _Stand:
    path = "/workspace"


class _Score:
    acceptance_criteria: ClassVar[list[str]] = [CRIT]
    pr_diff = "diff"
    description = "add workspace selector"
    base_branch = "main"
    role = "qa"


class _Toolkit:
    """Same scaffold as test_workflow_end_to_end.py."""

    def __init__(self, *, plan, judge, exit_codes=None, dom_before=None, dom_after=None, shape=None):
        from performer.workflows.base import WorkflowMetrics

        self.metrics = WorkflowMetrics()
        self.events = []
        self._plan, self._judge = plan, judge
        self._shape = shape
        self._exit_codes = exit_codes or {}
        self._dom_before, self._dom_after = dom_before or [], dom_after or []
        self._snapshots = 0
        self.ran: list[str] = []

    async def call_model(self, *, persona, schema, content, budget):
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
        self._snapshots += 1
        return self._dom_before if ":9999" in url else self._dom_after


def _library_shape():
    return ProjectShape(
        project_name="lib", summary="a Go library",
        test_command="go test ./...", start_command="", boot_seconds=30,
        source_dirs=["src"],
    )


@pytest.mark.asyncio
async def test_a_no_server_shape_skips_boot_with_an_explicit_verdict():
    """The shape reading already knows 'library, not browsable'. Running the
    checks anyway produces either a fake pass (exit 0 from nothing) or a
    'connection refused' defect neither of which is a verdict about the
    code. The honest verdict names the absence of a server."""
    tk = _Toolkit(
        plan=_plan_with_command("curl $BASE_URL/api"),
        judge=JudgeOutput(criteria=[{"criterion": CRIT, "passed": True}]),
        shape=_library_shape(),
    )
    result = await QAWorkflow().run(_Stand(), _Score(), tk)

    assert result.report["passed"] is False
    message = str(result.report.get("environment_error") or "")
    assert "no server" in message.lower(), (
        "the verdict must say the project has no server, not fail mysteriously"
    )
    assert all("curl" not in cmd for cmd in tk.ran), (
        "checks must not run against a server that does not exist"
    )


# --- AC5: visual checks capture evidence ------------------------------------


@pytest.mark.asyncio
async def test_visual_checks_capture_a_screenshot(tmp_path):
    """The workflow declared visual_validation_required while capturing
    nothing: report.py received visual_evidence=None from every caller, and
    the coordinare evidence floor then bounced every visual run. The capture
    is the workflow's job."""
    plan = TestPlan(
        checks=[PlanCheck(id="c1", criterion=CRIT, kind="visual")],
        surfaces=["http://localhost:3000/"],
    )
    prepare_visual_capture(plan, tmp_path)

    class _TK:
        def __init__(self):
            self.ran: list[str] = []

        async def run_command(self, cmd, *, cwd=None, timeout_s=300, plan_check_id=""):
            self.ran.append(cmd)
            return ExecutedCheck.from_result(cmd, 0, "", plan_check_id=plan_check_id)

    tk = _TK()
    await run_execute_step(tk, plan, cwd=tmp_path, driver_path=str(tmp_path / "d.py"))

    assert tk.ran, "the visual check must actually run"
    import shlex

    steps = json.loads(shlex.split(tk.ran[0])[2])
    screenshot_steps = [s for s in steps if s["action"] == "screenshot"]
    assert screenshot_steps, "a visual check must capture a screenshot"
    assert screenshot_steps[0]["target"].startswith(str(tmp_path)), (
        "the screenshot lands in the evidence directory"
    )
    assert steps[0]["action"] == "goto" and steps[0]["target"] == "http://localhost:3000/", (
        "the driver starts on about:blank: the check must navigate before it captures"
    )


def test_a_model_written_screenshot_target_is_normalised(tmp_path):
    """411 review: collect_visual_evidence looks in exactly one place per
    check. A screenshot step the model wrote with its own target would be
    captured but never collected, so the evidence floor bounces the run."""
    plan = TestPlan(
        checks=[PlanCheck(
            id="c1", criterion=CRIT, kind="visual",
            steps=[FlowStep(action="screenshot", target="/tmp/custom.png")],
        )],
        surfaces=["http://localhost:3000/"],
    )

    prepare_visual_capture(plan, tmp_path)

    assert plan.checks[0].steps[-1].target == str(tmp_path / _evidence_name("c1"))
    assert plan.checks[0].steps[0].action == "goto"


def test_collect_visual_evidence_only_reports_files_that_exist(tmp_path):
    plan = TestPlan(
        checks=[PlanCheck(id="c1", criterion=CRIT, kind="visual")],
        surfaces=["http://localhost:3000/"],
    )
    prepare_visual_capture(plan, tmp_path)
    png = tmp_path / _evidence_name("c1")
    png.write_bytes(b"png")

    evidence = collect_visual_evidence(plan, tmp_path)

    assert evidence == [{
        "label": "visual c1",
        "kind": "screenshot",
        "path_or_url": str(png),
    }], "only files that exist are evidence"


def test_collect_visual_evidence_says_nothing_when_no_file_landed(tmp_path):
    plan = TestPlan(
        checks=[PlanCheck(id="c1", criterion=CRIT, kind="visual")],
        surfaces=["http://localhost:3000/"],
    )
    prepare_visual_capture(plan, tmp_path)
    assert collect_visual_evidence(plan, tmp_path) == []


# --- AC6: a post-change observe failure is recorded, not fatal --------------


@pytest.mark.asyncio
async def test_a_post_change_observe_failure_is_recorded_not_fatal():
    """dom.read_dom raises RuntimeError on a navigation timeout. The baseline
    observe is wrapped (baseline_error); the post-change observe at the same
    page was not, so a flaky networkidle blew the whole run to the caller
    instead of reporting the surface as unobservable."""
    from performer.workflows.qa.boot import AppBoot

    class _BoomToolkit(_Toolkit):
        async def dom_snapshot(self, url):
            self._snapshots += 1
            if self._snapshots > 1:
                raise RuntimeError("Page.goto: net::ERR_CONNECTION_REFUSED")
            return [{"kind": "text_input", "label": "Email"}]

    tk = _BoomToolkit(
        plan=TestPlan(
            checks=[PlanCheck(id="c1", criterion=CRIT, kind="visual")],
            surfaces=["http://localhost:3000/"],
        ),
        judge=JudgeOutput(criteria=[{"criterion": CRIT, "passed": True}]),
        dom_before=[{"kind": "text_input", "label": "Email"}],
        dom_after=[{"kind": "text_input", "label": "Email"}],
        shape=ProjectShape(
            project_name="app", summary="a web app",
            test_command="npm test", start_command="npm start", boot_seconds=30,
            source_dirs=["src"],
        ),
    )
    result = await QAWorkflow(
        boot_factory=lambda ws: AppBoot(
            env={"PORT": "8000"}, workspace=ws, port_check=lambda h, p: True,
        ),
        base_boot_factory=lambda wt: AppBoot(
            env={"PORT": "9999"}, workspace=wt, port_check=lambda h, p: True,
        ),
    ).run(_Stand(), _Score(), tk)

    unobservable = [f for f in result.findings if f["category"] == "step_unavailable"]
    assert unobservable, "the unobservable surface must be reported"
    assert "http://localhost:3000/" in str(unobservable[0])


@pytest.mark.asyncio
async def test_an_empty_shell_surface_fails_the_run_closed():
    """411 round-two review: the `not before and not after` path appended the
    finding but did not latch, so a judge pass on an unobservable surface
    still reported passed=true and post-processing could accept the run."""
    from performer.workflows.qa.boot import AppBoot

    class _EmptyShellToolkit(_Toolkit):
        def __init__(self, **kw):
            super().__init__(**kw)
            self._snapshots = 0

        async def dom_snapshot(self, _url):
            self._snapshots += 1
            return []  # both before and after render nothing: a 404 shell

    tk = _EmptyShellToolkit(
        plan=TestPlan(
            checks=[PlanCheck(id="c1", criterion=CRIT, kind="visual")],
            surfaces=["http://localhost:3000/"],
        ),
        judge=JudgeOutput(criteria=[{"criterion": CRIT, "passed": True}]),
        dom_before=[],
        dom_after=[],
        shape=ProjectShape(
            project_name="app", summary="a web app",
            test_command="npm test", start_command="npm start", boot_seconds=30,
            source_dirs=["src"],
        ),
    )
    result = await QAWorkflow(
        boot_factory=lambda ws: AppBoot(
            env={"PORT": "8000"}, workspace=ws, port_check=lambda h, p: True,
        ),
        base_boot_factory=lambda wt: AppBoot(
            env={"PORT": "9999"}, workspace=wt, port_check=lambda h, p: True,
        ),
    ).run(_Stand(), _Score(), tk)

    assert result.report["passed"] is False, (
        "a surface that never rendered anything is not a verified pass"
    )
    assert "visual_capture_dir" in result.report, (
        "a visual run hands its evidence tree to the consumer for deletion"
    )


# --- AC9: HTTP assertions in flows ------------------------------------------


def test_flow_action_accepts_an_http_assertion():
    step = FlowStep(
        action="http_assert",
        target="/api/health",
        value=json.dumps({"status": 200, "json_path": "$.ok", "equals": True}),
    )
    assert step.action == "http_assert"


def test_http_assert_targets_resolve_like_other_urls():
    assert resolve_target("/api/health", "http://127.0.0.1:8000/") == (
        "http://127.0.0.1:8000/api/health"
    )


def test_http_assert_targets_are_rewritten_against_the_base_url():
    plan = TestPlan(checks=[
        PlanCheck(id="c1", criterion=CRIT, kind="flow", steps=[
            FlowStep(action="http_assert", target="/api/health", value="{}"),
        ]),
    ])
    AppBoot.rewrite_targets(plan, "http://127.0.0.1:8000/")
    assert plan.checks[0].steps[0].target == "http://127.0.0.1:8000/api/health", (
        "an http_assert target is a URL, not a selector"
    )


def test_the_driver_implements_http_assertions():
    """The driver is the executor: the action must be implemented there, and
    via the Playwright request API (no page navigation, no browser chrome)."""
    from performer.workflows.qa.execute import flow_driver_source

    src = flow_driver_source()
    assert "http_assert" in src
    assert "request.get" in src, "assertions go through the request context, not page.goto"


def test_the_driver_refuses_an_assertion_that_asserts_nothing():
    """411 review: an empty spec performs the request and checks nothing, so
    an HTTP 500 would pass. The driver must refuse it before the request."""
    from performer.workflows.qa.execute import flow_driver_source

    src = flow_driver_source()
    assert "must assert at least one thing" in src
    assert "not (path_expr and equals is not None)" in src


def test_the_driver_refuses_an_empty_contains_assertion():
    """411 round-seven review: an empty contains is in every response,
    including an HTTP 500's — it must read as asserting nothing."""
    from performer.workflows.qa.execute import flow_driver_source

    src = flow_driver_source()
    assert "not contains.strip()" in src, (
        "an empty contains is treated as absent and the no-assertion refusal fires"
    )


@pytest.mark.asyncio
async def test_a_flow_only_plan_does_not_claim_visual_validation():
    """411 review: needs_baseline() is true for flow checks too, but only
    visual checks produce screenshots. Driving the visual-artifact floor off
    needs_baseline() would bounce a flow-only plan for evidence it never
    promised."""
    from performer.workflows.qa.boot import AppBoot

    tk = _Toolkit(
        plan=TestPlan(
            checks=[PlanCheck(id="c1", criterion=CRIT, kind="flow", steps=[
                FlowStep(action="http_assert", target="/api/health",
                         value=json.dumps({"status": 200})),
            ])],
        ),
        judge=JudgeOutput(criteria=[{"criterion": CRIT, "passed": True}]),
        shape=ProjectShape(
            project_name="app", summary="a web app",
            test_command="npm test", start_command="npm start", boot_seconds=30,
            source_dirs=["src"],
        ),
    )
    result = await QAWorkflow(
        boot_factory=lambda ws: AppBoot(
            env={"PORT": "8000"}, workspace=ws, port_check=lambda h, p: True,
        ),
        base_boot_factory=lambda wt: AppBoot(
            env={"PORT": "9999"}, workspace=wt, port_check=lambda h, p: True,
        ),
    ).run(_Stand(), _Score(), tk)

    assert result.report["visual_validation_required"] is False
    assert result.report["passed"] is True, "the judge passed the criterion"
    assert "app_start_command" in result.report, (
        "the shape reading rides in the report so the fallback capture boots the same command"
    )
    assert "visual_capture_dir" not in result.report, (
        "a command-only run creates no evidence tree and cleans up nothing"
    )


# --- 411 round-two review ---------------------------------------------------


def test_a_path_component_with_the_host_is_not_a_server_reference():
    """411 round-two review: `localhost/` inside a POSIX path matched the
    old `host/` alternative and forced a library run through shape
    detection. Only a host:port token or a scheme-qualified URL counts."""
    from performer.workflows.qa.boot import plan_needs_server

    assert not plan_needs_server(
        _plan_with_command("pytest tests/localhost/fixtures.py"),
        "http://127.0.0.1:8000/",
    )
    assert plan_needs_server(
        _plan_with_command("curl http://localhost:8000/health"),
        "http://127.0.0.1:8000/",
    )
    assert plan_needs_server(
        _plan_with_command("curl localhost:$PORT/health"),
        "http://127.0.0.1:8000/",
    )


def test_a_bare_visual_check_without_surfaces_captures_nothing(tmp_path):
    """411 round-two review: with no surface to navigate to the screenshot
    would capture about:blank and be collected as evidence of the app. It
    must not be captured at all."""
    plan = TestPlan(checks=[PlanCheck(id="c1", criterion=CRIT, kind="visual")])
    prepare_visual_capture(plan, tmp_path)
    assert plan.checks[0].steps == [], (
        "no navigation target means no capture: the fallback or the bounce decides"
    )


def test_a_visual_check_with_its_own_goto_is_still_captured(tmp_path):
    """A check the model gave its own navigation keeps it, and its screenshot
    target is still normalised into the evidence directory."""
    plan = TestPlan(
        checks=[PlanCheck(id="c1", criterion=CRIT, kind="visual", steps=[
            FlowStep(action="goto", target="http://127.0.0.1:8000/admin"),
            FlowStep(action="screenshot", target="shot.png"),
        ])],
    )
    prepare_visual_capture(plan, tmp_path)
    steps = plan.checks[0].steps
    assert [s.action for s in steps] == ["goto", "screenshot"], "no goto duplicated"
    assert steps[1].target == str(tmp_path / _evidence_name("c1")), (
        "the model's own screenshot target is normalised into the evidence dir"
    )


def test_a_screenshot_preceding_its_goto_is_moved_after_it(tmp_path):
    """411 round-three review: a model-supplied [screenshot, goto] kept that
    order and captured about:blank, which was then collected as valid
    evidence and skipped the fallback. The harness owns placement: the
    screenshot runs after every navigation."""
    plan = TestPlan(
        checks=[PlanCheck(id="c1", criterion=CRIT, kind="visual", steps=[
            FlowStep(action="screenshot", target="shot.png"),
            FlowStep(action="goto", target="http://127.0.0.1:8000/admin"),
        ])],
    )
    prepare_visual_capture(plan, tmp_path)
    steps = plan.checks[0].steps
    assert [s.action for s in steps] == ["goto", "screenshot"], "capture after navigation"
    assert steps[1].target == str(tmp_path / _evidence_name("c1"))


def test_a_path_traversal_check_id_stays_in_the_evidence_dir(tmp_path):
    """411 round-four review: the check id is model-controlled and becomes a
    filesystem path. A separator-bearing id must not escape the evidence
    directory for Playwright to write into."""
    plan = TestPlan(
        checks=[PlanCheck(id="../../etc/passwd", criterion=CRIT, kind="visual")],
        surfaces=["http://127.0.0.1:8000/"],
    )
    prepare_visual_capture(plan, tmp_path)
    target = plan.checks[0].steps[-1].target
    assert (tmp_path / target).parent == tmp_path, (
        "the capture stays inside the evidence dir whatever the id contains"
    )


def test_distinct_check_ids_never_share_an_evidence_file(tmp_path):
    """411 round-five review: slugification is not injective — a/b and a_b
    produced the same filename, so one check's capture overwrote the other's
    and both collected the same artifact as evidence."""
    plan = TestPlan(
        checks=[
            PlanCheck(id="a/b", criterion=CRIT, kind="visual"),
            PlanCheck(id="a_b", criterion=CRIT, kind="visual"),
        ],
        surfaces=["http://127.0.0.1:8000/"],
    )
    prepare_visual_capture(plan, tmp_path)
    names = [Path(c.steps[-1].target).name for c in plan.checks]
    assert len(set(names)) == len(names), "distinct ids land on distinct files"

    for name in names:
        (tmp_path / name).write_bytes(b"png")
    evidence = collect_visual_evidence(plan, tmp_path)
    assert len({e["path_or_url"] for e in evidence}) == len(plan.checks), (
        "each check collects its own capture"
    )


def test_duplicate_check_ids_are_a_plan_schema_error():
    """411 round-seven review: TestPlan does not enforce id uniqueness — two
    checks sharing an id share one evidence file, and one capture satisfied
    both. Refused at the boundary."""
    with pytest.raises(ValidationError, match="check ids must be unique"):
        TestPlan(checks=[
            PlanCheck(id="c1", criterion=CRIT, kind="visual"),
            PlanCheck(id="c1", criterion=CRIT, kind="visual"),
        ], surfaces=["http://127.0.0.1:8000/"])


def test_an_empty_check_id_is_a_plan_schema_error():
    with pytest.raises(ValidationError, match="check ids must be non-empty"):
        TestPlan(checks=[
            PlanCheck(id="", criterion=CRIT, kind="visual"),
        ], surfaces=["http://127.0.0.1:8000/"])


def test_an_http_assert_only_flow_needs_a_server_but_no_baseline():
    """411 round-seven review: an API-only flow performs no DOM comparison, so
    booting the merge base added a failure mode without buying comparison
    value — but the assertion does contact the app, so the head must boot."""
    from performer.workflows.qa.boot import plan_needs_server

    plan = TestPlan(checks=[PlanCheck(
        id="api", criterion=CRIT, kind="flow",
        steps=[FlowStep(
            action="http_assert", target="/api/health",
            value=json.dumps({"status": 200}),
        )],
    )])
    assert plan.needs_baseline() is False, "no DOM to compare"
    assert plan_needs_server(plan, None) is True, "the assertion contacts the app"

    ui_plan = TestPlan(checks=[PlanCheck(
        id="ui", criterion=CRIT, kind="flow",
        steps=[FlowStep(action="goto", target="/signin")],
    )])
    assert ui_plan.needs_baseline() is True, "a UI flow still compares against the base"


def test_a_command_only_plan_still_needs_no_baseline():
    plan = TestPlan(checks=[PlanCheck(id="c1", criterion=CRIT, kind="command", command="true")])
    assert plan.needs_baseline() is False


def test_every_model_screenshot_step_is_replaced_by_the_harness_capture(tmp_path):
    """411 round-five review: only the first model screenshot was rewritten;
    additional ones kept arbitrary targets, letting Playwright write outside
    the evidence dir. One harness-owned capture replaces them all."""
    plan = TestPlan(
        checks=[PlanCheck(
            id="c1", criterion=CRIT, kind="visual",
            steps=[
                FlowStep(action="screenshot", target="../../escape.png"),
                FlowStep(action="goto", target="http://127.0.0.1:8000/admin"),
                FlowStep(action="screenshot", target="/tmp/other.png"),
            ],
        )],
        surfaces=["http://127.0.0.1:8000/"],
    )
    prepare_visual_capture(plan, tmp_path)
    steps = plan.checks[0].steps
    assert [s.action for s in steps] == ["goto", "screenshot"], (
        "one harness-owned capture after the navigation"
    )
    assert (tmp_path / Path(steps[1].target)).parent == tmp_path, (
        "the capture is the managed evidence path"
    )


@pytest.mark.asyncio
async def test_an_aborted_visual_run_cleans_its_evidence_tree():
    """411 round-three review: the evidence tree lives outside the scratch,
    so a run that dies between capture and report leaked one qa-visual-*
    directory per attempt. The run's own cleanup removes it."""
    from performer.workflows.qa.boot import AppBoot

    class _ExplodingJudgeToolkit(_Toolkit):
        async def call_model(self, *, persona, schema, content, budget):
            if schema is JudgeOutput:
                raise RuntimeError("judge exploded mid-run")
            return await super().call_model(
                persona=persona, schema=schema, content=content, budget=budget
            )

    tk = _ExplodingJudgeToolkit(
        plan=TestPlan(
            checks=[PlanCheck(id="c1", criterion=CRIT, kind="visual")],
            surfaces=["http://localhost:3000/"],
        ),
        judge=JudgeOutput(criteria=[{"criterion": CRIT, "passed": True}]),
        shape=ProjectShape(
            project_name="app", summary="a web app",
            test_command="npm test", start_command="npm start", boot_seconds=30,
            source_dirs=["src"],
        ),
    )
    wf = QAWorkflow(
        boot_factory=lambda ws: AppBoot(
            env={"PORT": "8000"}, workspace=ws, port_check=lambda h, p: True,
        ),
        base_boot_factory=lambda wt: AppBoot(
            env={"PORT": "9999"}, workspace=wt, port_check=lambda h, p: True,
        ),
    )
    with pytest.raises(RuntimeError, match="judge exploded"):
        await wf.run(_Stand(), _Score(), tk)

    capture_dir = wf._capture_dir
    assert capture_dir is not None, "the visual run created the evidence tree"
    assert not capture_dir.exists(), "an aborted run removes its evidence tree"


def test_a_relative_surface_is_resolved_against_the_booted_origin(tmp_path):
    """411 round-six review: the planner permits relative routes as surfaces,
    and the harness-inserted goto is written by the harness — a raw `/route`
    is not a URL Playwright accepts, so the capture fails with an invalid URL
    and no screenshot is collected. Resolve it against the booted origin."""
    plan = TestPlan(
        checks=[PlanCheck(id="c1", criterion=CRIT, kind="visual")],
        surfaces=["/signin"],
    )
    prepare_visual_capture(plan, tmp_path, base_url="http://127.0.0.1:8000")

    assert plan.checks[0].steps[0].target == "http://127.0.0.1:8000/signin", (
        "the inserted goto navigates to the booted origin, not the raw route"
    )


def test_a_relative_surface_without_a_boot_stays_raw(tmp_path):
    """No boot, no origin: the bare check with a relative surface and no
    navigation target still gets no capture (round-two review), and a check
    with its own goto is untouched."""
    plan = TestPlan(
        checks=[PlanCheck(id="c1", criterion=CRIT, kind="visual", steps=[
            FlowStep(action="goto", target="/signin"),
        ])],
        surfaces=["/signin"],
    )
    prepare_visual_capture(plan, tmp_path, base_url=None)

    assert plan.checks[0].steps[0].target == "/signin", (
        "rewrite_targets owns model-supplied steps; the harness adds nothing"
    )


def test_command_placeholders_are_rewritten_to_the_booted_origin():
    """411 round-six review: plan_needs_server treats $BASE_URL/$PORT as app
    references and boots the app, but the placeholder itself was never
    resolved — `curl $BASE_URL/api/status` executed with an empty URL and
    failed its check despite a healthy boot."""
    plan = _plan_with_command("curl -sS $BASE_URL/api/status?port=$PORT")
    rewritten = rewrite_command_placeholders(plan, "http://127.0.0.1:8000")

    assert rewritten == 1
    cmd = plan.checks[0].command
    assert cmd == "curl -sS http://127.0.0.1:8000/api/status?port=8000", (
        "both placeholders resolve to the booted origin"
    )


def test_command_placeholder_rewrite_needs_a_booted_origin():
    plan = _plan_with_command("curl $BASE_URL/api/status")
    assert rewrite_command_placeholders(plan, None) == 0
    assert plan.checks[0].command == "curl $BASE_URL/api/status", (
        "without a booted origin there is nothing to resolve to"
    )


def test_a_hard_coded_local_url_is_rewritten_to_the_booted_origin():
    """411 round-eight review: plan_needs_server boots on a hard-coded
    loopback URL, but only the placeholders were rewritten — a command
    pinned to the app's default port hit a closed port after the boot and
    the connection failure read as a code defect."""
    plan = _plan_with_command("curl -f http://localhost:3000/api/health")
    rewritten = rewrite_command_placeholders(plan, "http://127.0.0.1:8123")

    assert rewritten == 1
    assert plan.checks[0].command == "curl -f http://127.0.0.1:8123/api/health", (
        "a hard-coded loopback URL normalizes onto the booted origin"
    )


def test_a_zero_byte_capture_is_not_evidence(tmp_path):
    """411 round-six review: a failed or interrupted capture leaves a
    zero-byte file; collecting it satisfied the visual evidence floor with
    no image. Only a real file is evidence."""
    plan = TestPlan(
        checks=[PlanCheck(id="c1", criterion=CRIT, kind="visual")],
        surfaces=["http://127.0.0.1:8000/"],
    )
    prepare_visual_capture(plan, tmp_path)
    (tmp_path / _evidence_name("c1")).write_bytes(b"")

    assert collect_visual_evidence(plan, tmp_path) == [], (
        "a zero-byte capture reads as capture-unavailable"
    )


def test_the_driver_supports_json_path_array_indices():
    """411 round-six review: the PLAN persona advertises JSON path checks, but
    the dot-split alone turned $.items[0].id into the literal key "items[0]"
    and failed every array assertion."""
    from performer.workflows.qa.execute import flow_driver_source

    src = flow_driver_source()
    assert 're.sub(r"\\[(\\d+)\\]", r".\\1"' in src, (
        "bracket indices normalise into the dot path"
    )


# --- 411 round-eight review threads -----------------------------------------


def test_a_bare_relative_surface_is_resolved_like_a_root_relative_one(tmp_path):
    """resolve_target supports bare relative routes (signin) as well as
    root-relative ones (/route); the harness-inserted goto must resolve both
    shapes, or a bare surface navigates nowhere."""
    plan = TestPlan(
        checks=[PlanCheck(id="c1", criterion=CRIT, kind="visual")],
        surfaces=["signin"],
    )
    prepare_visual_capture(plan, tmp_path, base_url="http://127.0.0.1:8000")

    goto = plan.checks[0].steps[0]
    assert goto.action == "goto"
    assert goto.target == "http://127.0.0.1:8000/signin", (
        "a bare relative surface resolves against the booted origin"
    )


def test_a_visual_check_with_nowhere_to_navigate_keeps_no_model_screenshot(tmp_path):
    """The no-navigation bail-out ran after the screenshot strip could skip
    it entirely — a model screenshot with an escape target survived on a
    check that gets no capture step. The strip must run before the
    bail-out."""
    plan = TestPlan(
        checks=[PlanCheck(
            id="c1", criterion=CRIT, kind="visual",
            steps=[FlowStep(action="screenshot", target="/tmp/escape.png")],
        )],
        surfaces=[],
    )

    prepare_visual_capture(plan, tmp_path, base_url="http://127.0.0.1:8000")

    assert plan.checks[0].steps == [], (
        "a model screenshot with an arbitrary target must not survive a check "
        "that gets no harness capture"
    )


def test_the_booted_origin_is_shell_quoted_in_rewritten_commands():
    """411 round-eight review: the command runs under `bash -c`, so a
    config-derived origin carrying shell syntax (a PROTOCOL like
    `http; touch x`) would become executable the moment it is interpolated
    textually. The substituted values must be quoted."""
    plan = _plan_with_command("curl -sS $BASE_URL/api/status")
    rewritten = rewrite_command_placeholders(
        plan, "http://127.0.0.1:8000/; touch /tmp/pwned"
    )

    assert rewritten == 1
    cmd = plan.checks[0].command
    assert "touch /tmp/pwned" in cmd, "the value is still interpolated"
    assert cmd.startswith("curl -sS 'http://127.0.0.1:8000/; touch /tmp/pwned'"), (
        "the interpolated origin must be single-quoted so the shell reads it "
        "as one inert word"
    )


def test_the_ordinary_origin_stays_unquoted():
    """shlex.quote is inert for the ordinary origin shape: the rewrite must
    not change what existing plans execute."""
    plan = _plan_with_command("curl -sS $BASE_URL/api/status?port=$PORT")
    rewrite_command_placeholders(plan, "http://127.0.0.1:8000")

    assert plan.checks[0].command == "curl -sS http://127.0.0.1:8000/api/status?port=8000"


@pytest.mark.asyncio
async def test_a_judge_reported_unexpected_change_latches_the_run_failed():
    """411 round-eight review: build_findings turned a judge-reported change
    beyond the claimed one into an unexpected_regression finding, but the
    passed flag ignored it — a report that says PASSED with a hard finding
    attached. The latch must include it."""
    tk = _Toolkit(
        plan=TestPlan(
            checks=[PlanCheck(id="c1", criterion=CRIT, kind="flow", steps=[
                FlowStep(action="http_assert", target="/api/health",
                         value=json.dumps({"status": 200})),
            ])],
        ),
        judge=JudgeOutput(
            criteria=[{"criterion": CRIT, "passed": True}],
            unexpected_changes=["a new config file appeared beyond the claim"],
        ),
        shape=ProjectShape(
            project_name="app", summary="a web app",
            test_command="npm test", start_command="npm start", boot_seconds=30,
            source_dirs=["src"],
        ),
    )
    result = await QAWorkflow(
        boot_factory=lambda ws: AppBoot(
            env={"PORT": "8000"}, workspace=ws, port_check=lambda h, p: True,
        ),
        base_boot_factory=lambda wt: AppBoot(
            env={"PORT": "9999"}, workspace=wt, port_check=lambda h, p: True,
        ),
    ).run(_Stand(), _Score(), tk)

    assert result.report["passed"] is False, (
        "a judge-reported unexpected change is a hard finding, not a pass"
    )
    assert any(
        f.get("category") == "unexpected_regression" for f in result.findings
    ), "the finding must reach the repair brief"
