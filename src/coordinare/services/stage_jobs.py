"""423: stage jobs, a stage provider's CLI run in an ephemeral performer container.

QAre (#404) and Progettare (#403) are CLIs that need a checkout. Specs 107 and 555 say
coordinare never clones, fetches, rebases or pushes on its own host, so their runs go to
a performer: coordinare starts one container from the performer image per request, with
the request's directory mounted at the same path, and the command
``python -m performer.stage_jobs <kind> <job dir>``. Inside it the performer's workspace
layer makes the checkout, in the container's own filesystem, and the CLI runs there. The
job reports through files in the mounted directory, which is the job's whole state.

The container is named from its job directory and labelled with its request id, so:

- starting a request twice finds the container already there (Docker refuses the name);
- a restarted daemon finds a run in flight by asking Docker about that name, and a
  finished one by its files. The container is not labelled ``coordinare.performer.id``,
  so the startup sweep of orphaned performers leaves it running.

The GitHub token reaches the container through a private ``--env-file`` (removed as soon
as ``docker run`` returns), never on a command line or in the job directory. Variables the
operator passes through are named with ``-e NAME``, so Docker copies their values from
coordinare's environment without the value appearing in any argument.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import shlex
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Protocol

import structlog

from coordinare.lib.container_env import docker_environment
from coordinare.transport.base import TransportError

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

logger = structlog.get_logger(__name__)

#: The label every stage job container carries, with its request id as the value.
STAGE_JOB_LABEL = "coordinare.stage_job"
PROVIDER_LABEL = "coordinare.stage_provider"
#: The environment variable the job's GitHub token arrives in (``performer.stage_jobs``).
CREDENTIAL_ENV = "COORDINARE_STAGE_GITHUB_TOKEN"
#: The performer module a stage job runs.
STAGE_JOB_MODULE = "performer.stage_jobs"
#: Variables every stage job takes from coordinare's environment by name, when it has them:
#: git, gh, GitHub's API and the model calls go out the way the daemon's own requests do.
PROXY_ENV = ("HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY", "https_proxy", "http_proxy", "no_proxy")
_NAME_PREFIX = "coordinare-stage-"
_DOCKER_TIMEOUT_SECONDS = 60.0
_TAIL = 500
#: ``docker run`` exits 125 when Docker itself failed, and these when the container
#: started but could not run the command.
_DOCKER_FAILED = 125
_NOT_STARTED = frozenset({126, 127})


class RunState(StrEnum):
    """Whether a stage job's container is still running its job."""

    RUNNING = "running"
    GONE = "gone"


@dataclass(frozen=True)
class StageJobLaunch:
    """One stage job to start."""

    kind: str
    ref: str
    provider: str
    job_dir: Path
    #: The GitHub token for the checkout. Reaches the container in a private env file.
    token: str = ""
    #: Names of variables whose values the container takes from coordinare's environment.
    env_names: Sequence[str] = ()
    #: Plain, non-secret values.
    env: dict[str, str] = field(default_factory=dict)


class StageJobs(Protocol):
    """Where stage jobs run. Docker in production; tests stand in a local process."""

    async def start(self, launch: StageJobLaunch) -> str:
        """Start the job; its handle. A job whose container already exists is not started again."""
        ...

    async def state(self, job_dir: Path) -> RunState:
        """Whether the job's container is still running. Raises TransportError when unknown."""
        ...

    async def stop(self, job_dir: Path) -> None:
        """Stop and remove the job's container, if there is one. Best effort."""
        ...

    async def probe(
        self, argv: Sequence[str], *, env_names: Sequence[str] = (), env: dict[str, str] | None = None,
        within: float = 60.0,
    ) -> tuple[int | None, str, str]:
        """Run ``argv`` in a throwaway container of the job image: (exit code, stdout, stderr).

        The code is None when the image cannot run the command (it is not there, or not
        executable); None with an empty stderr means it did not answer within ``within``
        seconds. Raises TransportError when no container could be run at all (Docker is
        unreachable, the image cannot be pulled): retried, never read as a wrong image.
        """
        ...


