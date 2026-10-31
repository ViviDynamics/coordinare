"""Reap killed subprocesses without waiting on pipes an escaped descendant keeps open."""

from __future__ import annotations

import asyncio
import contextlib

_REAP_PIPE_GRACE_SECONDS = 5.0


async def _discard_output(reader: asyncio.StreamReader | None) -> None:
    if reader is not None:
        while await reader.read(64 * 1024):
            pass


async def _reap(proc: asyncio.subprocess.Process) -> None:
    # Paused stdout/stderr can keep the Python 3.12 process transport alive after SIGKILL.
    # Discard in bounded chunks, concurrently, rather than retaining cancelled output.
    drains = [asyncio.create_task(_discard_output(proc.stdout)),
              asyncio.create_task(_discard_output(proc.stderr))]
    waited = asyncio.ensure_future(proc.wait())
    try:
        _, pending = await asyncio.wait({waited, *drains}, timeout=_REAP_PIPE_GRACE_SECONDS)
        if pending:
            # A descendant that escaped the killed group can hold an inherited pipe open indefinitely,
            # which also blocks Process.wait() on Python 3.12. Close our ends, even when the child itself
            # has already exited, so neither the reap nor the pipe transports outlive this cleanup.
            transport = getattr(proc, '_transport', None)
            if transport is not None:
                transport.close()
        await waited
    finally:
        for drain in drains:
            drain.cancel()
        await asyncio.gather(*drains, return_exceptions=True)


async def reap_owned(proc: asyncio.subprocess.Process) -> None:
    """Finish reaping even if the caller is cancelled meanwhile (bounded by the grace), then re-raise."""
    reaped = asyncio.create_task(_reap(proc))
    cancelled = False
    while not reaped.done():
        try:
            await asyncio.shield(reaped)
        except asyncio.CancelledError:
            cancelled = True
    reaped.result()
    if cancelled:
        raise asyncio.CancelledError


async def kill_and_reap(proc: asyncio.subprocess.Process) -> None:
    """Kill a subprocess (an already-exited one raises ProcessLookupError, ignored), then reap it with reap_owned."""
    with contextlib.suppress(ProcessLookupError):
        proc.kill()
    await reap_owned(proc)
