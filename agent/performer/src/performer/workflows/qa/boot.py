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
import shlex
import subprocess
import time
from pathlib import Path
from typing import Callable
from urllib.parse import urljoin, urlsplit

import structlog

from performer.qa_capture import _port_serving, _terminate, app_base_url, infer_app_start_command
from performer.workflows.qa.models import TestPlan

log = structlog.get_logger(__name__)

#: Flow actions whose ``target`` is a URL rather than a selector.
_URL_ACTIONS = {"goto"}


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


def boot_timeout_from(env: dict[str, str]) -> float:
    """How long to wait for the app to answer, from QA_APP_BOOT_TIMEOUT.

    A fixed default is wrong in both directions: too short for a Rails app that
    migrates on startup, needlessly long for a stub that answers in 200ms. The
    operator knows which they have. A malformed value falls back rather than
    failing the run -- a typo in an env var must not take QA down with it.
    """
    raw = str(env.get("QA_APP_BOOT_TIMEOUT") or "").strip()
    if not raw:
        return DEFAULT_BOOT_TIMEOUT_S
    try:
        value = float(raw)
    except ValueError:
        log.warning("qa.boot.bad_timeout", value=raw, using=DEFAULT_BOOT_TIMEOUT_S)
        return DEFAULT_BOOT_TIMEOUT_S
    if value <= 0:
        log.warning("qa.boot.bad_timeout", value=raw, using=DEFAULT_BOOT_TIMEOUT_S)
        return DEFAULT_BOOT_TIMEOUT_S
    return value


def start_command_for(workspace: Path, env: dict[str, str]) -> str | None:
    """The command that starts the app, explicit override first."""
    override = str(env.get("QA_APP_START_COMMAND") or "").strip()
    if override:
        return override
    inferred = infer_app_start_command(Path(workspace), env)
    return shlex.join(inferred) if inferred else None


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
    ) -> None:
        self.env = dict(env)
        if boot_timeout is None:
            boot_timeout = boot_timeout_from(self.env)
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
        return subprocess.Popen(
            cmd, shell=True, cwd=str(cwd), env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    async def ensure_serving(self, toolkit) -> str | None:
        """Return the base URL once the app answers, or None.

        None is returned rather than a URL whenever the app is not actually
        serving -- a URL for a server that never answered would make every flow
        check fail for the wrong reason.
        """
        port = str(self.env.get("PORT") or "").strip()
        if not port:
            self.failure_reason = (
                "PORT is not set, so the app under test has no address to boot on. "
                "Set PORT in the role's workflow_env (config.yaml) or in the env "
                "cache's activation."
            )
            log.warning("qa.boot.no_port", hint="set PORT in workflow_env")
            return None

        base = app_base_url(self.env)
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

        cmd = start_command_for(self.workspace, self.env)
        if not cmd:
            self.failure_reason = (
                "no start command: infer_app_start_command recognises only Rails, "
                "Django and Node projects, and this is none of them. Set "
                "QA_APP_START_COMMAND in the role's workflow_env."
            )
            log.warning(
                "qa.boot.no_start_command",
                workspace=str(self.workspace),
                hint="set QA_APP_START_COMMAND in workflow_env",
            )
            return None

        self._proc = self._spawn(cmd, self.workspace, self.env)
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
        """
        if not base_url:
            return True  # nothing to check; the caller has no URL either way
        try:
            check = await toolkit.run_command(
                # Quoted: base_url is built from the PROTOCOL env var, which is
                # operator- or env-cache-supplied, not ours.
                f"curl -fsS -o /dev/null {shlex.quote(base_url)}",
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

        Only ``goto`` targets are URLs. A ``click`` or ``fill`` target is a
        selector, and rewriting it would corrupt the step.
        """
        if not base_url:
            return
        for check in plan.checks:
            for step in check.steps:
                if step.action in _URL_ACTIONS:
                    step.target = resolve_target(step.target, base_url)