def job_name(job_dir: Path) -> str:
    """The container name of the job in ``job_dir``: the same on every daemon start."""
    return _NAME_PREFIX + hashlib.sha256(str(job_dir).encode()).hexdigest()[:24]


#: The file in the job directory that keeps the job's own output (its container is removed).
JOB_LOG = "runner.log"
#: Why a job that ended without a word most likely never ran.
NEVER_STARTED = (
    "its performer exited before the stage job started, so nothing was logged; check that the "
    "image runs `python -m performer.stage_jobs` and that its entry point can start (an egress "
    "allowlist needs iptables and NET_ADMIN), for example with `docker run --rm <image> true`"
)


def never_started(job_dir: Path) -> bool:
    """Whether the job left no output at all: the container ended before its command ran."""
    try:
        return (job_dir / JOB_LOG).stat().st_size == 0
    except FileNotFoundError:
        return True
    except OSError:
        return False


def job_command(kind: str, job_dir: Path) -> list[str]:
    """The container's command: the stage job, its output appended to the job's log, so a
    job that dies before it reports still says why after its container is gone."""
    where = shlex.quote(str(job_dir))
    return ["sh", "-c", f"exec python -m {STAGE_JOB_MODULE} {shlex.quote(kind)} {where} >>{where}/{JOB_LOG} 2>&1"]


