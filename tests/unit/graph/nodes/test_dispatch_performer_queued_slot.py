"""354: queued-for-slot stamping in ``dispatch_performer``.

A card whose role pool is saturated must be stamped with the first-cycle
``slot_queued_since`` marker so the dashboard can render a real wait time
instead of "Dispatching · —", and the marker must clear the moment the
card acquires a slot. No scheduling behaviour changes: the node still
returns with phase ``dispatching`` and retries next cycle.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from coordinare.graph.nodes.dispatch_performer import dispatch_performer
from coordinare.graph.state import initial_state
from coordinare.services.slot_manager import SlotManager


class _StubGithub:
    """Bare github service: passes the prerequisite gate, no API methods."""


class _StubService:
    async def dispatch_card(self, *args, **kwargs):  # pragma: no cover
        raise AssertionError("dispatch_card must not run for a queued card")


def _saturated_state() -> dict:
    """State whose implementing pool is already saturated by another card."""
    state = initial_state()
    state["current_card"] = {"id": "PVTI_Q", "title": "queued", "status": "IN_PROGRESS"}
    # Production sets phase=dispatching upstream of this node (check_board
    # hands the card over already marked dispatching) — that is exactly why
    # queued rows read as "Dispatching" on the board.
    state["phase"] = "dispatching"
    state["performer_stage"] = "implementing"
    state["agent_dispatch"] = {}
    state["performer_services"] = {"implementing": _StubService()}
    state["github_service"] = _StubGithub()
    slot_manager = SlotManager()
    slot_manager.register_pool("implementing", services=[_StubService()], max_concurrency=1)
    assert slot_manager.acquire("implementing", "PVTI_OTHER") is not None
    state["slot_manager"] = slot_manager
    return state


@pytest.mark.asyncio
async def test_at_capacity_stamps_slot_queued_since() -> None:
    """First cycle wanting a slot and finding none: stamp the marker."""
    state = _saturated_state()

    result = await dispatch_performer(state)

    assert result.get("phase") == "dispatching"  # unchanged — card retries next cycle
    assert isinstance(result.get("slot_queued_since"), datetime)


@pytest.mark.asyncio
async def test_at_capacity_keeps_first_stamp() -> None:
    """A second queued cycle must NOT overwrite the first stamp."""
    state = _saturated_state()
    first = datetime.now(UTC)  # deliberate: predates any stamp the node would set
    state["slot_queued_since"] = first

    result = await dispatch_performer(state)

    assert result.get("slot_queued_since") == first


@pytest.mark.asyncio
async def test_override_restart_clears_stale_stamp() -> None:
    """An override that moves the card to another role discards the old
    queue's stamp — the new queue stamps from the override, so the board
    never reports a wait that predates the current queue."""
    state = _saturated_state()
    old = datetime.now(UTC) - timedelta(hours=1)
    state["slot_queued_since"] = old
    state["lifecycle_sequence"] = ["implementing", "reviewing"]
    state["pending_override"] = {"action": "restart", "target_stage": "reviewing"}

    # The reviewing pool is saturated by another card, so the node reaches
    # the at-capacity branch for the NEW stage and re-stamps from now.
    slot_manager = state["slot_manager"]
    slot_manager.register_pool("reviewing", services=[_StubService()], max_concurrency=1)
    assert slot_manager.acquire("reviewing", "PVTI_OTHER2") is not None

    result = await dispatch_performer(state)

    assert result.get("performer_stage") == "reviewing"
    assert result.get("slot_queued_since") != old
    assert isinstance(result.get("slot_queued_since"), datetime)


@pytest.mark.asyncio
async def test_veto_override_clears_stamp_on_terminal_exit() -> None:
    """A terminal override (veto → blocked) leaves dispatching without
    acquiring a slot — the queue stint is over, so the stamp must not
    survive into the next one after the board unblocks."""
    state = _saturated_state()
    state["slot_queued_since"] = datetime.now(UTC) - timedelta(hours=1)
    state["pending_override"] = {"action": "veto"}

    result = await dispatch_performer(state)

    assert result.get("phase") == "blocked"
    assert result.get("slot_queued_since") is None


@pytest.mark.asyncio
async def test_acquiring_slot_clears_stamp() -> None:
    """Acquiring a slot resets the queued marker (the wait is over)."""
    state = _saturated_state()
    state["slot_queued_since"] = datetime.now(UTC)
    slot_manager = state["slot_manager"]
    slot_manager.release("implementing", "PVTI_OTHER")  # free the pool

    result = await dispatch_performer(state)

    assert result.get("slot_queued_since") is None
