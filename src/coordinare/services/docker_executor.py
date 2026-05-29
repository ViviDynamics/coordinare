"""Thin wrapper around the ``docker`` CLI used by the reconciliation pass.

Centralises subprocess invocation, JSON parsing, and timeout handling so
the rest of the dispatcher dedup machinery does not reach for
``subprocess`` directly.

See ``specs/076-qa-cycle/contracts/docker-labels.md`` for the
label-vs-snapshot matching rules this module supports.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime

import structlog

logger = structlog.get_logger(__name__)


# Re-exported as the public exception type so callers can catch it
# without importing subprocess directly.


class DockerUnreachableError(RuntimeError):
    """Raised when ``docker ps`` (or equivalent) fails to reach the daemon.

    The reconciliation pass treats this as a hard failure: per FR-012 the
    daemon refuses to dispatch any ephemeral performer until Docker is
    reachable again.
    """


@dataclass(frozen=True)
class ContainerInfo:
    """Subset of ``docker ps`` output needed by reconciliation."""

    container_id: str
    name: str
    image: str
    started_at: datetime
    labels: dict[str, str] = field(default_factory=dict)


async def _run_docker(*args: str, timeout: float = 5.0) -> tuple[int, str, str]:
    """Run ``docker <args>`` and return ``(returncode, stdout, stderr)``.

    Raises :class:`DockerUnreachableError` if the daemon cannot be reached
    (rc=125, "Cannot connect to the Docker daemon") or the subprocess
    exceeds the wall-clock timeout.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            "docker", *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise DockerUnreachableError(f"docker CLI not found: {exc}") from exc

    try:
        stdout_b, stderr_b = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        # ``proc.kill()`` delivers SIGKILL asynchronously; the child
        # may still hold its stdout/stderr file descriptors until the
        # OS reaps it.  ``await proc.wait()`` (with a short additional
        # window) closes the FDs and prevents accumulation over many
        # repeated timeouts.
        proc.kill()
        # Reap the child to release stdout/stderr FDs.  If the kill
        # itself doesn't take within 2s, let the GC eventually reap;
        # we cannot block the dispatch loop on a hung Docker child.
        import contextlib
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(proc.wait(), timeout=2.0)
        raise DockerUnreachableError(
            f"docker {args[0] if args else ''} timed out after {timeout}s"
        ) from None

    stdout = stdout_b.decode(errors="replace").strip()
    stderr = stderr_b.decode(errors="replace").strip()
    rc = proc.returncode or 0
    # rc=125 is Docker's "daemon not reachable" exit code.  Treat as
    # DockerUnreachableError so callers can route through the FR-012
    # fail-closed path.
    if rc == 125 or "Cannot connect to the Docker daemon" in stderr:
        raise DockerUnreachableError(f"docker daemon unreachable: {stderr or stdout}")
    return rc, stdout, stderr