class DockerStageJobs:
    """Stage jobs as detached ``docker run --rm`` containers of the performer image.

    Every Docker call that does not answer in time is a TransportError, so the caller
    retries it rather than mistaking it for a verdict.
    """

    def __init__(
        self,
        image: str,
        *,
        egress_allowlist: Sequence[str] | None = None,
        extra_hosts: Sequence[str] = (),
        docker: str = "docker",
    ) -> None:
        self.image = image
        self._egress = list(egress_allowlist or [])
        self._extra_hosts = list(extra_hosts)
        self._docker = docker

    def _isolation(self) -> list[str]:
        args: list[str] = []
        for entry in self._extra_hosts:
            args += ["--add-host", entry]
        if self._egress:
            # The performer image's entry point locks egress down to these hosts.
            args += ["--cap-add", "NET_ADMIN", "-e", f"PERFORMER_EGRESS_ALLOWLIST={','.join(self._egress)}"]
        return args

    async def start(self, launch: StageJobLaunch) -> str:
        name = job_name(launch.job_dir)
        mount = str(launch.job_dir)
        args = [
            "run", "-d", "--rm", "--name", name,
            "--label", f"{STAGE_JOB_LABEL}={launch.ref}",
            "--label", f"{PROVIDER_LABEL}={launch.provider}",
            *self._isolation(),
            "-v", f"{mount}:{mount}",
            "-w", mount,
        ]
        for name_only in launch.env_names:
            args += ["-e", name_only]
        for key, value in launch.env.items():
            args += ["-e", f"{key}={value}"]
        with docker_environment({CREDENTIAL_ENV: launch.token} if launch.token else {}) as env_file:
            if env_file is not None:
                args += ["--env-file", str(env_file)]
            args += [self.image, *job_command(launch.kind, launch.job_dir)]
            code, out, err = await self._call(args)
            if code != 0 and "already in use" in err:
                if await self.state(launch.job_dir) is RunState.RUNNING:
                    # Started before (a restart, a race): the job is that container.
                    return name
                # A container of an earlier attempt that has ended but is not gone yet.
                await self._call(["rm", "-f", name])
                code, out, err = await self._call(args)
        if code == 0:
            logger.info("stage_job.started", kind=launch.kind, job=launch.ref, container=name, image=self.image)
            return name
        msg = f"could not start the {launch.kind} stage job {launch.ref} (docker exit {code}): {_tail(err or out)}"
        raise TransportError(msg)

    async def state(self, job_dir: Path) -> RunState:
        code, out, err = await self._call(
            ["container", "inspect", "--format", "{{.State.Status}}", job_name(job_dir)],
        )
        if code == 0:
            return RunState.RUNNING if out.strip() in ("created", "running", "restarting", "paused") else RunState.GONE
        if "no such" in err.lower():
            return RunState.GONE
        msg = f"docker could not say whether stage job {job_name(job_dir)} is running: {_tail(err)}"
        raise TransportError(msg)

    async def stop(self, job_dir: Path) -> None:
        with contextlib.suppress(TransportError):
            await self._call(["rm", "-f", job_name(job_dir)])

    async def probe(
        self, argv: Sequence[str], *, env_names: Sequence[str] = (), env: dict[str, str] | None = None,
        within: float = 60.0,
    ) -> tuple[int | None, str, str]:
        name = f"{_NAME_PREFIX}probe-{uuid.uuid4().hex[:12]}"
        args = ["run", "--rm", "--name", name, *self._isolation()]
        for name_only in env_names:
            args += ["-e", name_only]
        for key, value in (env or {}).items():
            args += ["-e", f"{key}={value}"]
        args += [self.image, *argv]
        try:
            code, out, err = await self._run(args, within=within)
        except TimeoutError:
            await self._remove(name)
            return None, "", ""
        if code == _DOCKER_FAILED:
            msg = f"docker could not run {self.image}: {_tail(err or out)}"
            raise TransportError(msg)
        if code in _NOT_STARTED:
            return None, out, f"{argv[0]} could not be started in {self.image}: {_tail(err or out)}"
        return code, out, err

    async def _remove(self, name: str) -> None:
        with contextlib.suppress(TransportError, TimeoutError):
            await self._run(["rm", "-f", name])

    async def _call(self, args: list[str]) -> tuple[int, str, str]:
        """``_run``, with a Docker that does not answer in time as a retryable error."""
        try:
            return await self._run(args, within=_DOCKER_TIMEOUT_SECONDS)
        except TimeoutError as exc:
            msg = f"docker {args[0]} did not answer within {_DOCKER_TIMEOUT_SECONDS:.0f} seconds"
            raise TransportError(msg) from exc

    async def _run(self, args: list[str], *, within: float = _DOCKER_TIMEOUT_SECONDS) -> tuple[int, str, str]:
        try:
            proc = await asyncio.create_subprocess_exec(
                self._docker, *args, stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
        except OSError as exc:
            msg = f"{self._docker} could not be started: {exc.strerror or exc}"
            raise TransportError(msg) from exc
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=within)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            raise
        return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace").strip()


class UnavailableStageJobs:
    """Stage jobs where none can run, saying why on every use."""

    def __init__(self, reason: str) -> None:
        self.reason = reason

    async def start(self, launch: StageJobLaunch) -> str:
        del launch
        raise TransportError(self.reason)

    async def state(self, job_dir: Path) -> RunState:
        del job_dir
        return RunState.GONE  # nothing ever runs here

    async def stop(self, job_dir: Path) -> None:
        del job_dir

    async def probe(
        self, argv: Sequence[str], *, env_names: Sequence[str] = (), env: dict[str, str] | None = None,
        within: float = 60.0,
    ) -> tuple[int | None, str, str]:
        del argv, env_names, env, within
        raise TransportError(self.reason)


async def released(jobs: StageJobs, job_dir: Path) -> bool:
    """Stop the job's container, if any, and confirm it is gone.

    False when its absence cannot be confirmed (it still runs, or Docker cannot say): the
    caller must then keep the job's directory as it is, so a container that started after
    all goes on with its own request and is never handed a new one through the same mount.
    """
    with contextlib.suppress(Exception):
        await jobs.stop(job_dir)
    try:
        return await jobs.state(job_dir) is RunState.GONE
    except Exception:
        return False


def _tail(text: str) -> str:
    return (text or "").strip()[-_TAIL:]
