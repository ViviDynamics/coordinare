"""T024 / T026 / T028 — plan, baseline and execute step behaviour."""
from __future__ import annotations

from pathlib import Path
from typing import ClassVar

import pytest
from performer.workflows.models import ExecutedCheck
from performer.workflows.qa.baseline import run_baseline_step
from performer.workflows.qa.execute import run_execute_step
from performer.workflows.qa.models import FlowStep, PlanCheck, TestPlan
from performer.workflows.qa.plan import EmptyPlan, run_plan_step

CRIT = "Users can sign in"


class _Score:
    acceptance_criteria: ClassVar[list[str]] = [CRIT]
    pr_diff = "diff --git a/x b/x"
    description = "add sign-in"


class _Toolkit:
    """Records what steps asked for, so behaviour is asserted not mocked away."""

    def __init__(self, *, plan=None, commands=None, dom=None):
        self._plan = plan
        self._commands = commands or {}
        self._dom = dom or {}
        self.ran: list[str] = []
        self.dom_calls: list[str] = []

    async def call_model(self, *, persona, schema, content, budget):
        return self._plan

    async def run_command(self, cmd, *, cwd=None, timeout_s=300, plan_check_id=""):
        self.ran.append(cmd)
        exit_code = self._commands.get(cmd, 0)
        for prefix, code in self._commands.items():
            if cmd.startswith(prefix):
                exit_code = code
                break
        return ExecutedCheck.from_result(cmd, exit_code, "", plan_check_id=plan_check_id)

    async def dom_snapshot(self, url):
        self.dom_calls.append(url)
        return self._dom.get(url, [])


# --- T024: plan ---

@pytest.mark.asyncio
async def test_a_plan_with_no_checks_fails_closed():
    """QA's historical failure is the confident pass on unverified work, so an
    empty plan is a step that could not run, not a run with nothing to do."""
    with pytest.raises(EmptyPlan):
        await run_plan_step(_Toolkit(plan=TestPlan(checks=[])), _Score())


@pytest.mark.asyncio
async def test_checks_not_bound_to_a_stated_criterion_are_dropped():
    plan = TestPlan(checks=[
        PlanCheck(id="c1", criterion=CRIT, kind="command", command="pytest"),
        PlanCheck(id="c2", criterion="something nobody asked for", kind="command", command="x"),
    ])
    result = await run_plan_step(_Toolkit(plan=plan), _Score())
    assert [c.id for c in result.checks] == ["c1"]


@pytest.mark.asyncio
async def test_a_plan_entirely_unbound_fails_closed():
    plan = TestPlan(checks=[PlanCheck(id="c9", criterion="unrelated", kind="command", command="x")])
    with pytest.raises(EmptyPlan):
        await run_plan_step(_Toolkit(plan=plan), _Score())


# --- T026: baseline ---

@pytest.mark.asyncio
async def test_baseline_is_skipped_when_nothing_visual_is_planned():
    """The second boot is the main cost this design adds. Verified by test
    rather than by inspection, per the plan.md performance budget."""
    plan = TestPlan(checks=[PlanCheck(id="c1", criterion=CRIT, kind="command", command="pytest")])
    tk = _Toolkit()

    result = await run_baseline_step(
        tk, plan, workspace=Path("/w"), merge_base="abc", worktree_dir=Path("/w/.base")
    )

    assert result == {}
    assert tk.ran == [], "no worktree should be created for a command-only plan"


@pytest.mark.asyncio
async def test_baseline_runs_for_a_flow_plan_and_uses_a_worktree():
    plan = TestPlan(
        checks=[PlanCheck(id="c1", criterion=CRIT, kind="flow",
                          steps=[FlowStep(action="goto", target="/")])],
        surfaces=["http://localhost:3000/"],
    )
    tk = _Toolkit(dom={"http://localhost:3000/": [{"kind": "button", "label": "Continue"}]})

    result = await run_baseline_step(
        tk, plan, workspace=Path("/w"), merge_base="abc", worktree_dir=Path("/w/.base")
    )

    assert any("git worktree add" in c for c in tk.ran)
    assert result["http://localhost:3000/"][0].label == "Continue"


@pytest.mark.asyncio
async def test_a_failed_worktree_raises_rather_than_reporting_no_regressions():
    """Without a baseline the delta is unknowable. Reporting 'nothing
    regressed' from a missing comparison is false reassurance."""
    plan = TestPlan(
        checks=[PlanCheck(id="c1", criterion=CRIT, kind="visual")],
        surfaces=["http://localhost:3000/"],
    )
    tk = _Toolkit(commands={"git worktree add": 128})

    with pytest.raises(RuntimeError):
        await run_baseline_step(
            tk, plan, workspace=Path("/w"), merge_base="abc", worktree_dir=Path("/w/.base")
        )


