"""Let standalone runtime owners unwind asynchronous cleanup on SIGTERM."""
from __future__ import annotations

import asyncio
import signal
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Coroutine
    from typing import Any


def run_cli[T](operation: Coroutine[Any, Any, T]) -> T:
    terminated = False

    async def run() -> T:
        nonlocal terminated
        loop = asyncio.get_running_loop()
        owner = asyncio.current_task()
        previous = signal.getsignal(signal.SIGTERM)

        def terminate() -> None:
            nonlocal terminated
            if not terminated:
                terminated = True
                if owner is not None:
                    owner.cancel()

        signal.signal(signal.SIGTERM, lambda _signum, _frame: loop.call_soon_threadsafe(terminate))
        try:
            return await operation
        finally:
            signal.signal(signal.SIGTERM, previous)

    try:
        result = asyncio.run(run())
    except asyncio.CancelledError:
        if terminated:
            raise SystemExit(128 + signal.SIGTERM) from None
        raise
    if terminated:
        # Cancellation must not become a success if project-facing work swallowed it.
        raise SystemExit(128 + signal.SIGTERM)
    return result
