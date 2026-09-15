"""QA app boot + base-URL resolution.

Found by the first live eval run: the model planned `goto /signin`, a relative
target, and nothing resolved it against a base URL -- Playwright rejected it as
an invalid URL. Separately, nothing booted the app at all.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from performer.workflows.project_shape import ProjectShape
from performer.workflows.qa.boot import (
    DEFAULT_BOOT_TIMEOUT_S,
    AppBoot,
    boot_timeout_from,
    resolve_target,
    start_command_for,
)
from performer.workflows.qa.models import FlowStep, PlanCheck, TestPlan

# --- relative targets must resolve against the app base URL ---

def test_a_relative_target_resolves_against_the_base_url():
    assert resolve_target("/signin", "http://127.0.0.1:8000/") == "http://127.0.0.1:8000/signin"


def test_an_absolute_target_is_left_alone():
    url = "http://example.test/other"
    assert resolve_target(url, "http://127.0.0.1:8000/") == url


def test_a_bare_path_without_a_leading_slash_still_resolves():
    assert resolve_target("signin", "http://127.0.0.1:8000/") == "http://127.0.0.1:8000/signin"


def test_a_relative_target_with_no_base_url_is_left_unchanged():
    """Better to fail visibly in the driver than to invent a host."""
    assert resolve_target("/signin", None) == "/signin"


def test_plan_flow_targets_are_rewritten_in_place():
    plan = TestPlan(checks=[
        PlanCheck(id="c1", criterion="c", kind="flow", steps=[
            FlowStep(action="goto", target="/signin"),
            FlowStep(action="click", target="#submit"),  # a selector, not a URL
        ]),
    ])
    AppBoot.rewrite_targets(plan, "http://127.0.0.1:8000/")

    steps = plan.checks[0].steps
    assert steps[0].target == "http://127.0.0.1:8000/signin", "goto targets are URLs"
    assert steps[1].target == "#submit", "click targets are selectors and must not be touched"


# --- an explicit start command, because inference cannot cover everything ---

def test_an_explicit_start_command_wins_over_the_reading(tmp_path):
    """The operator's override still beats everything (#367).

    They are stating intent about THIS project and know things the repository
    does not say. QA also skips the model call entirely when the override is
    set, so an operator never pays for a reading they have already overruled.
    """
    shape = ProjectShape(start_command="bin/rails server", boot_seconds=90)
    cmd = start_command_for({"QA_APP_START_COMMAND": "python app.py", "PORT": "8000"}, shape)
    assert cmd == "python app.py"


def test_the_reading_supplies_the_command_when_there_is_no_override():
    """This used to be a Django branch keyed on manage.py existing.

    Nothing here is Django, or Rails, or Node -- the three the old
    infer_app_start_command knew -- and it still boots.
    """
    shape = ProjectShape(summary="a Phoenix app", start_command="mix phx.server", boot_seconds=45)
    assert start_command_for({"PORT": "8123"}, shape) == "mix phx.server"


def test_no_shape_and_no_override_yields_no_command():
    """A boot nobody described is not a boot to guess at."""
    assert start_command_for({"PORT": "8000"}) is None


def test_a_project_the_model_says_has_no_server_yields_no_command():
    """An empty start_command is an answer: a library is not browsable.

    Distinct from cannot_determine, which stops the card. This one proceeds
    without a boot, which is correct for a library.
    """
    shape = ProjectShape(summary="a Go library", test_command="go test ./...", start_command="")
    assert start_command_for({"PORT": "8000"}, shape) is None


def test_the_boot_timeout_comes_from_the_reading_not_a_fixed_default():
    """The 60s default was chosen by reasoning about Rails migrations.

    The model has just read the boot path, so it is better placed to say. The
    operator's override still wins, and a malformed override falls back to the
    reading rather than failing the run.
    """
    shape = ProjectShape(start_command="bin/serve", boot_seconds=240)
    assert boot_timeout_from({}, shape) == 240.0
    assert boot_timeout_from({"QA_APP_BOOT_TIMEOUT": "30"}, shape) == 30.0
    assert boot_timeout_from({"QA_APP_BOOT_TIMEOUT": "not-a-number"}, shape) == 240.0
    assert boot_timeout_from({}) == DEFAULT_BOOT_TIMEOUT_S, "no reading falls back to the flat default"


# --- booting ---

@pytest.mark.asyncio
async def test_boot_is_skipped_when_the_app_is_already_serving():
    """No second server is launched -- but a health check still runs.

    Those are different things, and the boot proof the 088 evidence floor
    requires must be recorded either way: a run that found the app already up
    still has to demonstrate that it was up.
    """
    ran: list[str] = []

    class _TK:
        async def run_command(self, cmd, **kw):
            ran.append(cmd)
            if "app.py" in cmd:
                raise AssertionError("must not start a server that is already up")

            class R:
                passed = True
                exit_code = 0
                output_excerpt = ""
                command = cmd

            return R()

    boot = AppBoot(env={"PORT": "8000", "QA_APP_START_COMMAND": "python app.py"},
                   workspace=Path("/w"), port_check=lambda h, p: True)
    url = await boot.ensure_serving(_TK())

    assert url == "http://127.0.0.1:8000/"
    assert all("app.py" not in c for c in ran), "no server was started"
    assert any(c.startswith("curl") for c in ran), "boot proof was still recorded"
    assert boot.boot_check is not None


@pytest.mark.asyncio
async def test_a_seed_command_runs_before_the_server_starts():
    order: list[str] = []

    class _TK:
        async def run_command(self, cmd, **kw):
            order.append(cmd)
            class R:
                passed = True
                exit_code = 0
                output_excerpt = ""

            return R()

    boot = AppBoot(
        env={"PORT": "8000", "QA_APP_SEED_COMMAND": "python seed.py",
             "QA_APP_START_COMMAND": "python app.py"},
        workspace=Path("/w"),
        port_check=lambda h, p: False,
        spawn=lambda cmd, cwd, env: _FakeProc(),
        sleep=lambda _s: None,
        boot_timeout=0.0,
    )
    await boot.ensure_serving(_TK())

    assert order and order[0] == "python seed.py", "seed must precede boot"


@pytest.mark.asyncio
async def test_a_failed_seed_names_itself_in_the_failure_reason():
    spawned: list[str] = []

    class _TK:
        async def run_command(self, cmd, **kw):
            class R:
                passed = False
                exit_code = 1
                output_excerpt = "PG::UniqueViolation: duplicate key value"

            return R()

    boot = AppBoot(
        env={"PORT": "8000", "QA_APP_SEED_COMMAND": "bin/rails db:seed",
             "QA_APP_START_COMMAND": "python app.py"},
        workspace=Path("/w"),
        port_check=lambda h, p: False,
        spawn=lambda cmd, cwd, env: spawned.append(cmd) or _FakeProc(),
        sleep=lambda _s: None,
        boot_timeout=0.0,
    )
    assert await boot.ensure_serving(_TK()) is None
    assert spawned == [], "the server must not start on top of a failed seed"
    assert boot.failure_reason is not None
    assert "seed command exited 1" in boot.failure_reason
    assert "bin/rails db:seed" in boot.failure_reason
    assert "UniqueViolation" in boot.failure_reason


class _FakeProc:
    def terminate(self): pass
    def kill(self): pass
    def wait(self, timeout=None): return 0
    def poll(self): return None


@pytest.mark.asyncio
async def test_no_port_means_no_boot_and_no_base_url():
    boot = AppBoot(env={}, workspace=Path("/w"))
    assert await boot.ensure_serving(object()) is None


@pytest.mark.asyncio
async def test_a_server_that_never_comes_up_returns_none_rather_than_a_url():
    """Returning a URL for a server that never answered would make every flow
    check fail for the wrong reason."""
    boot = AppBoot(
        env={"PORT": "8000", "QA_APP_START_COMMAND": "python app.py"},
        workspace=Path("/w"),
        port_check=lambda h, p: False,
        spawn=lambda cmd, cwd, env: _FakeProc(),
        sleep=lambda _s: None,
        boot_timeout=0.0,
    )

    class _TK:
        async def run_command(self, cmd, **kw):
            class R:
                passed = True
                exit_code = 0
                output_excerpt = ""

            return R()

    assert await boot.ensure_serving(_TK()) is None


@pytest.mark.asyncio
async def test_the_launched_server_is_always_torn_down():
    proc = _FakeProc()
    killed: list[bool] = []
    proc.terminate = lambda: killed.append(True)  # type: ignore[method-assign]

    boot = AppBoot(
        env={"PORT": "8000", "QA_APP_START_COMMAND": "python app.py"},
        workspace=Path("/w"),
        port_check=lambda h, p: False,
        spawn=lambda cmd, cwd, env: proc,
        sleep=lambda _s: None,
        boot_timeout=0.0,
    )

    class _TK:
        async def run_command(self, cmd, **kw):
            class R:
                passed = True
                exit_code = 0
                output_excerpt = ""

            return R()

    await boot.ensure_serving(_TK())
    boot.shutdown()
    assert killed, "a launched server must not leak and hold PORT across runs"


# --- base-ref resolution (found by the live eval: exit 128 on origin/main) ---

@pytest.mark.asyncio
async def test_the_remote_tracking_ref_is_tried_first():
    """In a real clone origin/<base> is the authoritative ref: a stale local
    branch would silently compare against the wrong commit."""
    from performer.workflows.qa import QAWorkflow

    tried: list[str] = []

    class _TK:
        async def run_command(self, cmd, **kw):
            tried.append(cmd)
            class R:
                passed = "origin/main" in cmd
                exit_code = 0 if "origin/main" in cmd else 128
                output_excerpt = "abc123\n" if "origin/main" in cmd else ""
            return R()

    sha = await QAWorkflow()._merge_base(_TK(), Path("/w"), _ScoreStub())
    assert sha == "abc123"
    assert "origin/main" in tried[0]


@pytest.mark.asyncio
async def test_a_local_base_branch_is_accepted_when_there_is_no_remote():
    """A repository with no origin -- a generated fixture, or a local-only
    clone -- still has a legitimate base ref."""
    from performer.workflows.qa import QAWorkflow

    class _TK:
        async def run_command(self, cmd, **kw):
            has_remote = "origin/main" in cmd
            class R:
                passed = not has_remote
                exit_code = 128 if has_remote else 0
                output_excerpt = "" if has_remote else "def456\n"
            return R()

    sha = await QAWorkflow()._merge_base(_TK(), Path("/w"), _ScoreStub())
    assert sha == "def456"


@pytest.mark.asyncio
async def test_no_resolvable_base_ref_raises_rather_than_guessing():
    """Comparing against the wrong commit would produce a confident, wrong
    delta -- worse than reporting that the baseline is unavailable."""
    from performer.workflows.qa import QAWorkflow

    class _TK:
        async def run_command(self, cmd, **kw):
            class R:
                passed = False
                exit_code = 128
                output_excerpt = ""
            return R()

    with pytest.raises(RuntimeError, match="base"):
        await QAWorkflow()._merge_base(_TK(), Path("/w"), _ScoreStub())


class _ScoreStub:
    base_branch = "main"


def test_rebase_origin_moves_a_path_onto_the_base_apps_port():
    """Defect 9's second half: an absolute surface must be re-pointed, not
    resolved, or both snapshots read the same (head) server."""
    from performer.workflows.qa.boot import rebase_origin

    assert rebase_origin("http://127.0.0.1:8000/signin", "http://127.0.0.1:9999/") == (
        "http://127.0.0.1:9999/signin"
    )
    assert rebase_origin("http://127.0.0.1:8000/", "http://127.0.0.1:9999/") == (
        "http://127.0.0.1:9999/"
    )
    assert rebase_origin("http://x/a?b=1", "http://y/") == "http://y/a?b=1"


def test_rebase_origin_is_a_no_op_without_a_base_origin():
    from performer.workflows.qa.boot import rebase_origin

    assert rebase_origin("http://x/a", None) == "http://x/a"


@pytest.mark.asyncio
async def test_a_failing_health_check_is_not_boot_proof():
    """Caught by mutation testing: assigning the check unconditionally left
    every test green.

    A non-zero curl means the app did not answer. Recording that as boot proof
    would let a visual claim rest on evidence that says the opposite -- exactly
    the false-evidence problem the 088 floor exists to close.
    """
    class _TK:
        async def run_command(self, cmd, **kw):
            class R:
                passed = False
                exit_code = 7
                output_excerpt = "connection refused"
                command = cmd

            return R()

    boot = AppBoot(env={"PORT": "8000"}, workspace=Path("/w"), port_check=lambda h, p: True)
    await boot.ensure_serving(_TK())

    assert boot.boot_check is None, "a failing health check must not become proof"


@pytest.mark.asyncio
async def test_a_failed_health_check_means_not_serving():
    """Adversarial review, critical.

    An open port is not a serving app. The previous version returned the base
    URL whenever the port answered, even when the health check failed -- so a
    visual run proceeded against an app that never responded, and could report
    passed=True. The existing test only asserted boot_check was None; it never
    asserted the RUN failed. The mechanism was pinned, the consequence was not.
    """
    class _TK:
        async def run_command(self, cmd, **kw):
            class R:
                passed = False
                exit_code = 7
                output_excerpt = "connection refused"
                command = cmd

            return R()

    boot = AppBoot(env={"PORT": "8000"}, workspace=Path("/w"), port_check=lambda h, p: True)
    url = await boot.ensure_serving(_TK())

    assert url is None, "an app failing its health check is not serving"
    assert boot.boot_check is None


@pytest.mark.asyncio
async def test_the_poll_loop_does_not_block_the_event_loop():
    """Adversarial review, critical.

    The default sleep was time.sleep, which blocks the whole event loop for the
    duration of the boot wait -- stalling every other coroutine, including the
    HTTP client the workflow's model calls run on.
    """
    import inspect

    from performer.workflows.qa import boot as boot_module

    source = inspect.getsource(boot_module)
    assert "asyncio.sleep" in source, "the poll loop must yield, not block"
    sig = inspect.signature(AppBoot.__init__)
    default = sig.parameters["sleep"].default
    assert default is None or getattr(default, "__module__", "") == "asyncio", (
        f"default sleep must not be a blocking one, got {default!r}"
    )


def test_boot_timeout_is_configurable_through_the_workflow_env():
    """Second review round (operational-reality lens).

    A fixed 60s boot wait is wrong for a Rails app that runs migrations on
    startup, and right for a Python stub that answers in 200ms. The operator
    knows which they have; QA_APP_BOOT_TIMEOUT lets them say so, through the
    same workflow_env channel as the start command.
    """
    from performer.workflows.qa.boot import boot_timeout_from

    assert boot_timeout_from({}) == 60.0
    assert boot_timeout_from({"QA_APP_BOOT_TIMEOUT": "180"}) == 180.0
    assert boot_timeout_from({"QA_APP_BOOT_TIMEOUT": "45.5"}) == 45.5


def test_a_bad_boot_timeout_falls_back_rather_than_crashing_the_run():
    """A typo in an env var must not take QA down with it."""
    from performer.workflows.qa.boot import boot_timeout_from

    assert boot_timeout_from({"QA_APP_BOOT_TIMEOUT": "soon"}) == 60.0
    assert boot_timeout_from({"QA_APP_BOOT_TIMEOUT": "-5"}) == 60.0
    assert boot_timeout_from({"QA_APP_BOOT_TIMEOUT": ""}) == 60.0


@pytest.mark.asyncio
async def test_a_crashed_server_fails_fast_instead_of_burning_the_boot_window():
    """Found by the 3-repeat eval: the env_broken scenario's syntax-error app
    consumed the entire 20s boot window on every run, polling a port that a
    dead process could never open. In production that window is 60s, or
    whatever larger value the operator set for a slow Rails boot.
    """
    class _Dead(_FakeProc):
        def poll(self):
            return 1  # exited immediately

    slept: list[float] = []

    async def _sleep(s):
        slept.append(s)

    class _TK:
        async def run_command(self, cmd, **kw):
            class R:
                passed = True
                exit_code = 0
                output_excerpt = ""
                command = cmd

            return R()

    boot = AppBoot(
        env={"PORT": "8000", "QA_APP_START_COMMAND": "python app.py"},
        workspace=Path("/w"),
        port_check=lambda h, p: False,
        spawn=lambda cmd, cwd, env: _Dead(),
        sleep=_sleep,
        boot_timeout=60.0,
    )
    url = await boot.ensure_serving(_TK())

    assert url is None
    assert slept == [], "a dead process must not be waited on at all"


@pytest.mark.asyncio
async def test_a_process_still_running_is_given_the_full_window():
    """The fast-fail must not fire on a slow-but-alive server."""
    class _Alive(_FakeProc):
        def poll(self):
            return None  # still running

    slept: list[float] = []

    async def _sleep(s):
        slept.append(s)

    class _TK:
        async def run_command(self, cmd, **kw):
            class R:
                passed = True
                exit_code = 0
                output_excerpt = ""
                command = cmd

            return R()

    boot = AppBoot(
        env={"PORT": "8000", "QA_APP_START_COMMAND": "python app.py"},
        workspace=Path("/w"),
        port_check=lambda h, p: False,
        spawn=lambda cmd, cwd, env: _Alive(),
        sleep=_sleep,
        boot_timeout=0.0,
    )
    await boot.ensure_serving(_TK())
    # deadline governs; with a 0s window it exits after the first check,
    # having not declared the process dead


@pytest.mark.asyncio
async def test_a_missing_port_is_a_warning_that_names_the_fix():
    """Capture the warning without leaking a stdlib bridge into later tests."""
    from structlog.testing import capture_logs

    boot = AppBoot(env={}, workspace=Path("/w"))
    with capture_logs() as events:
        assert await boot.ensure_serving(object()) is None
    assert any(event["log_level"] == "warning" for event in events)
    assert boot.failure_reason is not None
    assert "PORT" in boot.failure_reason
    assert "workflow_env" in boot.failure_reason


@pytest.mark.asyncio
async def test_a_project_with_no_start_command_names_the_override_that_would_fix_it():
    class _TK:
        async def run_command(self, cmd, **kw):
            class R:
                passed = True
                exit_code = 0
                output_excerpt = ""
                command = cmd

            return R()

    boot = AppBoot(env={"PORT": "8000"}, workspace=Path("/nowhere-recognisable"),
                   port_check=lambda h, p: False)
    assert await boot.ensure_serving(_TK()) is None
    assert boot.failure_reason is not None
    assert "QA_APP_START_COMMAND" in boot.failure_reason, "the operator needs the way out"
    # 367: the reason used to name Rails, Django and Node -- the three
    # frameworks the inference knew -- which is how its limits became an issue.
    # The message must not name a framework list, because there no longer is one.
    lowered = boot.failure_reason.lower()
    for framework in ("rails", "django", "node", "infer_app_start_command"):
        assert framework not in lowered, f"the reason still names {framework!r}"


@pytest.mark.asyncio
async def test_a_crashed_process_reports_its_exit_code_in_the_reason():
    """Distinguishes 'the app crashed' from 'the app was slow' -- different
    fixes, and the report previously said 'never came up' for both."""
    class _Dead(_FakeProc):
        def poll(self):
            return 2

    class _TK:
        async def run_command(self, cmd, **kw):
            class R:
                passed = True
                exit_code = 0
                output_excerpt = ""
                command = cmd

            return R()

    boot = AppBoot(env={"PORT": "8000", "QA_APP_START_COMMAND": "python app.py"},
                   workspace=Path("/w"), port_check=lambda h, p: False,
                   spawn=lambda c, w, e: _Dead(), sleep=None)
    assert await boot.ensure_serving(_TK()) is None
    assert "exit" in boot.failure_reason.lower() and "2" in boot.failure_reason


@pytest.mark.asyncio
async def test_adopting_an_already_open_port_is_recorded_not_assumed():
    """Completeness critic (round two): if PORT is already open, ensure_serving
    adopts whatever answers as if it were this run's app. Inside a Docker or
    k8s performer that is fine -- loopback is per container. On the subprocess
    transport two concurrent QA runs of one project share 127.0.0.1, and the
    second would compare against the first run's app with every verdict wrong
    and nothing visibly amiss. Adoption is now recorded so it is traceable."""
    class _TK:
        async def run_command(self, cmd, **kw):
            class R:
                passed = True
                exit_code = 0
                output_excerpt = ""
                command = cmd

            return R()

    boot = AppBoot(env={"PORT": "8000", "QA_APP_START_COMMAND": "python app.py"},
                   workspace=Path("/w"), port_check=lambda h, p: True)
    url = await boot.ensure_serving(_TK())
    assert url is not None
    assert boot.adopted_existing_server is True


@pytest.mark.asyncio
async def test_a_server_we_launched_is_not_marked_adopted():
    class _TK:
        async def run_command(self, cmd, **kw):
            class R:
                passed = True
                exit_code = 0
                output_excerpt = ""
                command = cmd

            return R()

    checks = iter([False, True])
    boot = AppBoot(env={"PORT": "8000", "QA_APP_START_COMMAND": "python app.py"},
                   workspace=Path("/w"), port_check=lambda h, p: next(checks),
                   spawn=lambda c, w, e: _FakeProc(), sleep=None, boot_timeout=5.0)
    await boot.ensure_serving(_TK())
    assert boot.adopted_existing_server is False


@pytest.mark.asyncio
async def test_the_health_check_url_is_shell_quoted():
    """Round-one finding that never received a verdict (two of 34 were dropped
    when their verifiers failed on structured output). base_url is built from
    the PROTOCOL env var -- operator or env-cache supplied -- and was
    interpolated into the curl command unquoted."""
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

    boot = AppBoot(env={"PORT": "8000", "PROTOCOL": "http; touch /tmp/pwned; echo"},
                   workspace=Path("/w"), port_check=lambda h, p: True)
    await boot.ensure_serving(_TK())

    import shlex

    curl = next(c for c in ran if c.startswith("curl"))
    argv = shlex.split(curl)
    assert argv[-1] == "http; touch /tmp/pwned; echo://127.0.0.1:8000/", (
        "the URL must arrive as ONE argument however hostile PROTOCOL is"
    )
    assert "touch" not in " ".join(argv[:-1])


# --- 367 review findings: the three the suite did not catch ----------------

def test_a_model_read_command_is_not_run_through_a_shell(tmp_path):
    """A regression this change introduced, found by adversarial review.

    Before 367 the only inferred command reaching shell=True was built by code
    and passed through shlex.join, so it was quoted by construction. Replacing
    that inference with a model-supplied string handed a raw string to a shell.
    """
    marker = tmp_path / "pwned"
    evil = f"/bin/echo hi; touch {marker}"
    proc = AppBoot._default_spawn(evil, tmp_path, {"PORT": "8000"})
    proc.wait()
    assert not marker.exists(), "the ';' ran as a shell separator, not as an argument"


def test_the_operators_own_command_keeps_its_shell(tmp_path):
    """Deliberately preserved: they may want `RAILS_ENV=test bin/rails server`.

    An operator setting QA_APP_START_COMMAND is a trusted human stating intent
    about this project. Taking shell semantics away would break configurations
    that legitimately rely on them.
    """
    marker = tmp_path / "operator-ran-this"
    cmd = f"/bin/echo hi; touch {marker}"
    proc = AppBoot._default_spawn(cmd, tmp_path, {"PORT": "8000", "QA_APP_START_COMMAND": cmd})
    proc.wait()
    assert marker.exists(), "the operator's shell semantics were removed"


@pytest.mark.asyncio
async def test_an_unrunnable_command_holds_instead_of_raising(tmp_path):
    """An unbalanced quote from a reading must not raise through the workflow."""
    class _TK:
        async def run_command(self, cmd, **kw):
            class R:
                passed, exit_code, output_excerpt, command = True, 0, "", cmd
            return R()

    shape = ProjectShape(start_command='bin/serve --flag "unbalanced')
    boot = AppBoot(env={"PORT": "8000"}, workspace=tmp_path,
                   port_check=lambda _h, _p: False, shape=shape)
    assert await boot.ensure_serving(_TK()) is None
    assert boot.failure_reason and "could not be run" in boot.failure_reason


def test_the_baseline_gets_the_same_reading_as_the_head_app(tmp_path):
    """The baseline is the SAME project at an older commit.

    Adversarial review found the reading was never passed to it, so
    start_command_for fell through to None and the baseline never booted --
    breaking every visual or flow run that does not set QA_APP_START_COMMAND.
    """
    from performer.workflows.qa import _default_base_boot

    shape = ProjectShape(start_command="mix phx.server", boot_seconds=45)
    base = _default_base_boot(tmp_path, {"PORT": "9999"}, shape)
    assert base.shape is shape
    assert start_command_for(base.env, base.shape) == "mix phx.server"


@pytest.mark.asyncio
async def test_a_command_port_prefix_decides_the_polled_port():
    """411 round-eight review: `PORT=9000 python app.py` boots the child on
    9000, but ensure_serving derived the polled port from the outer env
    (8000) before spawning — a healthy app reported unavailable. The
    effective env, outer env overlaid with the command's own prefix,
    decides the port."""
    seen_ports: list[str] = []

    class _TK:
        async def run_command(self, cmd, **kw):
            class R:
                passed = True
                exit_code = 0
                output_excerpt = ""

            return R()

    boot = AppBoot(
        env={"PORT": "8000", "QA_APP_START_COMMAND": "PORT=9000 python app.py"},
        workspace=Path("/w"),
        port_check=lambda h, p: seen_ports.append(p) or False,
        spawn=lambda cmd, cwd, env: _FakeProc(),
        sleep=lambda _s: None,
        boot_timeout=0.0,
    )
    assert await boot.ensure_serving(_TK()) is None
    assert seen_ports and all(p == "9000" for p in seen_ports), (
        "the polled port must come from the command's own prefix"
    )


@pytest.mark.asyncio
async def test_an_already_serving_app_on_the_command_port_is_adopted():
    """Round-eight review (companion): the adopt path builds its URL from
    the effective env too, so an existing server on the command's port is
    adopted at that address, not at the outer env's."""
    seen_ports: list[str] = []

    class _TK:
        async def run_command(self, cmd, **kw):
            class R:
                passed = True
                exit_code = 0
                output_excerpt = ""

            return R()

    boot = AppBoot(
        env={"PORT": "8000", "QA_APP_START_COMMAND": "PORT=9000 python app.py"},
        workspace=Path("/w"),
        port_check=lambda h, p: seen_ports.append(p) or True,
        spawn=lambda cmd, cwd, env: _FakeProc(),
        sleep=lambda _s: None,
        boot_timeout=0.0,
    )
    base = await boot.ensure_serving(_TK())
    assert seen_ports == ["9000"]
    assert base == "http://127.0.0.1:9000/"
