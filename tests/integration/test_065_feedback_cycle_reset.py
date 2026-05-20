"""065 US4 — integration tests for the un-block feedback-cycle reset.

Background
----------
When a card exhausts its feedback budget, ``_feedback_cycle_exhausted`` in
``monitor_performer.py`` blocks the card (sets ``phase="blocked"``) but
leaves ``feedback_cycle_count`` at its terminal value (>= ``max_feedback_
cycles``).  The operator then moves the card from the BLOCKED column on
the project board back to TODO, expecting a fresh attempt.  On the next
daemon cycle ``check_board`` picks up the card, but the existing reset
gate at ``check_board.py:864`` only fires when the picked-up card id
*differs* from the prior card id — for a same-card un-block the counter
carries over and the next feedback signal trips
``feedback_cycle_exhausted`` again immediately.

What these tests assert
-----------------------
- FR-012: when a session whose ``current_card.previous_status == "BLOCKED"``
  is picked up from TODO, ``feedback_cycle_count`` MUST be reset to 0 even
  if the card id is unchanged.
- FR-013: monotonic ``total_feedback_cycles`` and ``triage_blocks`` counters
  MUST persist across the un-block / re-block boundary — only the operative
  ``feedback_cycle_count`` resets.
- FR-015: a ``dispatcher.feedback_cycle_reset`` structured log MUST be
  emitted on reset carrying ``{card_id, prior_count, total_feedback_cycles,
  triage_blocks}``.
- Control: when a card is picked up that was never blocked,
  ``feedback_cycle_count`` is untouched (no false-positive reset, no log).
"""
from __future__ import annotations

import pytest
import structlog

from coordinare.graph.nodes.check_board import check_board
from coordinare.graph.state import initial_state

pytestmark = pytest.mark.asyncio


class _GitHubUnblockedCard:
    """Board where ITEM_X has moved out of BLOCKED into TODO."""

    async def poll_board(self):
        return {
            "snapshot": {
                "TODO": ["ITEM_X"],
                "IN_PROGRESS": [],
                "IN_REVIEW": [],
                "BLOCKED": [],
            },
            "titles": {"ITEM_X": "Fix the auth bug"},
            "descriptions": {"ITEM_X": "Body"},
            "issue_numbers": {"ITEM_X": 101},
            "issue_urls": {"ITEM_X": "https://github.com/org/repo/issues/101"},
            "content_node_ids": {"ITEM_X": "PVTI_X"},
        }


class _GitHubFreshCard:
    """Board where a brand-new TODO card appears with no prior blocked history."""

    async def poll_board(self):
        return {
            "snapshot": {
                "TODO": ["ITEM_FRESH"],
                "IN_PROGRESS": [],
                "IN_REVIEW": [],
                "BLOCKED": [],
            },
            "titles": {"ITEM_FRESH": "Fresh card"},
            "descriptions": {"ITEM_FRESH": ""},
            "issue_numbers": {"ITEM_FRESH": 9},
            "issue_urls": {"ITEM_FRESH": ""},
            "content_node_ids": {"ITEM_FRESH": "PVTI_FRESH"},
        }


# ---------------------------------------------------------------------------
# Exploit — same-card un-block must reset the operative counter
# ---------------------------------------------------------------------------


async def test_check_board_resets_feedback_cycle_count_on_unblock() -> None:
    """Exploit: a card whose session was blocked at the feedback budget
    limit is moved back to TODO by the operator.  ``check_board`` must
    reset ``feedback_cycle_count`` to 0 so the next dispatch has a fresh
    budget — the existing same-card preservation path leaves the counter
    pinned at the limit and re-blocks on the next feedback signal.
    """
    state = initial_state()
    state["github_service"] = _GitHubUnblockedCard()
    state["current_card"] = {
        "id": "ITEM_X",
        "title": "Fix the auth bug",
        "status": "BLOCKED",
        "previous_status": "BLOCKED",
    }
    state["phase"] = "blocked"
    state["feedback_cycle_count"] = 5  # at max
    state["triage_blocks"] = 1  # type: ignore[typeddict-unknown-key]
    state["total_feedback_cycles"] = 5  # type: ignore[typeddict-unknown-key]

    result = await check_board(state)

    assert result["feedback_cycle_count"] == 0, (
        "un-block reset did not fire: feedback_cycle_count carried over "
        f"({result['feedback_cycle_count']}). Same-card un-block must reset."
    )
    # Stats persist
    assert result.get("triage_blocks") == 1, (
        f"triage_blocks lost across reset: {result.get('triage_blocks')!r}"
    )
    assert result.get("total_feedback_cycles") == 5, (
        f"total_feedback_cycles lost across reset: "
        f"{result.get('total_feedback_cycles')!r}"
    )
    # Card dispatched again
    assert result["phase"] == "dispatching"
    assert result["current_card"]["id"] == "ITEM_X"