# --- T028: execute ---

@pytest.mark.asyncio
async def test_every_planned_check_yields_a_real_result():
    plan = TestPlan(checks=[
        PlanCheck(id="c1", criterion=CRIT, kind="command", command="pytest -q"),
        PlanCheck(id="c2", criterion=CRIT, kind="command", command="ruff check"),
    ])
    results = await run_execute_step(_Toolkit(), plan, cwd=Path("/w"), driver_path="/d.py")

    assert [r.plan_check_id for r in results] == ["c1", "c2"]
    assert all(r.exit_code == 0 for r in results)


@pytest.mark.asyncio
async def test_a_failing_command_is_recorded_as_failing():
    plan = TestPlan(checks=[PlanCheck(id="c1", criterion=CRIT, kind="command", command="pytest -q")])
    results = await run_execute_step(
        _Toolkit(commands={"pytest -q": 1}), plan, cwd=Path("/w"), driver_path="/d.py"
    )
    assert results[0].passed is False


@pytest.mark.asyncio
async def test_a_raising_check_still_produces_a_result():
    """A missing result leaves its criterion unbound, which reads as 'not
    demonstrated' and hides the real cause."""

    class _Boom(_Toolkit):
        async def run_command(self, *a, **k):
            raise OSError("no such interpreter")

    plan = TestPlan(checks=[PlanCheck(id="c1", criterion=CRIT, kind="command", command="x")])
    results = await run_execute_step(_Boom(), plan, cwd=Path("/w"), driver_path="/d.py")

    assert len(results) == 1
    assert results[0].passed is False
    assert "OSError" in results[0].output_excerpt


@pytest.mark.asyncio
async def test_flow_steps_are_passed_as_json_not_interpolated_into_code(tmp_path):
    """Nothing the model wrote is ever interpolated into executable source."""
    plan = TestPlan(checks=[
        PlanCheck(id="c1", criterion=CRIT, kind="flow",
                  steps=[FlowStep(action="fill", target="#email", value="'; rm -rf /")])
    ])
    tk = _Toolkit()
    await run_execute_step(
        tk, plan, cwd=tmp_path, driver_path=str(tmp_path / ".qa_flow_driver.py")
    )

    import json
    import shlex

    assert tk.ran, "the check must actually have run"
    argv = shlex.split(tk.ran[0])
    # Three tokens: interpreter, driver, and ONE argument carrying the payload.
    # If the hostile value escaped quoting, shlex would split it into more.
    assert len(argv) == 3, f"payload was not a single shell argument: {argv}"
    from performer.workflows.qa.execute import resolve_interpreter

    assert argv[0] == resolve_interpreter(), (
        "must use an explicitly resolved interpreter, never a bare python3: the "
        "env-cache prepends its own Playwright-less python3 onto PATH"
    )
    assert argv[0] != "python3"
    steps = json.loads(argv[2])
    assert steps[0]["value"] == "\'; rm -rf /", "the value survives intact as data"


@pytest.mark.asyncio
async def test_the_flow_driver_is_written_before_it_is_invoked():
    """Found by the first live eval run.

    run_execute_step invokes driver_path, but nothing wrote it: flow_driver_source()
    existed and was never called. Every flow check would have failed on a missing
    file, and the failure would have read as "the flow failed" rather than "the
    harness never installed its own driver".
    """
    import tempfile

    from performer.workflows.qa.execute import ensure_flow_driver, flow_driver_source

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / ".qa_flow_driver.py"
        assert not path.exists()

        ensure_flow_driver(path)

        assert path.exists(), "the driver must be on disk before any flow runs"
        assert path.read_text() == flow_driver_source()
        # Rewriting must be safe: steps may run more than once per workspace.
        ensure_flow_driver(path)
        assert path.read_text() == flow_driver_source()


@pytest.mark.asyncio
async def test_running_a_flow_installs_the_driver():
    plan = TestPlan(checks=[
        PlanCheck(id="c1", criterion=CRIT, kind="flow",
                  steps=[FlowStep(action="goto", target="/")])
    ])
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        driver = Path(tmp) / ".qa_flow_driver.py"
        await run_execute_step(_Toolkit(), plan, cwd=Path(tmp), driver_path=str(driver))
        assert driver.exists(), "run_execute_step must install the driver it invokes"


