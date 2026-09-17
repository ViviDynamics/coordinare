"""Performer container lifecycle (spec 056, T026).

Drives the local ``docker`` CLI via ``asyncio.create_subprocess_exec`` to
start, stop, and wait for readiness of containerized performers. Persistent
performers skip ``start_ephemeral``/``stop`` and only call ``wait_ready`` once
at registration.
"""

from __future__ import annotations

import asyncio
import shlex
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

import structlog

from coordinare.transport.http_transport import (
    PerformerHTTPClient,
    PerformerUnreachableError,
)

if TYPE_CHECKING:
    from pathlib import Path

    from coordinare.models.performer_endpoint import (
        PerformerEndpointConfig,
        PerformerStatus,
    )

logger = structlog.get_logger(__name__)


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


async def start_ephemeral(
    config: PerformerEndpointConfig,
    *,
    extra_labels: dict[str, str] | None = None,
) -> StartedContainer:
    """Run a fresh container for *config* and return its id + endpoint URL.

    The container is started detached on a host port (either operator-pinned
    via ``config.port`` or chosen by docker via ``-p 0:8088``); the resolved
    host port is read back via ``docker port``. Auth, volumes, and env wiring
    for secrets are applied here when present.

    076 FR-009: ``extra_labels`` (if provided) is added as additional
    ``--label key=value`` args BEFORE the image argument.  Keys MUST match
    ``^coordinare\\.[a-z0-9._-]+$``; values MUST be non-empty strings
    ≤256 chars.  Validation failures raise :class:`ContainerStartError`.
    """
    if config.image is None:
        raise ContainerStartError(f"performer {config.id} has no image configured")

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

    if config.port is not None:
        args += ["-p", f"{config.port}:8088"]
    else:
        args += ["-p", "0:8088"]

    if config.auth_token is not None:
        args += ["-e", f"PERFORMER_AUTH_TOKEN={config.auth_token.get_secret_value()}"]

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
            f"{v.host_path}:{v.container_path}:{v.mode}" for v in config.volumes
        ],
    )
    for vol in config.volumes:
        args += ["-v", f"{vol.host_path}:{vol.container_path}:{vol.mode}"]

    for key, val in config.env.items():
        # config.yaml uses os.path.expandvars, which leaves unset ${VAR} as the
        # literal string. Forwarding that literal into the container looks "set"
        # to os.environ.get() and bypasses fallback-on-empty defaults — drop it.
        if isinstance(val, str) and val.startswith("${") and val.endswith("}"):
            logger.info(
                "performer_lifecycle.env_skipped_unresolved_placeholder",
                performer_id=config.id,
                env_key=key,
            )
            continue
        args += ["-e", f"{key}={val}"]

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

    endpoint = f"http://127.0.0.1:{host_port}"
    logger.info(
        "performer_endpoint.transition",
        performer_id=config.id,
        from_state="unknown",
        to_state="starting",
        container_id=container_id,
        endpoint=endpoint,
    )
    return StartedContainer(container_id=container_id, endpoint=endpoint)


async def _dump_container_logs(
    container_id: str, host_log_dir: Path, *, performer_id: str | None = None,
) -> None:
    """Best-effort dump of ``docker logs <id>`` to *host_log_dir*.

    Runs before the container is removed by ``docker stop`` so a crashed
    job still leaves an artifact behind. Failures are logged, never raised.
    """
    try:
        host_log_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning(
            "performer_lifecycle.log_dump_mkdir_failed",
            container_id=container_id, error=str(exc),
        )
        return
    label = performer_id or "performer"
    out_path = host_log_dir / f"{label}.{container_id[:12]}.container.log"
    try:
        rc, stdout, stderr = await _run_docker("logs", container_id, timeout=15.0)
    except ContainerStartError as exc:
        logger.warning(
            "performer_lifecycle.log_dump_failed",
            container_id=container_id, error=str(exc),
        )
        return
    if rc != 0:
        logger.warning(
            "performer_lifecycle.log_dump_failed",
            container_id=container_id, rc=rc, stderr=stderr,
        )
        return
    # Cap at the trailing 5 MB so a runaway-logger container cannot fill the
    # host disk. The tail is what matters for post-mortems; older lines are
    # rarely useful and ``docker logs`` already has them if needed.
    max_bytes = 5_000_000
    if len(stdout) > max_bytes:
        stdout = (
            f"[truncated: kept last {max_bytes} bytes of "
            f"{len(stdout)} total]\n" + stdout[-max_bytes:]
        )
    try:
        out_path.write_text(stdout)
    except OSError as exc:
        logger.warning(
            "performer_lifecycle.log_dump_write_failed",
            container_id=container_id, path=str(out_path), error=str(exc),
        )


async def stop(
    container_id: str,
    *,
    timeout_s: int = 10,
    host_log_dir: Path | None = None,
    performer_id: str | None = None,
) -> None:
    """Stop a container. Best-effort — failures are logged, not raised.

    When *host_log_dir* is set, ``docker logs`` is captured into that
    directory before the container is removed.
    """
    if host_log_dir is not None:
        await _dump_container_logs(
            container_id, host_log_dir, performer_id=performer_id,
        )
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
