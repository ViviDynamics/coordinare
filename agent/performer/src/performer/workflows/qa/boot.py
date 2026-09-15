"""Get the app serving, and resolve flow targets against it (spec 164).

Found by the first live eval run: the model planned ``goto /signin`` -- a
relative target -- and nothing resolved it against a base URL, so Playwright
rejected it as an invalid URL. Separately, nothing booted the app at all.

Reuses the existing helpers rather than duplicating them: ``app_base_url`` and
``infer_app_start_command`` already live in ``qa_capture``, which is also where
the SIGTERM-then-SIGKILL teardown reasoning is documented (a stubborn server
that ignores SIGTERM leaks and holds PORT across QA runs).

Adds one thing those helpers lack: an EXPLICIT start command.
``infer_app_start_command`` is deliberately conservative and returns None for
anything it does not recognise, and nothing anywhere else configures the app's
own start command -- ``ServiceEntry`` covers supportive services like postgres
and redis, not the app under test. So a project outside the recognised set could
never be booted for QA at all. ``QA_APP_START_COMMAND`` closes that, with
``QA_APP_SEED_COMMAND`` for the migrate/seed step that must precede it.
"""
from __future__ import annotations

import asyncio
import re
import shlex
import subprocess
import time
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin, urlsplit

import structlog

from performer.qa_capture import _port_serving, _split_env_prefix, _terminate, app_base_url
from performer.workflows.project_shape import ProjectShape  # noqa: F401  (annotation)
from performer.workflows.qa.models import TestPlan

log = structlog.get_logger(__name__)

#: Flow actions whose ``target`` is a URL rather than a selector.
_URL_ACTIONS = {"goto", "http_assert"}


def resolve_target(target: str | None, base_url: str | None) -> str | None:
    """Resolve a relative flow target against *base_url*.

    With no base URL the target is returned unchanged: failing visibly in the
    driver is better than inventing a host and reporting a confident result
    about a page nobody visited.
    """
    if not target or not base_url:
        return target
    if "://" in target:
        return target
    return urljoin(base_url, target.lstrip("/"))


def rebase_origin(url: str, origin: str | None) -> str:
    """Return *url*'s path on *origin*.

    The baseline reads the SAME PAGE from an older process on a different port,
    so a planned surface must be re-pointed rather than resolved. An absolute
    surface (`http://127.0.0.1:8000/signin`) would otherwise keep pointing at
    the head app, both snapshots would be identical, and no regression could
    ever be detected -- which is exactly how defect 9 hid.
    """
    if not origin:
        return url
    parts = urlsplit(url)
    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"
    return urljoin(origin, path.lstrip("/"))


DEFAULT_BOOT_TIMEOUT_S = 60.0


def boot_timeout_from(env: dict[str, str], shape: "ProjectShape | None" = None) -> float:
    """How long to wait for the app to answer, from QA_APP_BOOT_TIMEOUT.

    A fixed default is wrong in both directions: too short for an app that
    migrates on startup, needlessly long for a stub that answers in 200ms. The
    operator knows which they have, and the override still wins.

    367: the fallback used to be a flat 60 seconds, chosen in a comment that
    reasoned about Rails migrations -- the same embedded knowledge as the
    framework list, in a threshold instead. The model has just read the boot
    path and says how long starting it plausibly takes. A malformed value falls
    back rather than failing the run: a typo in an env var must not take QA down
    with it.
    """
    raw = str(env.get("QA_APP_BOOT_TIMEOUT") or "").strip()
    fallback = float(shape.boot_seconds) if shape is not None else DEFAULT_BOOT_TIMEOUT_S
    if not raw:
        return fallback
    try:
        value = float(raw)
    except ValueError:
        log.warning("qa.boot.bad_timeout", value=raw, using=fallback)
        return fallback
    if value <= 0:
        log.warning("qa.boot.bad_timeout", value=raw, using=fallback)
        return fallback
    return value


