"""SSE broadcaster for the dashboard (436)."""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from coordinare.services.activity_log import ActivityEntry

SUBSCRIBER_QUEUE_SIZE = 32


def _discard_event_notification(q: asyncio.Queue[dict[str, Any] | None]) -> None:
    """Replace invalidations without displacing state or changing task counts.

    Runs synchronously on the event loop, so consumers cannot observe the drain.
    """
    retained = []
    while not q.empty():
        item = q.get_nowait()
        q.task_done()
        if item is None or item.get("_event") != "events_changed":
            retained.append(item)
    for item in retained:
        q.put_nowait(item)


class SSEBroadcaster:
    """Fan-out SSE events to all connected browser tabs.

    Each subscriber gets its own asyncio.Queue.  put_nowait() is used so that
    a slow browser tab can never stall the daemon poll loop. State updates take
    priority over event-history invalidations when the queue is full.
    """

    def __init__(self) -> None:
        self._queues: set[asyncio.Queue[dict[str, Any] | None]] = set()

    def subscribe(self) -> asyncio.Queue[dict[str, Any] | None]:
        """Register a new client queue and return it."""
        q: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue(maxsize=SUBSCRIBER_QUEUE_SIZE)
        self._queues.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[dict[str, Any] | None]) -> None:
        """Remove a client queue (called from the SSE generator finally block)."""
        self._queues.discard(q)

    def broadcast(self, payload: dict[str, Any]) -> None:
        """Push payload to all subscribers; drops silently for full queues.

        Iterates a list() snapshot of _queues so that concurrent disconnects
        (which call unsubscribe) cannot cause RuntimeError: Set changed size
        during iteration.
        """
        for q in list(self._queues):
            if payload.get("_event") == "events_changed" or (
                q.full() and payload.get("_event", "state_update") == "state_update"
            ):
                _discard_event_notification(q)
            if not q.full():
                q.put_nowait(payload)

    def broadcast_activity(self, entries: list[ActivityEntry]) -> None:
        """138 T011: fan out an activity batch as a tagged payload (FR-001).

        Rides the existing broadcast path, so backpressure behaviour is
        unchanged: a slow client drops the batch and recovers on its next
        reconnect backfill (FR-004).
        """
        if not entries:
            return
        self.broadcast({
            "_event": "activity_event",
            "entries": [e.to_dict() for e in entries],
        })

    def shutdown(self) -> None:
        """Wake all SSE generators so they exit cleanly on server shutdown.

        Sends a None sentinel to every subscriber queue.  The sse_stream()
        generator treats None as a stop signal and returns, allowing uvicorn
        to close the connection and proceed with its graceful shutdown.
        """
        for q in list(self._queues):
            if not q.full():
                q.put_nowait(None)
