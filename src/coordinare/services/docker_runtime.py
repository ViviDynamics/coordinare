"""Docker implementation of :class:`PerformerRuntime` (spec 146 / issue #200).

This is a **wrapper, not a rewrite**. Every behaviour still lives in
``performer_lifecycle``; this module only presents it through the runtime
Protocol so a second runtime can exist beside it.

Two reasons it wraps rather than absorbs:

* ``performer_lifecycle`` is imported directly by ``bench/runner.py``,
  ``__main__.py`` and five test modules. Moving its contents would break all of
  them for no benefit.
* Spec 146's FR-002 requires the Docker path to behave identically, evidenced by
  its existing tests passing **unmodified**. Code that is not touched cannot
  regress, which makes that requirement nearly free to satisfy.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

import structlog

from coordinare.services import performer_lifecycle
from coordinare.services.performer_runtime import StartedPerformer
from coordinare.services.run_log import LOG_CHUNK_BYTES

if TYPE_CHECKING:  # pragma: no cover - typing only
    from collections.abc import AsyncGenerator
    from pathlib import Path

    from coordinare.models.performer_endpoint import PerformerEndpointConfig
    from coordinare.services.run_log import RunLogIO

_log = structlog.get_logger(__name__)


class DockerRuntime:
    """Runs performers as local Docker containers.

    *image_resolver* (optional, #526) pins the configured image tag to its
    registry digest before the container starts. ``None`` (tests, bench) pins
    nothing and the image is used verbatim.
    """

    def __init__(self, image_resolver: Any = None) -> None:
        self._image_resolver = image_resolver

    async def start_ephemeral(
        self,
        config: PerformerEndpointConfig,
        *,
        extra_labels: dict[str, str] | None = None,
        backend: str | None = None,
    ) -> StartedPerformer:
        started = await performer_lifecycle.start_ephemeral(
            await self._pinned(config),
            extra_labels=extra_labels,
            backend=backend,
        )
        return StartedPerformer(handle=started.container_id, endpoint=started.endpoint)

    async def _pinned(
        self, config: PerformerEndpointConfig,
    ) -> PerformerEndpointConfig:
        if self._image_resolver is None or config.image is None:
            return config
        image = await self._image_resolver.pin(config.image)
        if image == config.image:
            return config
        return config.model_copy(update={"image": image})

    async def stop(
        self,
        handle: str,
        *,
        timeout_s: int = 10,
        host_log_dir: Path | None = None,
        performer_id: str | None = None,
    ) -> None:
        await performer_lifecycle.stop(
            handle,
            timeout_s=timeout_s,
            host_log_dir=host_log_dir,
            performer_id=performer_id,
        )

    async def tail_logs(self, handle: str, *, lines: int = 200) -> list[str]:
        import asyncio

        try:
            proc = await asyncio.create_subprocess_exec(
                "docker",
                "logs",
                "--tail",
                str(lines),
                handle,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=10.0)
        except (OSError, TimeoutError):
            return []
        return stdout.decode(errors="replace").splitlines()[-lines:]

    async def iter_logs(self, handle: str, *, io: RunLogIO | None = None) -> AsyncGenerator[bytes, None]:
        """132: the container's complete output, read from ``docker logs`` in chunks.

        stderr is merged into the stream, as the live tail merges it. A reader that
        stops early kills the ``docker logs`` process. The pipe is read asynchronously,
        so ``io`` is not needed and is ignored.
        """
        proc = await asyncio.create_subprocess_exec(
            "docker",
            "logs",
            handle,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        try:
            if proc.stdout is None:
                return
            while chunk := await proc.stdout.read(LOG_CHUNK_BYTES):
                yield chunk
            returncode = await proc.wait()
            if returncode != 0:
                _log.warning("docker_runtime.logs_failed", container_id=handle, rc=returncode)
        finally:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()

    async def cleanup_orphaned(self, performer_id: str | None = None) -> int:
        return await performer_lifecycle.cleanup_orphaned_containers(performer_id)
