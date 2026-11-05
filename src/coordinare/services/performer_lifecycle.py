"""Performer container lifecycle (spec 056, T026).

Drives the local ``docker`` CLI via ``asyncio.create_subprocess_exec`` to
start, stop, and wait for readiness of containerized performers. Persistent
performers skip ``start_ephemeral``/``stop`` and only call ``wait_ready`` once
at registration.
"""

from __future__ import annotations

import asyncio
import re
import shlex
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog

from coordinare.env_placeholders import drop_unset_placeholders, is_secret_env_key
from coordinare.lib.container_env import docker_environment
from coordinare.transport.http_transport import (
    PerformerHTTPClient,
    PerformerUnreachableError,
)

if TYPE_CHECKING:
    from coordinare_service_inference.environment.runtime import RuntimePolicy

    from coordinare.models.performer_endpoint import (
        PerformerEndpointConfig,
        PerformerStatus,
    )

logger = structlog.get_logger(__name__)
_warned_unauthenticated_endpoints: set[str] = set()


class LifecycleError(RuntimeError):
    """Base error for performer lifecycle operations."""


class ContainerStartError(LifecycleError):
    """Raised when ``docker run`` fails to start a container."""


class ReadinessTimeoutError(LifecycleError):
    """Raised when a performer fails to reach a non-``starting`` state in time."""


@dataclass(frozen=True)
class StartedContainer:
    container_id: str
    endpoint: str  # e.g. "http://127.0.0.1:8080"


def _redact_docker_args(args: tuple[str, ...]) -> str:
    """Render docker args as a log-safe string, redacting -e KEY=VALUE secrets."""
    out: list[str] = []
    i = 0
    while i < len(args):
        a = args[i]
        if a == "-e" and i + 1 < len(args):
            key = args[i + 1].split("=", 1)[0]
            out.append(shlex.quote(a))
            out.append(shlex.quote(f"{key}=<redacted>"))
            i += 2
        else:
            out.append(shlex.quote(a))
            i += 1
    return " ".join(out)


async def _run_docker(*args: str, timeout: float = 30.0) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        "docker",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        raise ContainerStartError(
            f"docker {_redact_docker_args(args)} timed out after {timeout}s",
        ) from None
    return proc.returncode or 0, stdout_b.decode().strip(), stderr_b.decode().strip()


_LABEL_KEY_RE = __import__("re").compile(r"^coordinare\.[a-z0-9._-]+$")


def _validate_extra_label(key: str, value: str) -> None:
    """076 FR-009: validate Docker label key + value before passing to ``docker run``.

    Keys MUST match ``^coordinare\\.[a-z0-9._-]+$``; values MUST be non-empty
    strings ≤256 chars.  Caller (``http_performer_service.dispatch_card``)
    is responsible for not constructing invalid pairs; this validation
    catches typos at the launch site so a malformed label cannot silently
    skip the reconciliation pass.
    """
    if not isinstance(key, str) or not _LABEL_KEY_RE.match(key):
        msg = f"invalid extra_label key {key!r}: must match {_LABEL_KEY_RE.pattern}"
        raise ContainerStartError(msg)
    if not isinstance(value, str) or not value:
        msg = f"invalid extra_label value for {key!r}: must be non-empty string"
        raise ContainerStartError(msg)
    if len(value) > 256:
        msg = f"extra_label value for {key!r} exceeds 256 chars (got {len(value)})"
        raise ContainerStartError(msg)


def normalize_backend_name(backend: str) -> str:
    """Canonicalize a performer-backend name for the ``BACKEND`` env var.

    Mirrors the normalization used by ``config_validation`` and the performer
    factory's ``get_backend``: ``claude-code`` and ``Claude_Code`` both become
    the canonical ``claude_code``. The image's entrypoint.sh matches the
    underscored spelling, so the env var must carry the canonical form.
    """
    return str(backend).replace("-", "_").lower()


