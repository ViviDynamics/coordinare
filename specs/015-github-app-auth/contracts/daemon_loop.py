"""
Contract: Daemon polling loop changes for webhook trigger and poll=0 support.

Branch: 015-github-app-auth
"""
from __future__ import annotations

import asyncio
import contextlib


# ---------------------------------------------------------------------------
# Conceptual replacement for the two `await self._sleep(N)` calls in daemon.py
# ---------------------------------------------------------------------------

async def wait_for_next_cycle(
    interval_seconds: int,
    webhook_trigger: asyncio.Event,
    stop_event: asyncio.Event,
) -> None:
    """Block until the next cycle should run.

    Behaviour:
    - interval_seconds > 0: wait up to `interval_seconds` for a webhook trigger
      (using asyncio.wait_for); proceed regardless (timer OR event).
    - interval_seconds == 0: block indefinitely on `webhook_trigger`; only
      the stop path can unblock the daemon externally via stop_event.

    In both cases, clear the webhook_trigger after waking so it acts as
    a one-shot signal for the current wake-up.

    This function NEVER raises; TimeoutError is suppressed.
    """
    if interval_seconds > 0:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(webhook_trigger.wait(), float(interval_seconds))
        webhook_trigger.clear()
    else:
        # poll=0: wait indefinitely for a webhook or stop signal
        done, _ = await asyncio.wait(
            {
                asyncio.ensure_future(webhook_trigger.wait()),
                asyncio.ensure_future(stop_event.wait()),
            },
            return_when=asyncio.FIRST_COMPLETED,
        )
        webhook_trigger.clear()


# ---------------------------------------------------------------------------
# CoordinareDaemon.__init__ additions (signature fragment)
# ---------------------------------------------------------------------------

class _DaemonInitFragment:
    """Documents the new parameters and attributes added to CoordinareDaemon.__init__.

    Not a real class — exists only for contract documentation.
    """

    def __init__(
        self,
        # ... existing params unchanged ...
        webhook_trigger: asyncio.Event | None = None,
    ) -> None:
        # New attribute
        self._webhook_trigger: asyncio.Event = webhook_trigger or asyncio.Event()

        # poll_interval_seconds=0 startup log
        # if self._poll_interval_seconds == 0:
        #     logger.info("polling_disabled")
