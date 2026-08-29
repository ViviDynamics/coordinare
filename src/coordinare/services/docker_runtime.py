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

from typing import TYPE_CHECKING

from coordinare.services import performer_lifecycle
from coordinare.services.performer_runtime import StartedPerformer

if TYPE_CHECKING:  # pragma: no cover - typing only
    from pathlib import Path

    from coordinare.models.performer_endpoint import PerformerEndpointConfig


class DockerRuntime:
    """Runs performers as local Docker containers."""

    async def start_ephemeral(
        self,
        config: PerformerEndpointConfig,
        *,
        extra_labels: dict[str, str] | None = None,
    ) -> StartedPerformer:
        started = await performer_lifecycle.start_ephemeral(config, extra_labels=extra_labels)
        return StartedPerformer(handle=started.container_id, endpoint=started.endpoint)

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

    async def cleanup_orphaned(self, performer_id: str | None = None) -> int:
        return await performer_lifecycle.cleanup_orphaned_containers(performer_id)
