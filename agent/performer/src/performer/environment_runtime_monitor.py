"""Keep private runtime bounds active for the owning job's whole lifetime."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from coordinare_service_inference.environment.runtime_execution import run_guarded

if TYPE_CHECKING:
    from coordinare_service_inference.environment.runtime_supervisor import RuntimeLease


@dataclass
class RuntimeMonitor:
    lease: RuntimeLease
    owner: asyncio.Task[Any] | None
    stopping: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[None] | None = None
    failed: bool = False

    async def watch(self) -> None:
        try:
            await run_guarded(self.lease, self.stopping.wait())
        except (Exception, asyncio.CancelledError):
            self.failed = True
            if not self.stopping.is_set() and self.owner is not None and not self.owner.done():
                self.owner.cancel('private runtime resource monitor failed')

    async def stop(self) -> None:
        self.stopping.set()
        try:
            if self.task is not None:
                await self.task
        finally:
            self.owner = None
        if self.failed:
            raise ValueError('private runtime resource monitor failed')


def start_runtime_monitor(lease: RuntimeLease) -> RuntimeMonitor:
    monitor = RuntimeMonitor(lease, asyncio.current_task())
    monitor.task = asyncio.create_task(monitor.watch())
    return monitor
