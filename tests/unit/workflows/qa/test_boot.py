"""QA app boot + base-URL resolution.

Found by the first live eval run: the model planned `goto /signin`, a relative
target, and nothing resolved it against a base URL -- Playwright rejected it as
an invalid URL. Separately, nothing booted the app at all.
"""
from __future__ import annotations

from pathlib import Path

import pytest
from performer.workflows.qa.boot import (
    AppBoot,
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

def test_an_explicit_start_command_wins_over_inference(tmp_path):
    """infer_app_start_command is deliberately conservative and returns None for
    anything it does not recognise. Without an override, such a project can
    never be booted for QA at all."""
    cmd = start_command_for(tmp_path, {"QA_APP_START_COMMAND": "python app.py", "PORT": "8000"})
    assert cmd == "python app.py"


def test_inference_is_used_when_no_override_is_given(tmp_path):
    (tmp_path / "manage.py").write_text("# django")
    cmd = start_command_for(tmp_path, {"PORT": "8123"})
    assert cmd is not None and "manage.py" in cmd and "8123" in cmd


def test_an_unrecognised_project_with_no_override_yields_no_command(tmp_path):
    assert start_command_for(tmp_path, {"PORT": "8000"}) is None


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
async def test_a_missing_port_is_a_warning_that_names_the_fix(caplog):
    """Round-two review: a missing PORT logged at INFO with no guidance, and
    the report said only 'never came up'. An operator cannot act on that."""
    import logging

    import structlog

    structlog.configure(
        processors=[structlog.stdlib.render_to_log_kwargs],
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=False,
    )
    caplog.set_level(logging.WARNING)
    boot = AppBoot(env={}, workspace=Path("/w"))
    assert await boot.ensure_serving(object()) is None
    assert boot.failure_reason is not None
    assert "PORT" in boot.failure_reason
    assert "workflow_env" in boot.failure_reason


@pytest.mark.asyncio
async def test_an_unrecognised_project_names_the_override_that_would_fix_it():
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
    assert "QA_APP_START_COMMAND" in boot.failure_reason
    assert "infer" in boot.failure_reason.lower()


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
