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

435: a QAre provider with ``sandbox: sysbox`` runs every container (the job and its probes)
with ``--runtime=sysbox-runc``, and nothing else changes. Its image (``Dockerfile.sandbox``)
starts its own dockerd inside the job's container, so the job gets a Docker engine without
privilege and without the host's Docker, and removing the one container by its name
removes that engine and everything it started.

447: a sandboxed provider whose runs take an egress allowlist cannot keep it inside the job's
container (root there, or anything with the inner Docker, can rewrite the container's own
firewall). Its job runs on a Docker network of its own created with ``--internal``, which
has no route out, and a proxy container (``performer.egress_proxy``) on that network and on
the default bridge is its only exit: it relays only to the allowlisted hosts. Containers the
inner Docker starts are NATed through the job's one interface, so the same holds for them.
The network and the proxy are made before the job container and removed after it.
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import shlex
import time
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
#: 435: the Docker runtime a sandboxed provider's containers run under (Sysbox).
SANDBOX_RUNTIME = "sysbox-runc"
#: 435: where Kubernetes support for the sandbox is tracked.
SANDBOX_KUBERNETES_ISSUE = "#443"
#: 447: a sandboxed job with an egress allowlist: its proxy module and port, and the suffix
#: naming the job's own internal network and its proxy container (both ``<job name>-egress``).
EGRESS_PROXY_MODULE = "performer.egress_proxy"
EGRESS_PROXY_PORT = 3128
EGRESS_SUFFIX = "-egress"
#: Labels on the egress network and proxy: the job container's name, and when they were made.
EGRESS_LABEL = "coordinare.stage_job.egress"
EGRESS_CREATED_LABEL = "coordinare.stage_job.egress.created"
#: The variable that tells the sandbox image's entry point where the proxy is.
EGRESS_PROXY_ENV = "PERFORMER_EGRESS_PROXY"
#: An egress network whose job is gone is swept once it is this old, so a start running at
#: the same time never loses its proxy between making it and starting its job.
EGRESS_SWEEP_GRACE_SECONDS = 600.0
#: The proxy exits after this long with no connection; the job's entry point connects every
#: 30 seconds while the job runs, so the proxy goes soon after its job.
EGRESS_PROXY_IDLE_EXIT_SECONDS = 180
#: The internal network's bridge gets no address on the host, so the job cannot reach host
#: services through a gateway address; it only links the job to its proxy.
EGRESS_NETWORK_OPTIONS = ("-o", "com.docker.network.bridge.inhibit_ipv4=true")
#: 447: the health check's extra step for a proxied provider: the image has the proxy.
EGRESS_PROXY_CHECK = f"python -c 'import {EGRESS_PROXY_MODULE}'"
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


def runtime_missing(runtime: str) -> str:
    """Why a provider whose containers need ``runtime`` cannot run on this Docker."""
    return (
        f"the QAre sandbox needs the Docker runtime {runtime}, and `docker info` does not list it. "
        "Install Sysbox on a Linux Docker host and register it with Docker (docs/stage-providers.md, "
        "\"Docker sandbox\"). macOS Docker Desktop does not have it unless Enhanced Container Isolation "
        "provides it; Kubernetes is not supported yet"
    )



async def docker_runtimes(docker: str = "docker", *, within: float = 60.0) -> set[str]:
    """The runtimes the Docker behind ``docker`` lists in ``docker info``.

    Raises TransportError when Docker cannot say (unreachable, slow): retried, never read as
    "the runtime is missing".
    """
    args = ["info", "--format", "{{json .Runtimes}}"]
    try:
        proc = await asyncio.create_subprocess_exec(
            docker, *args, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        msg = f"{docker} could not be started to list its runtimes: {exc.strerror or exc}"
        raise TransportError(msg) from exc
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=within)
    except TimeoutError as exc:
        proc.kill()
        await proc.wait()
        msg = f"docker info did not list its runtimes within {within:.0f} seconds"
        raise TransportError(msg) from exc
    try:
        listed = json.loads(out.decode(errors="replace")) if proc.returncode == 0 else None
    except ValueError:
        listed = None
    if not isinstance(listed, dict):
        msg = f"docker info could not list its runtimes: {_tail(err.decode(errors='replace') or out.decode(errors='replace'))}"
        raise TransportError(msg)
    return {str(name) for name in listed}


def job_name(job_dir: Path) -> str:
    """The container name of the job in ``job_dir``: the same on every daemon start."""
    return _NAME_PREFIX + hashlib.sha256(str(job_dir).encode()).hexdigest()[:24]