def env_carries_set_value(env: dict[str, str], key: str) -> bool:
    """True when *env* carries a meaningful operator value for *key*.

    An empty value does not count as set (the entrypoint's ``case
    "${BACKEND:-}"`` treats empty exactly like unset), nor does an unresolved
    ``${VAR}`` placeholder (``os.path.expandvars`` leaves those literal in
    config.yaml): the env-forward loop drops such values, so the container
    would otherwise receive neither the configured value nor the derived one.
    """
    value = env.get(key)
    if not value:
        return False
    return not (isinstance(value, str) and value.startswith("${") and value.endswith("}"))


async def start_ephemeral(
    config: PerformerEndpointConfig,
    *,
    extra_labels: dict[str, str] | None = None,
    backend: str | None = None,
    runtime_policy: RuntimePolicy | None = None,
) -> StartedContainer:
    """Run a fresh container for *config* and return its id + endpoint URL.

    The container is started detached on a host port (either operator-pinned
    via ``config.port`` or chosen by docker via ``-p 127.0.0.1:0:8088``); the resolved
    host port is read back via ``docker port``. Auth, volumes, and env wiring
    for secrets are applied here when present.

    076 FR-009: ``extra_labels`` (if provided) is added as additional
    ``--label key=value`` args BEFORE the image argument.  Keys MUST match
    ``^coordinare\\.[a-z0-9._-]+$``; values MUST be non-empty strings
    ≤256 chars.  Validation failures raise :class:`ContainerStartError`.

    Issue #486: *backend* is the dispatch's resolved performer backend. The
    image's entrypoint installs the backend CLI at container start from
    ``BACKEND``, so it is injected as an env var here unless the operator
    already set ``BACKEND`` in ``performer_endpoints[].env``.
    """
    if config.image is None:
        raise ContainerStartError(f"performer {config.id} has no image configured")
    token = config.auth_token.get_secret_value() if config.auth_token is not None else ""
    if match := re.search(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}", token):
        name = match.group(1)
        message = (
            f"performer {config.id} auth_token still holds an unexpanded ${{{name}}} placeholder. "
            f"Set environment variable {name} to a secret value before launching."
        )
        raise ContainerStartError(message)
    if not token and config.id not in _warned_unauthenticated_endpoints:
        _warned_unauthenticated_endpoints.add(config.id)
        logger.warning(
            "performer_lifecycle.auth_disabled",
            performer_id=config.id,
            bind_host=config.bind_host,
            message=(
                "Performer auth disabled because auth_token is absent or empty. "
                "Run coordinare init --force or set auth_token to enable authentication."
            ),
        )

    args: list[str] = ["run", "-d", "--rm", "--label", f"coordinare.performer.id={config.id}"]

    # 151 (bench-only): `--add-host` entries (e.g. host.docker.internal:host-gateway)
    # let a bridged container resolve the host running the harness fakes. Empty in
    # production, so the run command is unchanged there. Containers stay BRIDGED —
    # there is deliberately no --network knob: host networking would share the
    # host's netns, so the entrypoint's egress iptables rules (--cap-add NET_ADMIN
    # below) would flush and DROP the *host's* OUTPUT chain, published ports would
    # collide 1:1 across concurrent performers, and Docker Desktop shares the VM's
    # netns rather than the host's anyway.
    for host_entry in config.extra_hosts:
        args += ["--add-host", host_entry]

    if extra_labels:
        for k, v in extra_labels.items():
            _validate_extra_label(k, v)
            args += ["--label", f"{k}={v}"]

    if runtime_policy is not None:
        if config.egress_allowlist:
            raise ContainerStartError('private runtime forbids the egress adapter privilege gain')
        limits = runtime_policy.resources
        args += ['--cap-drop', 'ALL', '--security-opt', 'no-new-privileges', '--cpus', str(limits.cpus),
                 '--memory', str(limits.memory_mib)+'m', '--memory-swap', str(limits.memory_mib)+'m',
                 '--pids-limit', '512']

    if config.egress_allowlist:
        args += ["--cap-add", "NET_ADMIN"]
        args += [
            "-e",
            f"PERFORMER_EGRESS_ALLOWLIST={','.join(config.egress_allowlist)}",
        ]
        logger.info(
            "performer_lifecycle.start_ephemeral_egress",
            performer_id=config.id,
            hosts=list(config.egress_allowlist),
        )

    args += ["-p", f"{config.bind_host}:{config.port or 0}:8088"]
    private_env: dict[str, str] = {}
    if token:
        private_env["PERFORMER_AUTH_TOKEN"] = token

    ss = config.secret_sources
    if not ss.init_payload:
        args += ["-e", "PERFORMER_SECRET_SOURCE_INIT_PAYLOAD=0"]
    if not ss.env:
        args += ["-e", "PERFORMER_SECRET_SOURCE_ENV=0"]
    if not ss.creds_file:
        args += ["-e", "PERFORMER_SECRET_SOURCE_CREDS_FILE=0"]
    elif ss.creds_file_path is not None:
        args += ["-e", f"PERFORMER_CREDS_FILE={ss.creds_file_path}"]

    logger.info(
        "performer_lifecycle.start_ephemeral_volumes",
        performer_id=config.id,
        volume_count=len(config.volumes),
        volumes=[
            v.docker_volume_arg() for v in config.volumes
        ],
    )
    for vol in config.volumes:
        args += ["-v", vol.docker_volume_arg()]

    # config.yaml uses os.path.expandvars, which leaves an unset ${VAR} as the literal text. Forwarding
    # that literal makes an unset variable look set, so an entry with an unset placeholder is left out
    # (252; a secret-looking key was refused at boot already, so this is the plain-key warning's entry).
    env_kept, env_dropped = drop_unset_placeholders(config.env)
    for key in env_dropped:
        logger.info(
            "performer_lifecycle.env_skipped_unresolved_placeholder",
            performer_id=config.id,
            env_key=key,
        )
    for key, val in env_kept.items():
        if is_secret_env_key(key):
            private_env.setdefault(key, val)
        else:
            args += ["-e", f"{key}={val}"]

    if backend and not env_carries_set_value(config.env, "BACKEND"):
        args += ["-e", f"BACKEND={normalize_backend_name(backend)}"]
        logger.info(
            "performer_lifecycle.backend_env_derived",
            performer_id=config.id,
            backend=backend,
        )

    with docker_environment(private_env) as env_path:
        if env_path is not None:
            args += ["--env-file", str(env_path)]
        args += [config.image]
        rc, stdout, stderr = await _run_docker(*args)
    if rc != 0 or not stdout:
        raise ContainerStartError(
            f"docker run failed for {config.id} (rc={rc}): {stderr or stdout}",
        )
    container_id = stdout.splitlines()[0].strip()

    rc, port_out, port_err = await _run_docker("port", container_id, "8088/tcp")
    if rc != 0 or not port_out:
        await _safe_stop(container_id)
        raise ContainerStartError(
            f"docker port lookup failed for {container_id} (rc={rc}): {port_err or port_out}",
        )
    # `docker port` may emit one line per family (0.0.0.0 + ::), e.g.
    #   0.0.0.0:49160
    #   [::]:49160
    # We pick the first IPv4 line.
    host_port: str | None = None
    for line in port_out.splitlines():
        line = line.strip()
        if line.startswith(("0.0.0.0:", "127.0.0.1:")):
            host_port = line.rsplit(":", 1)[1]
            break
    if host_port is None and port_out:
        host_port = port_out.splitlines()[0].rsplit(":", 1)[1].strip()
    if host_port is None:  # pragma: no cover — fallback always produces a string or raises
        await _safe_stop(container_id)
        raise ContainerStartError(f"could not resolve host port for {container_id}")

    endpoint = f"http://{config.connect_host}:{host_port}"
    logger.info(
        "performer_endpoint.transition",
        performer_id=config.id,
        from_state="unknown",
        to_state="starting",
        container_id=container_id,
        endpoint=endpoint,
    )
    return StartedContainer(container_id=container_id, endpoint=endpoint)


