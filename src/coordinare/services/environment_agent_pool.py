"""A bounded pool of shard agents sharing one attempt budget; parallelism never adds allowances."""
from __future__ import annotations

import asyncio
import contextlib
from typing import TYPE_CHECKING, Any

from coordinare.services.environment_attempt import EnvironmentDiscoveryError

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine, Sequence

    from coordinare.services.environment_attempt import AttemptBudget

DEFAULT_CONCURRENCY = 12


class ShardOverflowError(Exception):
    """A shard agent could not finish within its context; the remainder is split and requeued."""

    def __init__(self, remainder: Any) -> None:
        super().__init__('shard exceeded its context share')
        self.remainder = remainder


async def run_pool[T, R](work: Sequence[T], worker: Callable[[T], Coroutine[Any, Any, R]], *, budget: AttemptBudget,
                   split: Callable[[T, ShardOverflowError], Sequence[T]], concurrency: int = DEFAULT_CONCURRENCY) -> list[R]:
    """Run every item; results follow the input order, with split remainders in place of their parent.

    Any failure other than an overflow cancels every running worker and is raised once.
    """
    if concurrency < 1:
        message = 'agent pool concurrency must be positive'
        raise ValueError(message)
    queue: list[tuple[tuple[int, ...], T]] = [((index,), item) for index, item in enumerate(work)]
    results: dict[tuple[int, ...], R] = {}
    running: dict[asyncio.Task[R], tuple[tuple[int, ...], T]] = {}
    try:
        while queue or running:
            while queue and len(running) < concurrency:
                budget.check()
                key, item = queue.pop(0)
                running[asyncio.create_task(worker(item))] = (key, item)
            done, _pending = await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
            for task in sorted(done, key=lambda finished: running[finished][0]):
                key, item = running.pop(task)
                try:
                    results[key] = task.result()
                except ShardOverflowError as overflow:
                    parts = list(split(item, overflow))
                    if not parts or parts == [item]:
                        message = 'a shard agent overflows even its smallest share'
                        raise EnvironmentDiscoveryError(message, code='shard_context_limit') from None
                    queue.extend(((*key, index), part) for index, part in enumerate(parts))
                    queue.sort(key=lambda queued: queued[0])
    finally:
        for task in running:
            task.cancel()
        for task in running:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
    return [results[key] for key in sorted(results)]