def plan_needs_server(plan: TestPlan, base_url: str | None) -> bool:
    """Whether anything in the plan will contact the app under test.

    The boot gate was `needs_baseline()` — a visual plan booted the app, but a
    command-only plan that curls the app's health endpoint ran against
    nothing, failed, and the verdict blamed the code (411). Boot when any
    check references the app structurally: a host:port token, a
    scheme-qualified localhost/127.0.0.1 URL, or a placeholder the model was
    told to use instead of a port it does not know. A bare path component
    that merely CONTAINS the host (`pytest tests/localhost/fixtures.py`) or
    a bare address (`echo 127.0.0.1`) is not a server reference and must not
    boot the app.
    """
    if plan.needs_baseline():
        return True

    placeholders = re.compile(r"\$\{?BASE_URL\}?|\$\{?PORT\}?")
    url_token = re.compile(
        r"(?:localhost|127\.0\.0\.1)(?::\d|:\$)|https?://(?:localhost|127\.0\.0\.1)"
    )
    if base_url:
        origin = base_url.rstrip("/")
        port = origin.rsplit(":", 1)[-1]
    else:
        port = ""
    for check in plan.checks:
        if check.kind == "flow":
            # An http_assert contacts the app by construction: its target is
            # an endpoint on the app under test, relative or not (411
            # round-seven review — an API-only flow booted nothing and its
            # rewritten absolute URL never materialised).
            if any(s.action == "http_assert" for s in check.steps):
                return True
            continue
        if check.kind != "command" or not check.command:
            continue
        if placeholders.search(check.command):
            return True
        if url_token.search(check.command):
            return True
        if base_url and port.isdigit() and origin and origin in check.command:
            return True
    return False


def start_command_for(env: dict[str, str], shape: "ProjectShape | None" = None) -> str | None:
    """The command that starts the app, explicit override first (#367).

    The override still wins. An operator naming the command for THIS project is
    stating intent, not encoding stack knowledge, and they know things the
    repository does not say.

    Behind it, ``infer_app_start_command`` used to branch Rails, then Django,
    then Node, and return None for everything else -- and it said so in the
    failure message, which is how this became an issue. The model has read the
    repository and says how it starts.
    """
    override = str(env.get("QA_APP_START_COMMAND") or "").strip()
    if override:
        return override
    if shape is not None and shape.start_command.strip():
        return shape.start_command.strip()
    return None