async def stop(container_id: str, *, timeout_s: int = 10) -> None:
    """Stop a container. Best-effort — failures are logged, not raised.

    The container's output is not copied here: the full redacted run log (132) is
    collected before this call, into the daemon-only run logs root (spec 208).
    """
    try:
        rc, _stdout, stderr = await _run_docker("stop", "-t", str(timeout_s), container_id)
    except ContainerStartError as exc:
        logger.warning("performer_lifecycle.stop_failed", container_id=container_id, error=str(exc))
        return
    if rc != 0:
        logger.warning(
            "performer_lifecycle.stop_failed",
            container_id=container_id,
            rc=rc,
            stderr=stderr,
        )


async def _safe_stop(container_id: str) -> None:
    try:
        await stop(container_id)
    except Exception as exc:
        logger.warning(
            "performer_lifecycle.cleanup_failed",
            container_id=container_id,
            error=str(exc),
        )


async def cleanup_orphaned_containers(performer_id: str | None = None) -> int:
    """Stop containers left behind by a previous coordinare crash.

    Finds all running containers with the ``coordinare.performer.id`` label and
    stops them. Returns the count stopped. Errors on individual stops are
    logged but do not abort cleanup of remaining containers.

    *performer_id* narrows the sweep to one endpoint's containers
    (``label=coordinare.performer.id=<id>``). The board-sim bench needs that: an
    unscoped sweep would also stop a production coordinare's performers running on
    the same host.
    """
    label = (
        f"label=coordinare.performer.id={performer_id}"
        if performer_id
        else "label=coordinare.performer.id"
    )
    rc, stdout, _stderr = await _run_docker(
        "ps", "--filter", label, "--format", "{{.ID}}",
        timeout=15.0,
    )
    if rc != 0 or not stdout:
        return 0
    ids = [line.strip() for line in stdout.splitlines() if line.strip()]
    if not ids:
        return 0
    logger.info(
        "performer_lifecycle.cleanup_orphaned_start",
        count=len(ids),
        container_ids=ids,
    )
    for cid in ids:
        await _safe_stop(cid)
    logger.info("performer_lifecycle.cleanup_orphaned_done", count=len(ids))
    return len(ids)