async def test_check_board_emits_feedback_cycle_reset_log_on_unblock() -> None:
    """FR-015: reset is auditable via ``dispatcher.feedback_cycle_reset``."""
    state = initial_state()
    state["github_service"] = _GitHubUnblockedCard()
    state["current_card"] = {
        "id": "ITEM_X",
        "title": "Fix the auth bug",
        "status": "BLOCKED",
        "previous_status": "BLOCKED",
    }
    state["phase"] = "blocked"
    state["feedback_cycle_count"] = 5
    state["triage_blocks"] = 1  # type: ignore[typeddict-unknown-key]
    state["total_feedback_cycles"] = 5  # type: ignore[typeddict-unknown-key]

    with structlog.testing.capture_logs() as logs:
        await check_board(state)

    reset = [
        r for r in logs if r.get("event") == "dispatcher.feedback_cycle_reset"
    ]
    assert reset, (
        "no dispatcher.feedback_cycle_reset emitted; got events: "
        f"{[r.get('event') for r in logs]}"
    )
    record = reset[0]
    assert record.get("card_id") == "ITEM_X"
    assert record.get("prior_count") == 5
    assert record.get("triage_blocks") == 1
    assert record.get("total_feedback_cycles") == 5


# ---------------------------------------------------------------------------
# Controls — no false-positive resets, no log spam on fresh cards
# ---------------------------------------------------------------------------


async def test_check_board_does_not_reset_or_log_for_fresh_card() -> None:
    """Control: a card picked up with no prior blocked history must not
    trip the reset path or emit a reset log.  Guards against the
    un-block detection misfiring on the happy path."""
    state = initial_state()
    state["github_service"] = _GitHubFreshCard()
    # No current_card, no prior triage history.

    with structlog.testing.capture_logs() as logs:
        result = await check_board(state)

    assert result["feedback_cycle_count"] == 0  # unchanged from initial_state
    assert not [
        r for r in logs if r.get("event") == "dispatcher.feedback_cycle_reset"
    ], "reset log fired for a fresh card with no prior blocked state"


async def test_check_board_preserves_counters_when_card_was_not_blocked() -> None:
    """Control: same-card pickup whose previous_status is NOT 'BLOCKED'
    (e.g. dispatching → dispatching across cycles) MUST NOT reset
    counters, because no un-block occurred.  This isolates the new branch
    to the un-block edge only."""
    state = initial_state()
    state["github_service"] = _GitHubUnblockedCard()
    state["current_card"] = {
        "id": "ITEM_X",
        "title": "Fix the auth bug",
        "status": "TODO",
        "previous_status": "TODO",  # not previously blocked
    }
    state["phase"] = "dispatching"
    state["feedback_cycle_count"] = 2  # mid-budget — must be preserved
    state["total_feedback_cycles"] = 2  # type: ignore[typeddict-unknown-key]
    state["triage_blocks"] = 0  # type: ignore[typeddict-unknown-key]

    with structlog.testing.capture_logs() as logs:
        result = await check_board(state)

    assert result["feedback_cycle_count"] == 2, (
        "operative counter clobbered on non-blocked same-card pickup: "
        f"{result['feedback_cycle_count']}"
    )
    assert not [
        r for r in logs if r.get("event") == "dispatcher.feedback_cycle_reset"
    ], "reset log fired for a non-blocked same-card pickup"


# ---------------------------------------------------------------------------
# Monotonic stats — total_feedback_cycles increments on every feedback round
# and triage_blocks increments on every exhaustion-block.
# ---------------------------------------------------------------------------


async def test_feedback_cycle_exhausted_increments_monotonic_stats() -> None:
    """FR-013: ``_feedback_cycle_exhausted`` must increment
    ``total_feedback_cycles`` every time it is called (each feedback
    round), and ``triage_blocks`` only when the cycle limit is hit and
    the card is moved to BLOCKED."""
    from types import SimpleNamespace

    from coordinare.graph.nodes.monitor_performer import _feedback_cycle_exhausted

    # Round 1 — below limit; total bumps, triage_blocks stays 0.
    state = initial_state()
    state["config"] = SimpleNamespace(max_feedback_cycles=2)
    state["current_card"] = {
        "id": "ITEM_X", "title": "x", "issue_number": 1, "pr_url": "",
    }
    state["feedback_cycle_count"] = 0
    state["total_feedback_cycles"] = 0  # type: ignore[typeddict-unknown-key]
    state["triage_blocks"] = 0  # type: ignore[typeddict-unknown-key]

    result = _feedback_cycle_exhausted(state, "ITEM_X", "reviewing", "rev", [])
    # Below limit returns None — state is mutated in place.
    assert result is None
    assert state["feedback_cycle_count"] == 1
    assert state.get("total_feedback_cycles") == 1
    assert state.get("triage_blocks") == 0

    # Round 2 — still below limit, total bumps again.
    _feedback_cycle_exhausted(state, "ITEM_X", "reviewing", "rev", [])
    assert state["feedback_cycle_count"] == 2
    assert state.get("total_feedback_cycles") == 2
    assert state.get("triage_blocks") == 0

    # Round 3 — exceeds limit, card is blocked, triage_blocks bumps.
    result = _feedback_cycle_exhausted(state, "ITEM_X", "reviewing", "rev", [])
    assert result is not None  # returned state — card blocked
    assert result["phase"] == "blocked"
    assert result.get("total_feedback_cycles") == 3
    assert result.get("triage_blocks") == 1


