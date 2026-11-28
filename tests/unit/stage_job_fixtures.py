"""423: a stand-in for performer containers, for the stage provider tests.

:class:`LocalStageJobs` fills :class:`coordinare.services.stage_jobs.StageJobs` with local
processes: a "container" is ``python -m performer.stage_jobs <kind> <job dir>`` in a
session of its own, given only what Docker would give it (a base environment, the named
variables, the plain values and the token), with its output appended to the job's log as
``stage_jobs.job_command`` arranges in the real container. One instance stands for one
Docker host: a provider built again on the same instance is a restarted daemon that finds
the same containers.

The production path (``DockerStageJobs``) is tested separately with a fake ``docker``.
"""
from __future__ import annotations

import asyncio
import contextlib
import os
import signal
import subprocess
import sys
from typing import TYPE_CHECKING

from coordinare.services.stage_jobs import (
    CREDENTIAL_ENV,
    JOB_LOG,
    RunState,
    StageJobLaunch,
    job_name,
)

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

#: What a container has before any variable is passed to it.
_IMAGE_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "USER")


def container_env(env_names: Sequence[str] = (), env: dict[str, str] | None = None) -> dict[str, str]:
    result = {k: os.environ[k] for k in _IMAGE_ENV if k in os.environ}
    result.update({k: os.environ[k] for k in env_names if k in os.environ})
    result.update(env or {})
    return result


class LocalStageJobs:
    def __init__(self) -> None:
        self.containers: dict[str, subprocess.Popen[bytes]] = {}
        self.launches: list[StageJobLaunch] = []
        self.probes: list[list[str]] = []

    async def start(self, launch: StageJobLaunch) -> str:
        name = job_name(launch.job_dir)
        running = self.containers.get(name)
        if running is not None and running.poll() is None:
            return name
        self.launches.append(launch)
        env = container_env(launch.env_names, launch.env)
        if launch.token:
            env[CREDENTIAL_ENV] = launch.token
        with (launch.job_dir / JOB_LOG).open("ab") as log:
            self.containers[name] = subprocess.Popen(
                [sys.executable, "-m", "performer.stage_jobs", launch.kind, str(launch.job_dir)],
                stdin=subprocess.DEVNULL, stdout=log, stderr=log, env=env, cwd=launch.job_dir,
                start_new_session=True,
            )
        return name

    async def state(self, job_dir: Path) -> RunState:
        process = self.containers.get(job_name(job_dir))
        return RunState.RUNNING if process is not None and process.poll() is None else RunState.GONE

    async def stop(self, job_dir: Path) -> None:
        process = self.containers.get(job_name(job_dir))
        if process is not None and process.poll() is None:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(process.pid, signal.SIGKILL)
            process.wait()

    async def ended(self, job_dir: Path) -> None:
        del job_dir  # a local process holds no proxy or network

    async def probe(
        self, argv: Sequence[str], *, env_names: Sequence[str] = (), env: dict[str, str] | None = None,
        within: float = 60.0,
    ) -> tuple[int | None, str, str]:
        self.probes.append(list(argv))
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, env=container_env(env_names, env), stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, start_new_session=True,
            )
        except OSError as exc:
            return None, "", f"{argv[0]} could not be started: {exc.strerror or exc}"
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=within)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGKILL)
            await proc.wait()
            return None, "", ""
        if proc.returncode in (126, 127):  # as ``docker run`` reports a command it could not run
            return None, out.decode(errors="replace"), f"{argv[0]} could not be started: {err.decode(errors='replace')}"
        return proc.returncode, out.decode(errors="replace"), err.decode(errors="replace")

    def stop_all(self) -> None:
        for process in self.containers.values():
            if process.poll() is None:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(process.pid, signal.SIGKILL)
                process.wait()