def test_the_container_interpreter_is_preferred_when_it_exists():
    """In the performer, /usr/local/bin/python3 MUST win.

    The env-cache prepends its own Playwright-less python3 onto PATH, which
    shadows the image's. qa_capture.py documents this; the flow driver inherits
    the same constraint.
    """
    from performer.workflows.qa import execute

    resolved = execute.resolve_interpreter(exists=lambda p: p == execute.SYSTEM_PYTHON)
    assert resolved == execute.SYSTEM_PYTHON


def test_falls_back_to_the_running_interpreter_not_to_bare_python3():
    """Found by the first live eval run: on the host the container path does not
    exist, so the driver failed with exit 127.

    The fallback is sys.executable, never a bare `python3` -- resolving through
    PATH is the exact shadowing bug the explicit path exists to avoid, and a
    fallback that reintroduces it in the container would be worse than the crash.
    """
    import sys

    from performer.workflows.qa import execute

    resolved = execute.resolve_interpreter(exists=lambda _p: False)
    assert resolved == sys.executable
    assert resolved != "python3"


def test_flow_step_fields_tell_the_model_what_goes_where():
    """Found by the first live eval run.

    The model planned {"action": "goto", "target": null, "value": "/signin"} --
    the URL in the wrong field -- because `target` and `value` were bare
    `str | None` with nothing distinguishing them. A schema that names fields
    without saying what they mean is only half a contract.
    """
    from performer.workflows.qa.models import FlowStep

    schema = FlowStep.model_json_schema()
    target_doc = schema["properties"]["target"].get("description", "")
    value_doc = schema["properties"]["value"].get("description", "")

    assert target_doc, "target must document what it holds"
    assert value_doc, "value must document what it holds"
    # The distinction that actually failed: which one carries a URL.
    assert "url" in target_doc.lower()
    assert "goto" in target_doc.lower()
    assert "url" not in value_doc.lower() or "not" in value_doc.lower()


def test_plan_check_fields_are_documented_too():
    from performer.workflows.qa.models import PlanCheck

    props = PlanCheck.model_json_schema()["properties"]
    for field in ("id", "criterion", "kind", "command", "steps"):
        assert props[field].get("description"), f"{field} must be documented"


@pytest.mark.asyncio
async def test_the_planner_is_told_where_the_app_will_be_served():
    """Found by the first live eval run.

    The model planned `curl localhost:3000` against an app actually serving on
    an ephemeral port, because nothing told it the URL -- boot happened after
    planning. The check failed, and since every bound check must pass, a
    genuinely demonstrated criterion was demoted to failed.

    The base URL is knowable from PORT without booting, so the planner gets it.
    """
    seen: list[str] = []

    class _TK(_Toolkit):
        async def call_model(self, *, persona, schema, content, budget):
            seen.append(" ".join(c.get("text", "") for c in content))
            return self._plan

    plan = TestPlan(checks=[PlanCheck(id="c1", criterion=CRIT, kind="command", command="x")])
    await run_plan_step(_TK(plan=plan), _Score(), base_url="http://127.0.0.1:62253/")

    assert "http://127.0.0.1:62253/" in seen[0], (
        "the planner must be told where the app is served, or it invents a port"
    )


@pytest.mark.asyncio
async def test_planning_without_a_known_base_url_says_so_explicitly():
    """Silence would leave the model to assume a conventional port."""
    seen: list[str] = []

    class _TK(_Toolkit):
        async def call_model(self, *, persona, schema, content, budget):
            seen.append(" ".join(c.get("text", "") for c in content))
            return self._plan

    plan = TestPlan(checks=[PlanCheck(id="c1", criterion=CRIT, kind="command", command="x")])
    await run_plan_step(_TK(plan=plan), _Score(), base_url=None)

    assert "not known" in seen[0].lower() or "no url" in seen[0].lower()


