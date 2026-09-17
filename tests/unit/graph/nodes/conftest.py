"""Test invariants shared across graph-node unit tests.

066 (FR-010): asserts the I3 invariant — `state["current_card"]` MUST be the
derived mirror of `state["active_sessions"][state["active_card_id"]]["current_card"]`,
or both sides MUST be None.  Contract:
specs/066-unify-card-pickup/contracts/current_card-derivation.md.

This conftest auto-applies the invariant to every test in this folder by
wrapping ``check_board`` to assert after each invocation.  Tests that mutate
state directly (without going through check_board) can also call
``assert_current_card_invariant(state)`` for an explicit post-condition.
"""

from __future__ import annotations

from typing import Any

import pytest


def _check_invariant(state: dict[str, Any]) -> None:
    sessions = state.get("active_sessions") or {}
    active_id = state.get("active_card_id")
    expected: dict[str, Any] | None
    if active_id and active_id in sessions:
        session = sessions[active_id]
        expected = session.get("current_card") if isinstance(session, dict) else None
    else:
        expected = None
    assert state.get("current_card") == expected, (
        "066 I3 invariant violated: top-level current_card is not the derived "
        f"mirror of active_sessions[{active_id!r}]['current_card']. "
        f"current_card={state.get('current_card')!r} expected={expected!r}"
    )


@pytest.fixture
def assert_current_card_invariant():
    """Explicit assertion helper for tests that mutate state directly.

    Most tests don't need this — the autouse ``_i3_invariant_guard`` fixture
    below already enforces the invariant after every ``check_board`` call.
    Use this fixture when the test mutates state outside of a check_board
    invocation and wants to verify the invariant at a custom point.
    """

    return _check_invariant


@pytest.fixture(autouse=True)
def _i3_invariant_guard(
    monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest,
) -> None:
    """Auto-enforce I3 after every ``check_board`` invocation in this folder.

    Wraps ``coordinare.graph.nodes.check_board.check_board`` so each call's
    return value is checked against the invariant before being handed back to
    the test.  Any node-level regression that leaves ``current_card`` out of
    sync with ``active_sessions[active_card_id]`` will fail the test loudly
    rather than silently passing.

    Defends against import-rebinding: in addition to patching the module
    attribute, this fixture also rebinds any ``check_board`` symbol that the
    *test module* itself imported directly (``from … import check_board``).
    Without this second step, ``from coordinare.graph.nodes.check_board import
    check_board`` captures the original reference at import time and bypasses
    the module-attribute patch — letting an invariant regression slip through.
    """
    from coordinare.graph.nodes import check_board as cb_module

    original = cb_module.check_board

    async def _guarded(state: dict[str, Any]):  # type: ignore[no-untyped-def]
        result = await original(state)
        _check_invariant(result if isinstance(result, dict) else state)
        return result

    monkeypatch.setattr(cb_module, "check_board", _guarded)

    # If the test module bound the function by ``from … import check_board``,
    # patch that local reference too.  Only rebind if it's identical to the
    # original (don't clobber unrelated symbols that happen to share the name).
    test_module = request.module
    if test_module is not None:
        local = test_module.__dict__.get("check_board")
        if local is original:
            monkeypatch.setitem(test_module.__dict__, "check_board", _guarded)
