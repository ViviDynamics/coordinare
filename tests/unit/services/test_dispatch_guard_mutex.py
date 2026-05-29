"""Per-(card, stage) mutex correctness (spec 076 T036, FR-006).

Two concurrent coroutines on the same (card_id, performer_stage) MUST
serialise.  Two concurrent coroutines on DIFFERENT tuples MUST run
fully in parallel.
"""
from __future__ import annotations

import asyncio

import pytest

from coordinare.services.dispatch_guard import acquire_dispatch_lock, dispatch_mutex


@pytest.mark.asyncio
async def test_mutex_serialises_same_card_stage() -> None:
    """Two coroutines on the SAME (card, stage) tuple cannot overlap.

    Run two coroutines concurrently; each appends to a shared list
    under the mutex with an await in the middle.  If serialisation
    works, the appends form two contiguous blocks (A_in, A_out, B_in,
    B_out) — never interleaved (A_in, B_in, A_out, B_out).
    """
    events: list[str] = []

    async def task(name: str) -> None:
        async with dispatch_mutex("PVTI_X", "implementing"):
            events.append(f"{name}_in")
            await asyncio.sleep(0.01)  # yield to event loop
            events.append(f"{name}_out")

    await asyncio.gather(task("A"), task("B"))

    # Whichever ran first must complete in/out before the other starts in
    assert events[0].endswith("_in")
    assert events[1] == events[0].replace("_in", "_out")
    assert events[2].endswith("_in")
    assert events[3] == events[2].replace("_in", "_out")


@pytest.mark.asyncio
async def test_mutex_parallel_for_different_cards() -> None:
    """Two coroutines on DIFFERENT (card, stage) tuples MAY interleave."""
    events: list[str] = []

    async def task(card_id: str, name: str) -> None:
        async with dispatch_mutex(card_id, "implementing"):
            events.append(f"{name}_in")
            await asyncio.sleep(0.01)
            events.append(f"{name}_out")

    await asyncio.gather(task("PVTI_X", "A"), task("PVTI_Y", "B"))

    # Interleaved order is permitted: A_in B_in A_out B_out (or any of
    # the 4! orderings that respect within-task order).  Specifically,
    # if both are truly parallel, A_in and B_in both appear before
    # either A_out or B_out.
    in_events = [i for i, e in enumerate(events) if e.endswith("_in")]
    out_events = [i for i, e in enumerate(events) if e.endswith("_out")]
    assert min(out_events) > max(in_events)  # both `_in` precede any `_out`


@pytest.mark.asyncio
async def test_mutex_parallel_for_different_stages_same_card() -> None:
    """The mutex granularity is (card_id, performer_stage) — different
    stages of the same card MAY run in parallel if the graph permits."""
    events: list[str] = []

    async def task(stage: str, name: str) -> None:
        async with dispatch_mutex("PVTI_X", stage):
            events.append(f"{name}_in")
            await asyncio.sleep(0.01)
            events.append(f"{name}_out")

    await asyncio.gather(task("implementing", "I"), task("reviewing", "R"))

    in_events = [i for i, e in enumerate(events) if e.endswith("_in")]
    out_events = [i for i, e in enumerate(events) if e.endswith("_out")]
    assert min(out_events) > max(in_events)  # both `_in` precede any `_out`


@pytest.mark.asyncio
async def test_mutex_released_on_exception() -> None:
    """An exception inside the `async with` MUST still release the lock,
    so a subsequent acquirer can proceed."""
    lock = acquire_dispatch_lock("PVTI_RELEASE_TEST", "implementing")

    with pytest.raises(RuntimeError, match="boom"):
        async with dispatch_mutex("PVTI_RELEASE_TEST", "implementing"):
            assert lock.locked()
            raise RuntimeError("boom")

    # Lock MUST be released after the exception propagates
    assert not lock.locked()


@pytest.mark.asyncio
async def test_mutex_waited_event_emitted_when_contested(monkeypatch) -> None:
    """If the lock was contested (held by another waiter at the time of
    acquire), the helper emits ``dispatch_performer.mutex_waited``.

    Uncontested acquisitions do NOT emit this event (it would flood
    the log on cold paths).
    """
    from coordinare.services import dispatch_guard as dg_mod

    info_calls: list[tuple[str, dict]] = []
    original_info = dg_mod.logger.info

    def _capture_info(event: str, **kwargs):  # type: ignore[no-untyped-def]
        info_calls.append((event, kwargs))
        return original_info(event, **kwargs)

    monkeypatch.setattr(dg_mod.logger, "info", _capture_info)

    async def task(name: str) -> None:
        async with dispatch_mutex("PVTI_WAIT_TEST", "implementing"):
            await asyncio.sleep(0.02)

    await asyncio.gather(task("A"), task("B"))

    waited = [c for c in info_calls if c[0] == "dispatch_performer.mutex_waited"]
    assert len(waited) == 1, f"expected 1 waited event, got {len(waited)}: {info_calls}"
    assert waited[0][1]["card_id"] == "PVTI_WAIT_TEST"
    assert waited[0][1]["performer_stage"] == "implementing"
    assert waited[0][1]["wait_ms"] >= 0
