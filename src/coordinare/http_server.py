from __future__ import annotations

import asyncio
from contextlib import contextmanager
from typing import TYPE_CHECKING

import uvicorn

if TYPE_CHECKING:
    from collections.abc import Iterator

HTTP_DRAIN_SECONDS = 20
LISTENER_POLL_SECONDS = 0.01
LISTENER_START_ERROR = "HTTP server exited before starting"


class ManagedServer(uvicorn.Server):
    """Leave signal ownership to the process running both HTTP listeners."""

    @contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


async def wait_for_listener(server: uvicorn.Server, task: asyncio.Task[None]) -> None:
    while not server.started:
        if task.done():
            await task
            raise RuntimeError(LISTENER_START_ERROR)
        await asyncio.sleep(LISTENER_POLL_SECONDS)