@pytest.mark.asyncio
async def test_the_baseline_snapshots_the_base_app_not_the_running_head_app():
    """Found by the first full six-scenario eval run.

    run_baseline_step checked out a worktree at the merge-base and then called
    dom_snapshot(surface) -- which hits whatever is already serving, i.e. the
    HEAD app. Before and after were therefore the same page, the delta was
    always empty, and regression detection could not work at all. The scenario
    with a silently deleted password field returned the right verdict for an
    unrelated reason, which is how it stayed hidden.

    The baseline must boot the base-commit app and snapshot THAT.
    """
    snapshotted: list[str] = []

    class _TK(_Toolkit):
        async def dom_snapshot(self, url):
            snapshotted.append(url)
            return [{"kind": "password_input", "label": "Password"}]

    plan = TestPlan(
        checks=[PlanCheck(id="c1", criterion=CRIT, kind="visual")],
        surfaces=["/signin"],
    )
    tk = _TK()

    booted: list[str] = []

    class _BaseBoot:
        env: ClassVar[dict[str, str]] = {"PORT": "9999"}

        async def ensure_serving(self, _toolkit):
            booted.append("base")
            return "http://127.0.0.1:9999/"

        def shutdown(self):
            booted.append("down")

    before = await run_baseline_step(
        tk, plan,
        workspace=Path("/w"), merge_base="abc", worktree_dir=Path("/w/.base"),
        base_url="http://127.0.0.1:8000/",
        boot_base=lambda _worktree: _BaseBoot(),
    )

    assert booted == ["base", "down"], "the base app must be booted and torn down"
    assert snapshotted == ["http://127.0.0.1:9999/signin"], (
        "surfaces must be read from the BASE app's port, not the head app's"
    )
    assert before["/signin"][0].kind == "password_input"


@pytest.mark.asyncio
async def test_a_base_app_boot_failure_says_why():
    """Round-two review: the baseline raised 'never came up' whether the
    merge-base failed to build, crashed, or was slow. Different fixes."""
    plan = TestPlan(
        checks=[PlanCheck(id="c1", criterion=CRIT, kind="visual")],
        surfaces=["http://localhost:3000/"],
    )

    class _DeadBase:
        env: ClassVar[dict[str, str]] = {"PORT": "9999"}
        failure_reason = "the app process exited with code 2 before opening port 9999"

        async def ensure_serving(self, _toolkit):
            return None

        def shutdown(self):
            pass

    with pytest.raises(RuntimeError) as exc:
        await run_baseline_step(
            _Toolkit(), plan,
            workspace=Path("/w"), merge_base="abc", worktree_dir=Path("/tmp/base"),
            base_url="http://127.0.0.1:8000/", boot_base=lambda _wt: _DeadBase(),
        )
    assert "exited with code 2" in str(exc.value)


@pytest.mark.asyncio
async def test_an_unobservable_visual_check_fails_closed():
    """411 round-eight review: the driver on an empty step list exits 0, so a
    visual check left with no steps by the capture prep — no navigation and
    no declared surface — would satisfy its criterion while observing
    nothing. The harness refuses to run it at all."""
    plan = TestPlan(checks=[PlanCheck(id="c1", criterion=CRIT, kind="visual")])
    tk = _Toolkit()
    results = await run_execute_step(
        tk, plan, cwd=Path("/w"), driver_path="/tmp/qa_flow_driver.py"
    )

    assert len(results) == 1
    assert results[0].passed is False
    assert results[0].exit_code != 0
    assert tk.ran == [], "an unobservable check must never reach the driver"


@pytest.mark.asyncio
async def test_the_baseline_observes_a_visual_checks_own_goto_target():
    """411 round-eight review: a visual check may navigate a target the
    planner never declared as a surface. With declared surfaces empty, the
    before map came back empty, the post-change observe was skipped
    entirely, and the screenshot/exit code supported a visual pass with no
    before/after comparison. The derived target must be observed."""
    snapshotted: list[str] = []

    class _TK(_Toolkit):
        async def dom_snapshot(self, url):
            snapshotted.append(url)
            return [{"kind": "heading", "label": "Dashboard"}]

    plan = TestPlan(
        checks=[PlanCheck(
            id="c1", criterion=CRIT, kind="visual",
            steps=[FlowStep(action="goto", target="/dashboard")],
        )],
        surfaces=[],
    )

    class _BaseBoot:
        env: ClassVar[dict[str, str]] = {"PORT": "9999"}

        async def ensure_serving(self, _toolkit):
            return "http://127.0.0.1:9999/"

        def shutdown(self):
            pass

    before = await run_baseline_step(
        _TK(), plan,
        workspace=Path("/w"), merge_base="abc", worktree_dir=Path("/tmp/base"),
        base_url="http://127.0.0.1:8000/", boot_base=lambda _wt: _BaseBoot(),
    )

    assert snapshotted == ["http://127.0.0.1:9999/dashboard"], (
        "the visual check's own goto target must be observed, not just surfaces"
    )
    assert before["/dashboard"][0].kind == "heading"