#: The file in the job directory that keeps the job's own output (its container is removed).
JOB_LOG = "runner.log"
#: Why a job that ended without a word most likely never ran.
NEVER_STARTED = (
    "its performer exited before the stage job started, so nothing was logged; check that the "
    "image runs `python -m performer.stage_jobs` and that its entry point can start (an egress "
    "allowlist needs iptables and NET_ADMIN; in the QAre sandbox, its egress proxy must answer), "
    "for example with `docker run --rm <image> true`"
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

    435: with ``runtime`` (the sandbox), every container gets ``--runtime=<runtime>`` and
    nothing else, once ``docker info`` has listed that runtime.

    447: with ``runtime`` and an egress allowlist, the job runs on its own internal network
    behind an allowlisting proxy (module docstring), and probes run with no network.
    """

    def __init__(
        self,
        image: str,
        *,
        egress_allowlist: Sequence[str] | None = None,
        extra_hosts: Sequence[str] = (),
        docker: str = "docker",
        runtime: str | None = None,
    ) -> None:
        self.image = image
        self.runtime = runtime
        self._egress = list(egress_allowlist or [])
        self._extra_hosts = list(extra_hosts)
        self._docker = docker
        #: The runtime was listed once: it stays registered while Docker runs.
        self._runtime_listed = False

    def _run_options(self) -> list[str]:
        """What follows ``docker run``: the sandbox runtime, when there is one."""
        return [f"--runtime={self.runtime}"] if self.runtime else []

    @property
    def proxied(self) -> bool:
        """447: a sandbox with an egress allowlist, enforced by a proxy outside the job container."""
        return bool(self.runtime and self._egress)

    async def _require_runtime(self) -> None:
        """435: refuse, with the reason, to start a sandboxed container Docker cannot run."""
        if not self.runtime or self._runtime_listed:
            return
        if self.runtime not in await docker_runtimes(self._docker, within=_DOCKER_TIMEOUT_SECONDS):
            raise TransportError(runtime_missing(self.runtime))
        self._runtime_listed = True

    def _check_runtime_refused(self, err: str) -> None:
        """A Docker that no longer knows the sandbox runtime: say so, and check it again next time."""
        if self.runtime and self.runtime in err and "unknown or invalid runtime name" in err.lower():
            self._runtime_listed = False
            raise TransportError(runtime_missing(self.runtime))

    def _hosts(self) -> list[str]:
        args: list[str] = []
        for entry in self._extra_hosts:
            args += ["--add-host", entry]
        return args

    def _isolation(self, *, probe: bool = False) -> list[str]:
        if self.proxied:
            # 447: probes need no network; the job's network and proxy are added by start().
            return ["--network", "none"] if probe else self._hosts()
        args = self._hosts()
        if self._egress:
            # The performer image's entry point locks egress down to these hosts.
            args += ["--cap-add", "NET_ADMIN", "-e", f"PERFORMER_EGRESS_ALLOWLIST={','.join(self._egress)}"]
        return args

    async def start(self, launch: StageJobLaunch) -> str:
        await self._require_runtime()
        name = job_name(launch.job_dir)
        egress: list[str] = []
        if self.proxied:
            if await self._container_state(name) is RunState.RUNNING:
                return name  # started before (a restart, a race): leave it and its proxy alone
            proxy = await self._egress_up(name, launch.provider)
            egress = ["--network", name + EGRESS_SUFFIX, "-e", f"{EGRESS_PROXY_ENV}={proxy}"]
        try:
            return await self._start_job(launch, name, egress)
        except BaseException:
            if egress:
                await self._egress_down(name)
            raise

    async def _start_job(self, launch: StageJobLaunch, name: str, egress: list[str]) -> str:
        mount = str(launch.job_dir)
        args = [
            "run", *self._run_options(), "-d", "--rm", "--name", name,
            "--label", f"{STAGE_JOB_LABEL}={launch.ref}",
            "--label", f"{PROVIDER_LABEL}={launch.provider}",
            *self._isolation(),
            *egress,
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
                if await self._container_state(name) is RunState.RUNNING:
                    # Started before (a restart, a race): the job is that container.
                    return name
                # A container of an earlier attempt that has ended but is not gone yet.
                await self._call(["rm", "-f", name])
                code, out, err = await self._call(args)
        if code == 0:
            logger.info("stage_job.started", kind=launch.kind, job=launch.ref, container=name, image=self.image)
            return name
        self._check_runtime_refused(err)
        msg = f"could not start the {launch.kind} stage job {launch.ref} (docker exit {code}): {_tail(err or out)}"
        raise TransportError(msg)

    async def state(self, job_dir: Path) -> RunState:
        name = job_name(job_dir)
        state = await self._container_state(name)
        if state is RunState.GONE and self.proxied:
            await self._egress_down(name)  # 447: the job is over, so is its way out
        return state

    async def _container_state(self, name: str) -> RunState:
        code, out, err = await self._call(["container", "inspect", "--format", "{{.State.Status}}", name])
        if code == 0:
            return RunState.RUNNING if out.strip() in ("created", "running", "restarting", "paused") else RunState.GONE
        if "no such" in err.lower():
            return RunState.GONE
        msg = f"docker could not say whether stage job {name} is running: {_tail(err)}"
        raise TransportError(msg)

    async def stop(self, job_dir: Path) -> None:
        with contextlib.suppress(TransportError):
            await self._call(["rm", "-f", job_name(job_dir)])
        if self.proxied:
            await self._egress_down(job_name(job_dir))

    # -- 447: the egress network and proxy of a sandboxed job with an allowlist -------------

    async def _egress_up(self, name: str, provider: str) -> str:
        """Make the job's internal network and its proxy; the proxy's URL on that network.

        Anything that fails removes what was made and raises: the job is then never started,
        so it never runs with a route out that the allowlist does not cover.
        """
        egress = name + EGRESS_SUFFIX
        with contextlib.suppress(TransportError):
            await self._sweep_egress()
        await self._egress_down(name)  # an earlier attempt's, if any: the job is not running
        labels = [
            "--label", f"{EGRESS_LABEL}={name}",
            "--label", f"{EGRESS_CREATED_LABEL}={int(time.time())}",
            "--label", f"{PROVIDER_LABEL}={provider}",
        ]
        try:
            await self._egress_step(
                "create its internal network", ["network", "create", "--internal", *EGRESS_NETWORK_OPTIONS, *labels, egress],
            )
            await self._egress_step("start its proxy", [
                "run", *self._run_options(), "-d", "--rm", "--name", egress, *labels,
                "--user", "65534:65534", "--read-only", *self._hosts(),
                "--entrypoint", "python", self.image,
                "-m", EGRESS_PROXY_MODULE, "--allow", ",".join(self._egress), "--port", str(EGRESS_PROXY_PORT),
                "--idle-exit", str(EGRESS_PROXY_IDLE_EXIT_SECONDS),
            ])
            await self._egress_step("attach its proxy to the internal network", ["network", "connect", egress, egress])
            address = await self._proxy_address(egress)
        except BaseException:
            await self._egress_down(name)
            raise
        logger.info("stage_job.egress_proxy", container=name, network=egress, hosts=list(self._egress))
        return f"http://{address}:{EGRESS_PROXY_PORT}"

    async def _egress_step(self, what: str, args: list[str]) -> str:
        code, out, err = await self._call(args)
        if code != 0:
            self._check_runtime_refused(err)
            msg = f"could not set up the egress proxy of a stage job: docker could not {what}: {_tail(err or out)}"
            raise TransportError(msg)
        return out

    async def _proxy_address(self, egress: str) -> str:
        out = await self._egress_step(
            "read its proxy's address", ["container", "inspect", "--format", "{{json .NetworkSettings.Networks}}", egress],
        )
        address = network_address(out, egress)
        if not address:
            msg = f"could not set up the egress proxy of a stage job: its proxy has no address on {egress}"
            raise TransportError(msg)
        return address

    async def _egress_down(self, name: str) -> None:
        """Remove a job's proxy, then its network. Best effort: the sweep catches what is left."""
        egress = name + EGRESS_SUFFIX
        with contextlib.suppress(TransportError):
            await self._call(["rm", "-f", egress])
        with contextlib.suppress(TransportError):
            await self._call(["network", "rm", egress])

    async def _sweep_egress(self) -> None:
        """Remove egress networks (and proxies) whose job is gone: a restart, or a job that ended."""
        code, out, _err = await self._call([
            "network", "ls", "--filter", f"label={EGRESS_LABEL}",
            "--format", f'{{{{.Name}}}}\t{{{{.Label "{EGRESS_LABEL}"}}}}\t{{{{.Label "{EGRESS_CREATED_LABEL}"}}}}',
        ])
        if code != 0:
            return
        now = time.time()
        for line in out.splitlines():
            network, job, created = ([*line.strip().split("\t"), "", ""])[:3]
            if not job or network != job + EGRESS_SUFFIX:
                continue
            try:
                young = now - float(created) < EGRESS_SWEEP_GRACE_SECONDS
            except ValueError:
                young = False
            if young or await self._container_state(job) is RunState.RUNNING:
                continue
            logger.info("stage_job.egress_swept", container=job, network=network)
            await self._egress_down(job)

    async def probe(
        self, argv: Sequence[str], *, env_names: Sequence[str] = (), env: dict[str, str] | None = None,
        within: float = 60.0,
    ) -> tuple[int | None, str, str]:
        await self._require_runtime()
        name = f"{_NAME_PREFIX}probe-{uuid.uuid4().hex[:12]}"
        args = ["run", *self._run_options(), "--rm", "--name", name, *self._isolation(probe=True)]
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
            self._check_runtime_refused(err)
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


def network_address(inspected: str, network: str) -> str:
    """The container's IPv4 address on ``network``, from ``{{json .NetworkSettings.Networks}}``."""
    try:
        networks = json.loads(inspected)
    except ValueError:
        return ""
    entry = networks.get(network) if isinstance(networks, dict) else None
    address = entry.get("IPAddress") if isinstance(entry, dict) else None
    return address if isinstance(address, str) else ""


def _tail(text: str) -> str:
    return (text or "").strip()[-_TAIL:]