class DockerExecutor:
    """076 (T040-T043): Async wrapper around the ``docker`` CLI.

    All public methods accept an optional ``timeout`` (default 5 s) and
    raise :class:`DockerUnreachableError` if the daemon cannot be
    reached or the operation exceeds the timeout.

    Instances are stateless; one per daemon process is sufficient.
    """

    async def list_containers_by_label(
        self,
        label_filters: dict[str, str],
        *,
        timeout: float = 5.0,
    ) -> list[ContainerInfo]:
        """Return every running container matching ALL of the given labels.

        Uses ``docker ps --filter label=k=v --format '{{json .}}'``.
        Multiple filters compound (all must match — Docker's default
        AND semantics).  Empty dict returns every running container the
        daemon can see (including non-coordinare ones); callers should
        always pass at least ``{"coordinare.performer.id": "..."}`` or
        ``{"coordinare.spec_version": "076"}`` to scope the result.
        """
        args: list[str] = ["ps", "--format", "{{json .}}"]
        for k, v in label_filters.items():
            args += ["--filter", f"label={k}={v}"]
        rc, stdout, stderr = await _run_docker(*args, timeout=timeout)
        if rc != 0:
            raise DockerUnreachableError(
                f"docker ps failed (rc={rc}): {stderr or stdout}"
            )

        results: list[ContainerInfo] = []
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("docker_executor.malformed_ps_line", line=line[:200])
                continue
            container_id = str(entry.get("ID") or "")
            if not container_id:
                continue
            name = str(entry.get("Names") or "")
            image = str(entry.get("Image") or "")
            # `docker ps --format` returns CreatedAt as a human-readable
            # string ("2026-05-28 22:06:50 +0000 UTC").  Parse defensively;
            # fall back to a synthetic UTC=now if unparseable.
            started_at = _parse_docker_created_at(entry.get("CreatedAt"))
            # `docker ps` "Labels" is a comma-separated "k=v,k=v" string;
            # parse into a dict for the reconciliation matcher.
            labels = _parse_docker_label_string(entry.get("Labels", ""))
            results.append(
                ContainerInfo(
                    container_id=container_id,
                    name=name,
                    image=image,
                    started_at=started_at,
                    labels=labels,
                )
            )
        return results

    async def stop_container(
        self,
        container_id: str,
        *,
        timeout: float = 5.0,
    ) -> bool:
        """Stop a container (SIGTERM with grace period, then SIGKILL).

        Returns True on success, False if ``docker stop`` failed.  Never
        raises :class:`DockerUnreachableError` unless the daemon itself
        is down (a non-zero rc on a specific container is not a daemon
        failure).
        """
        grace_seconds = max(1, int(timeout))
        rc, _stdout, stderr = await _run_docker(
            "stop", "--time", str(grace_seconds), container_id,
            timeout=timeout + 5.0,  # docker stop should return within grace + buffer
        )
        if rc == 0:
            return True
        logger.warning(
            "docker_executor.stop_failed",
            container_id=container_id,
            rc=rc,
            stderr=stderr[:200],
        )
        # Try docker kill as a last resort (FR-004 best-effort path).
        rc, _stdout, _stderr = await _run_docker("kill", container_id, timeout=5.0)
        return rc == 0

    async def probe_healthz(
        self,
        container_id: str,
        *,
        timeout: float = 15.0,
        internal_port: int = 8088,
        auth_token: str | None = None,
    ) -> bool:
        """076 (T054): HTTP health-check a performer container's job-runner.

        Resolves the host port via ``docker port``, then GETs
        ``http://127.0.0.1:<port>/healthz`` with the given auth token.
        Returns True on HTTP 200 within ``timeout``, False on any failure
        (port not exposed, connection refused, non-200, exception).

        Lives on DockerExecutor so test doubles can mock it directly,
        and so the underlying httpx dependency stays inside this module.
        """
        port = await self.port_of(container_id, internal_port=internal_port)
        if port is None:
            return False
        import httpx
        url = f"http://127.0.0.1:{port}/healthz"
        headers: dict[str, str] = {}
        if auth_token:
            headers["Authorization"] = f"Bearer {auth_token}"
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.get(url, headers=headers)
                return resp.status_code == 200
        except Exception as exc:
            logger.debug(
                "docker_executor.probe_healthz_failed",
                container_id=container_id,
                url=url,
                error=str(exc),
            )
            return False

    async def port_of(self, container_id: str, *, internal_port: int = 8088) -> int | None:
        """Resolve a container's host port for its internal performer port.

        Returns the host port number, or None if the container does not
        expose the requested port.  Caller may treat None as "container
        is not reachable" and route to reap-and-replace.
        """
        rc, stdout, stderr = await _run_docker(
            "port", container_id, f"{internal_port}/tcp", timeout=3.0
        )
        if rc != 0 or not stdout:
            logger.debug(
                "docker_executor.port_lookup_failed",
                container_id=container_id,
                rc=rc,
                stderr=stderr[:200],
            )
            return None
        # `docker port` returns "0.0.0.0:55555" or "[::]:55555"; parse
        # the trailing :PORT.
        try:
            return int(stdout.rsplit(":", 1)[-1].strip())
        except (ValueError, IndexError):
            return None


def _parse_docker_created_at(raw: str | None) -> datetime:
    """Best-effort parser for ``docker ps --format`` CreatedAt strings.

    Docker emits "2026-05-28 22:06:50 +0000 UTC".  We tolerate ISO8601
    inputs as well (some Docker versions vary).  On failure, return
    UTC=now so the reconciliation matcher never crashes on a single
    malformed entry.
    """
    if not raw or not isinstance(raw, str):
        return datetime.now(UTC)
    # Strip the trailing "UTC" if present
    raw = raw.replace(" UTC", "").strip()
    # Try a handful of formats
    for fmt in (
        "%Y-%m-%d %H:%M:%S %z",
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%d %H:%M:%S",
    ):
        try:
            dt = datetime.strptime(raw, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return dt
        except ValueError:
            continue
    return datetime.now(UTC)


def _parse_docker_label_string(raw: str | None) -> dict[str, str]:
    """Parse Docker's comma-separated label string into a dict.

    Input format: ``"k=v,k=v"`` — empty values produce empty-string
    entries.  Returns an empty dict on None or empty input.
    """
    if not raw or not isinstance(raw, str):
        return {}
    result: dict[str, str] = {}
    for piece in raw.split(","):
        piece = piece.strip()
        if not piece:
            continue
        if "=" in piece:
            k, v = piece.split("=", 1)
            result[k.strip()] = v.strip()
        else:
            result[piece] = ""
    return result


__all__ = [
    "ContainerInfo",
    "DockerExecutor",
    "DockerUnreachableError",
]
