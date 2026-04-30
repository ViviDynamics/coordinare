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
            f"docker {_redact_docker_args(args)} timed out after {timeout}s"
        ) from None
    return proc.returncode or 0, stdout_b.decode().strip(), stderr_b.decode().strip()


async def start_ephemeral(config: PerformerEndpointConfig) -> StartedContainer:
    """Run a fresh container for *config* and return its id + endpoint URL.

    The container is started detached on a host port (either operator-pinned
    via ``config.port`` or chosen by docker via ``-p 0:8088``); the resolved
    host port is read back via ``docker port``. Auth, volumes, and env wiring
    for secrets are applied here when present.
    """
    if config.image is None:
        raise ContainerStartError(f"performer {config.id} has no image configured")

    args: list[str] = ["run", "-d", "--rm", "--label", f"coordinare.performer.id={config.id}"]

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

    for vol in config.volumes:
        args += ["-v", f"{vol.host_path}:{vol.container_path}:{vol.mode}"]

    args += [config.image]

    rc, stdout, stderr = await _run_docker(*args)
    if rc != 0 or not stdout:
        raise ContainerStartError(
            f"docker run failed for {config.id} (rc={rc}): {stderr or stdout}"
        )
    container_id = stdout.splitlines()[0].strip()

    rc, port_out, port_err = await _run_docker("port", container_id, "8088/tcp")
    if rc != 0 or not port_out:
        await _safe_stop(container_id)
        raise ContainerStartError(
            f"docker port lookup failed for {container_id} (rc={rc}): {port_err or port_out}"
        )
    # `docker port` may emit one line per family (0.0.0.0 + ::), e.g.
    #   0.0.0.0:49160
    #   [::]:49160
    # We pick the first IPv4 line.
    host_port: str | None = None
    for line in port_out.splitlines():
        line = line.strip()
        if line.startswith("0.0.0.0:") or line.startswith("127.0.0.1:"):
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


async def stop(container_id: str, *, timeout_s: int = 10) -> None:
    """Stop a container. Best-effort — failures are logged, not raised."""
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
                    + (f": {last_error}" if last_error else "")
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
    "start_ephemeral",
    "stop",
    "wait_ready",
]
