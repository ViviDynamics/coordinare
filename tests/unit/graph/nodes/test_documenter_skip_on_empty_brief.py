"""165 FR-014 / US2: an empty documentation brief means no documenter side run
is ever dispatched, and the card proceeds to implementing only."""
from __future__ import annotations

import pytest

from coordinare.graph.nodes.dispatch_performer import documenter_side_run_wanted
from coordinare.services.documenting_side import run_cycle, should_dispatch

_SMALL = {
    "summary": "s", "milestones": [{"goal": "g", "scope": [], "done_when": "d"}], "modules": [],
    "data_model": {"changes": []}, "interfaces": [], "risks": [],
    "criteria": [{"surface": "/", "action": "a", "expected": "e", "kind": "functional"}],
    "docs": [], "size": "small", "blueprint_hash": "h", "created_at": "t",
}


def test_empty_docs_means_no_side_run_wanted():
    assert documenter_side_run_wanted(_SMALL) is False
    ok, reason = should_dispatch({"performer_stage": "implementing", "blueprint": _SMALL, "documenting_side": None})
    assert not ok and "no documentation brief" in reason


@pytest.mark.asyncio
async def test_the_cycle_dispatches_nothing_for_an_empty_brief():
    class _Svc:
        dispatched = 0

        async def dispatch_card(self, ctx, workspace_info=None):
            _Svc.dispatched += 1
            return {"session_id": "x"}

        async def check_status(self, sid):
            return {"status": "docs_committed"}

    sessions = {"c1": {"card_id": "c1", "performer_stage": "implementing", "blueprint": _SMALL, "documenting_side": None}}

    async def resolve(cid, s):
        return ({}, None)

    assert await run_cycle(sessions, svc=_Svc(), resolve=resolve, spawn=lambda c: c.close()) == 0
    assert _Svc.dispatched == 0 and sessions["c1"]["documenting_side"] is None