class AppBoot:
    """Ensures the app is serving for the duration of a QA run."""

    def __init__(
        self,
        *,
        env: dict[str, str],
        workspace: Path,
        port_check: Callable[[str, str], bool] = _port_serving,
        spawn: Callable[..., object] = None,  # type: ignore[assignment]
        sleep=None,
        boot_timeout: float | None = None,
        poll_interval: float = 1.0,
        shape: "ProjectShape | None" = None,
    ) -> None:
        self.env = dict(env)
        self.shape = shape
        if boot_timeout is None:
            boot_timeout = boot_timeout_from(self.env, shape)
        self.workspace = Path(workspace)
        self._port_check = port_check
        self._spawn = spawn or self._default_spawn
        self._sleep = sleep or asyncio.sleep
        self._boot_timeout = boot_timeout
        self._poll_interval = poll_interval
        self._proc: object | None = None
        self.base_url: str | None = None
        #: True when the port was already open and we adopted whatever answered
        #: instead of launching our own server. Loopback is per container under
        #: Docker/k8s, but on the subprocess transport two concurrent runs of one
        #: project share 127.0.0.1 and the second would silently compare against
        #: the first's app. Recorded so a verdict from an adopted server is
        #: traceable (completeness critic, round two).
        self.adopted_existing_server: bool = False
        #: Why ensure_serving returned None, in words an operator can act on.
        #: "never came up" covered a missing PORT, an unrecognised project, a
        #: crash and a slow boot alike -- four different fixes, one message.
        self.failure_reason: str | None = None
        #: A real health check against the serving app, used as boot proof.
        self.boot_check = None

    @staticmethod
    def _default_spawn(cmd: str, cwd: Path, env: dict[str, str]):
        """Start the app. The operator's command gets a shell; the model's does not.

        367 regression, found by adversarial review. Before this change the
        only command reaching ``shell=True`` from inference was built by code
        and passed through ``shlex.join`` -- quoted by construction. Replacing
        that inference with a model-supplied string handed a raw string to a
        shell, so ``bin/serve; anything`` would run the second half too.

        The distinction that fixes it is real rather than cosmetic. An operator
        setting QA_APP_START_COMMAND is a trusted human who may legitimately
        want shell semantics (``RAILS_ENV=test bin/rails server``), and taking
        that away would break their configuration. A start command the model
        read out of a repository is supposed to be a command, not a script, so
        it is split and exec'd with no shell between it and the process.

        The performer is a sandboxed container that already runs model-authored
        code, so this is not the last line of defence -- but silently dropping
        quoting that used to be there is a regression whatever the blast radius.
        """
        override = str(env.get("QA_APP_START_COMMAND") or "").strip()
        if cmd == override:
            return subprocess.Popen(
                cmd, shell=True, cwd=str(cwd), env=env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        # A shape command may carry VAR=val assignments the same way the
        # override can (`RAILS_ENV=test bin/rails server`); split naively,
        # `RAILS_ENV=test` runs as argv[0]. Keep the parsing identical to
        # resolve_start_command's so both boot paths agree (411 round-six).
        argv, extra = _split_env_prefix(cmd)
        if not argv:
            raise ValueError(f"start command is not runnable: {cmd!r}")
        if extra:
            env = {**env, **extra}
        return subprocess.Popen(
            argv, cwd=str(cwd), env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    async def ensure_serving(self, toolkit) -> str | None:
        """Return the base URL once the app answers, or None.

        None is returned rather than a URL whenever the app is not actually
        serving -- a URL for a server that never answered would make every flow
        check fail for the wrong reason.
        """
        # The command may carry its own env assignments (a shape reading like
        # `PORT=9000 python app.py`): the child receives the prefix, so the
        # port polled here must come from the effective env — the outer env
        # overlaid with the command's own prefix — or a healthy app on 9000
        # is reported unavailable because nothing answered on 8000 (411
        # round-eight review). Failure precedence is unchanged: no_port and
        # no_start_command still fire in that order.
        cmd = start_command_for(self.env, self.shape)
        extra_env: dict[str, str] = {}
        if cmd:
            try:
                _argv, extra_env = _split_env_prefix(cmd)
            except (ValueError, OSError) as exc:
                # The same failure the spawn path reports: an unbalanced quote
                # is a boot that did not happen, not a poll that timed out.
                self.failure_reason = (
                    f"the start command could not be run ({cmd!r}): {exc}"
                )
                log.warning("qa.boot.spawn_failed", command=cmd[:200], error=str(exc)[:200])
                return None
        effective_env = {**self.env, **extra_env}
        port = str(effective_env.get("PORT") or "").strip()
        if not port:
            self.failure_reason = (
                "PORT is not set, so the app under test has no address to boot on. "
                "Set PORT in the role's workflow_env (config.yaml) or in the env "
                "cache's activation."
            )
            log.warning("qa.boot.no_port", hint="set PORT in workflow_env")
            return None

        base = app_base_url(effective_env)
        if self._port_check("127.0.0.1", port):
            if not await self._record_health_check(toolkit, base):
                # An open port is not a serving app. Proceeding here would run
                # visual checks against something that never answered, and the
                # run could still report a pass.
                log.warning("qa.boot.health_check_failed_not_serving", url=base)
                return None
            self.adopted_existing_server = True
            log.warning(
                "qa.boot.adopted_existing_server",
                url=base,
                detail="port already open; this run did not start the app it will test",
            )
            self.base_url = base
            return base

        seed = str(self.env.get("QA_APP_SEED_COMMAND") or "").strip()
        if seed:
            # Before the server, not after: a migrate/seed against a running app
            # is a different and usually wrong operation.
            result = await toolkit.run_command(seed, cwd=self.workspace, timeout_s=300)
            if not result.passed:
                log.warning("qa.boot.seed_failed", exit_code=result.exit_code)
                excerpt = (getattr(result, "output_excerpt", "") or "").strip()
                self.failure_reason = (
                    f"seed command exited {result.exit_code}: {seed}"
                    + (f" -- {excerpt[-300:]}" if excerpt else "")
                )
                return None

        if not cmd:
            # 367: this used to name the three frameworks it knew, which is how
            # the limitation became an issue. The model reads the repository
            # now, so a missing command means it judged this not to be something
            # that can be started and browsed -- a library or a CLI -- or the
            # shape never reached this boot.
            self.failure_reason = (
                "no start command: the model did not identify a way to start "
                "this project so a browser could reach it. Set "
                "QA_APP_START_COMMAND in the role's workflow_env if it does "
                "have one."
            )
            log.warning(
                "qa.boot.no_start_command",
                workspace=str(self.workspace),
                hint="set QA_APP_START_COMMAND in workflow_env",
            )
            return None

        try:
            self._proc = self._spawn(cmd, self.workspace, self.env)
        except (ValueError, OSError) as exc:
            # An unbalanced quote in a model-read command, or a binary that is
            # not there. Either is a boot that did not happen, and the run says
            # so rather than raising through the workflow.
            self.failure_reason = f"the start command could not be run ({cmd!r}): {exc}"
            log.warning("qa.boot.spawn_failed", command=cmd[:200], error=str(exc)[:200])
            return None
        deadline = time.monotonic() + self._boot_timeout
        while True:
            # A process that has already exited will never open the port. Bail
            # now rather than polling to the deadline: the 3-repeat eval showed
            # a syntax-error app burning the full window on every run, and in
            # production that window is 60s or the operator's larger value.
            exit_code = self._exit_code()
            if exit_code is not None:
                self.failure_reason = (
                    f"the app process exited with code {exit_code} before opening "
                    f"port {port} (command: {cmd}). This is a crash, not a slow boot."
                )
                log.warning(
                    "qa.boot.process_exited", command=cmd, port=port, exit_code=exit_code
                )
                self._proc = None  # nothing left to terminate
                return None
            if self._port_check("127.0.0.1", port):
                if not await self._record_health_check(toolkit, base):
                    self.failure_reason = (
                        f"the app opened port {port} but failed its health check at "
                        f"{base}. It is running but not answering HTTP."
                    )
                    log.warning("qa.boot.started_but_unhealthy", url=base)
                    self.shutdown()
                    return None
                self.base_url = base
                log.info("qa.boot.serving", url=base)
                return base
            if time.monotonic() >= deadline:
                self.failure_reason = (
                    f"the app did not open port {port} within {self._boot_timeout:g}s "
                    f"(command: {cmd}). If it is merely slow, raise QA_APP_BOOT_TIMEOUT "
                    "in workflow_env."
                )
                log.warning("qa.boot.never_came_up", command=cmd, port=port)
                self.shutdown()
                return None
            await self._sleep(self._poll_interval)

    def _exit_code(self) -> int | None:
        """The launched process's exit code, or None while it is still running.

        Tolerates spawners whose handle has no poll() (test doubles) by treating
        them as still running -- the deadline then governs, as before.
        """
        poll = getattr(self._proc, "poll", None)
        if not callable(poll):
            return None
        try:
            return poll()
        except Exception:  # noqa: BLE001
            return None

    async def _record_health_check(self, toolkit, base_url: str | None) -> bool:
        """Run a real health check so boot proof is evidence, not assertion.

        Recorded as an ExecutedCheck with its true exit code, and included in
        the report's executed_checks, because the 088 floor requires the boot
        command to be one that actually ran.

        The path comes from QA_APP_HEALTH_PATH (default /) and the check
        accepts ANY HTTP response: curl without -f exits 0 when the app
        answers at all. A 404 on a wrong health path is not 'not serving' --
        `-fsS` used to demand a 2xx and rejected boots that were, in fact, up
        (411).
        """
        if not base_url:
            return True  # nothing to check; the caller has no URL either way
        try:
            health_path = str(self.env.get("QA_APP_HEALTH_PATH") or "/").strip() or "/"
            if not health_path.startswith("/"):
                health_path = f"/{health_path}"
            url = f"{base_url.rstrip('/')}{health_path}"
            check = await toolkit.run_command(
                # Quoted: base_url is built from the PROTOCOL env var, which is
                # operator- or env-cache-supplied, not ours.
                f"curl -sS -o /dev/null {shlex.quote(url)}",
                cwd=self.workspace,
                timeout_s=30,
                plan_check_id="app-boot",
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("qa.boot.health_check_errored", error=str(exc))
            return False
        # Only a PASSING check is proof, and a failing one means the app is not
        # usefully serving -- the caller must not proceed as though it were.
        healthy = bool(getattr(check, "passed", False))
        self.boot_check = check if healthy else None
        return healthy

    def shutdown(self) -> None:
        """Always tear down a server we launched."""
        if self._proc is not None:
            _terminate(self._proc)
            self._proc = None

    @staticmethod
    def rewrite_targets(plan: TestPlan, base_url: str | None) -> None:
        """Resolve every URL-valued flow target in *plan* against *base_url*.

        Only ``goto`` and ``http_assert`` targets are URLs. A ``click`` or
        ``fill`` target is a selector, and rewriting it would corrupt the step.
        """
        if not base_url:
            return
        for check in plan.checks:
            for step in check.steps:
                if step.action in _URL_ACTIONS:
                    step.target = resolve_target(step.target, base_url)
