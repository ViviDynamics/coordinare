"""Tests for cycle_id log correlation (spec 009 US2, T015)."""
from __future__ import annotations

import structlog.contextvars

from coordinare.observability import bind_cycle_id, clear_cycle_id


def test_cycle_id_present_on_log_entries_during_cycle() -> None:
    """bind_cycle_id adds cycle_id to the current structlog context."""
    clear_cycle_id()
    bind_cycle_id("test-cycle-abc")
    ctx = structlog.contextvars.get_contextvars()
    assert ctx.get("cycle_id") == "test-cycle-abc"
    clear_cycle_id()


def test_cycle_id_absent_on_log_entries_outside_cycle() -> None:
    """Without bind_cycle_id, no cycle_id exists in the structlog context."""
    clear_cycle_id()
    ctx = structlog.contextvars.get_contextvars()
    assert "cycle_id" not in ctx


def test_cycle_id_unique_across_sequential_cycles() -> None:
    """Two sequential cycles produce distinct cycle_ids."""
    ids: list[str] = []
    for i in range(2):
        clear_cycle_id()
        cycle_id = f"cycle-{i}"
        bind_cycle_id(cycle_id)
        ctx = structlog.contextvars.get_contextvars()
        ids.append(ctx["cycle_id"])
        clear_cycle_id()

    assert ids[0] != ids[1]


def test_clear_cycle_id_removes_field_from_subsequent_logs() -> None:
    """After clear_cycle_id(), the context contains no cycle_id."""
    bind_cycle_id("will-be-cleared")
    clear_cycle_id()
    ctx = structlog.contextvars.get_contextvars()
    assert "cycle_id" not in ctx


def test_exception_in_cycle_clears_cycle_id_in_finally() -> None:
    """Simulates a cycle that raises: cycle_id must be absent after clear_cycle_id."""
    bind_cycle_id("exception-cycle")
    ctx_before = structlog.contextvars.get_contextvars()
    assert "cycle_id" in ctx_before

    try:
        raise RuntimeError("simulated failure")
    except RuntimeError:
        clear_cycle_id()

    ctx_after = structlog.contextvars.get_contextvars()
    assert "cycle_id" not in ctx_after
