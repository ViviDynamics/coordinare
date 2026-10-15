"""171 review fix: a transient poll error does not hold a side run's slot until restart.

A poll error records the run as failed with ``writer_active`` set, so the slot stays
held until the writer's termination is confirmed. The next cycle asks ``recover`` again
(the daemon's ``_recover``), and a writer that is still reachable is polled again. Once
the poll succeeds the run completes and the slot frees for the next card.
"""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from coordinare.services.pipeline_budget import select_pipelines
from tests.unit.test_171_side_run_budget import (
    _daemon,
    _fresh,
    _parked,
    _run,
)


class _FlakySvc:
    """Fails the first status poll with a transient error, then reports the run done."""

    def __init__(self) -> None:
        self.dispatched: list[str] = []
        self.polls = 0

    async def dispatch_card(self, ctx: dict[str, Any], workspace_info: Any = None) -> dict[str, Any]:
        self.dispatched.append(ctx["card_id"])
        return {"session_id": f"side-{ctx['card_id']}"}

    async def check_status(self, session_id: str) -> dict[str, Any]:
        self.polls += 1
        if self.polls == 1:
            raise ConnectionError("transient poll failure")
        return {"status": "docs_committed", "head_sha": "abc"}


@pytest.mark.asyncio
async def test_a_transient_poll_error_is_confirmed_on_the_next_cycle_and_frees_the_slot() -> None:
    sessions = {"P": _parked("P"), "F": _fresh("F")}
    svc = _FlakySvc()
    d = _daemon(sessions, svc, limit=1)
    await _run(d, sessions, {"P"})
    await asyncio.gather(*d._documenting_side_tasks)
    side = sessions["P"]["documenting_side"]
    assert side["status"] == "failed" and side["writer_active"] is True
    assert select_pipelines(sessions, 1) == {"P"}, "an unconfirmed writer still holds the slot"

    await _run(d, sessions, {"P"})
    await asyncio.gather(*d._documenting_side_tasks)
    side = sessions["P"]["documenting_side"]
    assert side["status"] == "done" and side["writer_active"] is False
    assert select_pipelines(sessions, 1) == {"F"}, "the slot frees once the run is confirmed done"
