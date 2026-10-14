"""171 review fixes: a side run must not free the review cap, and must not wait on an ineligible card.

Both scenarios go through a daemon cycle and the real eligibility and selection path
(``_dispatch_documenting_side_runs`` then ``_compute_session_eligibilities``), with no
stubbed selection.
"""
from __future__ import annotations

from typing import Any

import pytest

from coordinare.daemon import _compute_session_eligibilities
from tests.unit.test_171_side_run_budget import (
    _daemon,
    _drain,
    _fresh,
    _parked,
    _run,
    _session,
    _Svc,
)


def _review_daemon(sessions: dict[str, Any], svc: _Svc, *, limit: int, review_limit: int) -> Any:
    d = _daemon(sessions, svc, limit=limit)
    d._state["config"].max_cards_in_review = review_limit
    return d


@pytest.mark.asyncio
async def test_a_side_run_on_a_parked_card_keeps_it_counted_against_the_review_cap() -> None:
    """Scenario A. Two cards wait on review under a cap of two. P gets a side run.

    P is still waiting on a human, so it must still count toward the cap, and the
    fresh card F must stay held. Before the fix, P's live side writer made it stop
    counting as parked, the cap freed up, and F was admitted while two cards were in review.
    """
    sessions = {"Q": _parked("Q"), "P": _parked("P"), "F": _fresh("F")}
    svc = _Svc()
    d = _review_daemon(sessions, svc, limit=2, review_limit=2)
    await _run(d, sessions, {"P"})
    await _drain(d)
    assert svc.dispatched == ["P"]
    eligibilities = _compute_session_eligibilities(d._state, sessions, 2)
    assert eligibilities["F"].eligible is False
    assert eligibilities["F"].reason == "review_capacity"
    assert "F" not in (d._state.get("_pipeline_selected") or set())


@pytest.mark.asyncio
async def test_a_paused_started_card_does_not_hold_the_slot_a_side_run_needs() -> None:
    """Scenario B. A is started, operator-paused, and so ineligible. P is parked and wants a side run.

    The lifecycle will never admit A, so A must not reserve the one slot the side run
    needs. Before the fix, the side-run admission ignored eligibility, so A kept the
    slot and P's side run waited forever.
    """
    paused = _session("A", "IN_PROGRESS", "monitoring_pr", board_paused=True, review_wait="automation",
                      pr_number=9, pr_url="https://github.com/o/r/pull/9", pr_node_id="PR_9")
    sessions = {"A": paused, "P": _parked("P")}
    svc = _Svc()
    d = _review_daemon(sessions, svc, limit=1, review_limit=None)
    await _run(d, sessions, {"A", "P"})
    await _drain(d)
    assert svc.dispatched == ["P"]