async def wait_ready(
    endpoint: str,
    auth_token: str | None,
    *,
    timeout: float = 120.0,
    poll_interval: float = 1.0,
    performer_id: str | None = None,
    client: PerformerHTTPClient | None = None,
) -> PerformerStatus:
    """Poll ``GET /status`` until ``availability != "starting"`` or *timeout*.

    Returns the first non-``starting`` :class:`PerformerStatus`. Raises
    :class:`ReadinessTimeoutError` on timeout. Connection errors are tolerated
    while the container warms up — they only fail the call once the deadline
    is reached.
    """
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    owns_client = client is None
    if client is None:
        client = PerformerHTTPClient(
            endpoint,
            auth_token=auth_token,
            timeout_seconds=min(10.0, timeout),
        )
    try:
        while True:
            try:
                status = await client.get_status()
            except PerformerUnreachableError as exc:
                last_error = exc
            else:
                if status.availability != "starting":
                    if performer_id is not None:
                        logger.info(
                            "performer_endpoint.transition",
                            performer_id=performer_id,
                            from_state="starting",
                            to_state=status.availability,
                            endpoint=endpoint,
                        )
                    return status
            if time.monotonic() >= deadline:
                raise ReadinessTimeoutError(
                    f"performer at {endpoint} not ready within {timeout}s"
                    + (f": {last_error}" if last_error else ""),
                )
            await asyncio.sleep(poll_interval)
    finally:
        if owns_client:
            await client.aclose()


__all__ = [
    "ContainerStartError",
    "LifecycleError",
    "ReadinessTimeoutError",
    "StartedContainer",
    "cleanup_orphaned_containers",
    "start_ephemeral",
    "stop",
    "wait_ready",
]
