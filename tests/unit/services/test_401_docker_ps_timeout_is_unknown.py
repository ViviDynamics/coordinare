"""#401: a Docker that did not answer in 5s is not a container that is gone.

Card website#111 was restarted twice in 27 minutes while its performer was
running normally. Each time, ``docker ps`` (filtered by session label, 5s
deadline) timed out under load, ``handle_potentially_stale_session`` treated
``DockerUnreachableError`` exactly like "no container found", cleared the
session's dispatch and fresh-dispatched a new performer. The old container was
still running (the following ``docker stop`` also timed out). Two of the
card's attempts, and two system errors on its budget, were spent this way.

The startup pass handles the same condition the other way round: on
``docker_unreachable`` it refuses to dispatch anything. The per-cycle path
should at least not dispatch MORE.

A timeout is an unknown. Leave the session alone this cycle, count the
streak, and only fall back to fresh-dispatch after several consecutive
unknowns, which is when "Docker is genuinely gone" becomes the likelier
reading.

MUTATIONS THAT MUST FAIL A TEST HERE:
  M1  clear agent_dispatch on the first unreachable
      -> test_first_docker_timeout_defers_and_leaves_the_session_alone
  M2  ignore the streak (always defer, never escalate)
      -> test_repeated_docker_timeouts_eventually_fresh_dispatch
  M3  never reset the streak on a successful listing
      -> test_a_successful_listing_resets_the_streak
  M4  key the streak by target instead of by session (review)
      -> test_two_cards_without_sessions_do_not_share_a_streak
         test_a_replacement_performer_starts_its_own_streak
  M5  do not reset the streak when escalating (review)
      -> test_escalation_resets_the_streak_for_that_session
"""
from __future__ import annotations

import pytest

from coordinare.services.dispatcher_dedup_models import ReconciliationDecision
from coordinare.services.reconciliation import (
    DOCKER_UNREACHABLE_ESCALATION_STREAK,
    handle_potentially_stale_session,
)
from tests.unit.services.test_reconciliation import _MockDockerExecutor, _MockHTTPService


def _state_with_live_dispatch() -> dict:
    return {
        "active_sessions": {},
        "performer_stage": "implementing",
        "agent_dispatch": {"session_id": "live-uuid"},
        "phase": "monitoring_performer",
        "performer_services": {"implementing": _MockHTTPService()},
    }


@pytest.mark.asyncio
async def test_first_docker_timeout_defers_and_leaves_the_session_alone() -> None:
    """M1: the live performer keeps running; nothing about the session moves."""
    state = _state_with_live_dispatch()
    executor = _MockDockerExecutor()
    executor.unreachable = True

    decision = await handle_potentially_stale_session(state, "PVTI_X", docker_executor=executor)

    assert decision == ReconciliationDecision.DEFERRED
    assert state["agent_dispatch"] == {"session_id": "live-uuid"}, "dispatch was cleared on an unknown"
    assert state["phase"] == "monitoring_performer", "phase moved on an unknown"


@pytest.mark.asyncio
async def test_repeated_docker_timeouts_eventually_fresh_dispatch() -> None:
    """M2: an unknown that persists for several consecutive cycles is no longer
    an unknown. The escalation keeps the old safety net, just not on the first
    slow answer."""
    state = _state_with_live_dispatch()
    executor = _MockDockerExecutor()
    executor.unreachable = True

    for _ in range(DOCKER_UNREACHABLE_ESCALATION_STREAK - 1):
        decision = await handle_potentially_stale_session(state, "PVTI_X", docker_executor=executor)
        assert decision == ReconciliationDecision.DEFERRED
        assert state["agent_dispatch"] == {"session_id": "live-uuid"}

    decision = await handle_potentially_stale_session(state, "PVTI_X", docker_executor=executor)
    assert decision == ReconciliationDecision.FRESH_DISPATCHED
    assert state["agent_dispatch"] == {}
    assert state["phase"] == "dispatching"


@pytest.mark.asyncio
async def test_a_successful_listing_resets_the_streak() -> None:
    """M3: two slow answers, one good one, then a slow one again is two separate
    unknowns, not a streak of three."""
    state = _state_with_live_dispatch()
    executor = _MockDockerExecutor()

    executor.unreachable = True
    for _ in range(DOCKER_UNREACHABLE_ESCALATION_STREAK - 1):
        await handle_potentially_stale_session(state, "PVTI_X", docker_executor=executor)
    executor.unreachable = False
    await handle_potentially_stale_session(state, "PVTI_X", docker_executor=executor)
    # a reachable Docker with no matching container legitimately fresh-dispatches;
    # restore a live dispatch to test the streak alone
    state["agent_dispatch"] = {"session_id": "live-uuid"}
    state["phase"] = "monitoring_performer"
    executor.unreachable = True

    decision = await handle_potentially_stale_session(state, "PVTI_X", docker_executor=executor)

    assert decision == ReconciliationDecision.DEFERRED, "the streak was not reset by a successful listing"
    assert state["agent_dispatch"] == {"session_id": "live-uuid"}


def test_escalation_streak_is_more_than_one() -> None:
    """The whole point: a single slow answer must never be enough."""
    assert DOCKER_UNREACHABLE_ESCALATION_STREAK >= 3


@pytest.mark.asyncio
async def test_two_cards_without_sessions_do_not_share_a_streak() -> None:
    """Review (M4): when a card has no session dict the target is the STATE
    ROOT, so a single counter there was shared by every such card. Card B's
    first slow answer must not inherit Card A's two."""
    state = _state_with_live_dispatch()
    executor = _MockDockerExecutor()
    executor.unreachable = True

    # Card A: two timeouts on its own session id.
    state["agent_dispatch"] = {"session_id": "sess-A"}
    for _ in range(DOCKER_UNREACHABLE_ESCALATION_STREAK - 1):
        assert await handle_potentially_stale_session(state, "CARD_A", docker_executor=executor) == ReconciliationDecision.DEFERRED
    # Card B, same state root, its own session id: first timeout.
    state["agent_dispatch"] = {"session_id": "sess-B"}
    decision = await handle_potentially_stale_session(state, "CARD_B", docker_executor=executor)

    assert decision == ReconciliationDecision.DEFERRED, "card B escalated on a streak it never had"
    assert state["agent_dispatch"] == {"session_id": "sess-B"}


@pytest.mark.asyncio
async def test_a_replacement_performer_starts_its_own_streak() -> None:
    """Review (M4): the session dict outlives the performer. A replacement
    performer (new session id) must not inherit the count of the one it
    replaced, or it escalates after fewer slow answers than the rule says."""
    state = _state_with_live_dispatch()
    executor = _MockDockerExecutor()
    executor.unreachable = True

    state["agent_dispatch"] = {"session_id": "performer-1"}
    for _ in range(DOCKER_UNREACHABLE_ESCALATION_STREAK - 1):
        assert await handle_potentially_stale_session(state, "PVTI_X", docker_executor=executor) == ReconciliationDecision.DEFERRED
    state["agent_dispatch"] = {"session_id": "performer-2"}  # replaced

    decision = await handle_potentially_stale_session(state, "PVTI_X", docker_executor=executor)

    assert decision == ReconciliationDecision.DEFERRED, "the new performer inherited the old streak"
    assert state["agent_dispatch"] == {"session_id": "performer-2"}


@pytest.mark.asyncio
async def test_escalation_resets_the_streak_for_that_session() -> None:
    """Review (M5): escalation pops the streak. Without that, a session that
    somehow came back after a fresh-dispatch (the same id re-attached, or a
    stale entry left behind) would escalate on its very next slow answer."""
    state = _state_with_live_dispatch()
    executor = _MockDockerExecutor()
    executor.unreachable = True

    for _ in range(DOCKER_UNREACHABLE_ESCALATION_STREAK - 1):
        await handle_potentially_stale_session(state, "PVTI_X", docker_executor=executor)
    assert await handle_potentially_stale_session(state, "PVTI_X", docker_executor=executor) == ReconciliationDecision.FRESH_DISPATCHED

    # the same session id re-attached (the escalation cleared the dispatch; put it back)
    state["agent_dispatch"] = {"session_id": "live-uuid"}
    state["phase"] = "monitoring_performer"
    decision = await handle_potentially_stale_session(state, "PVTI_X", docker_executor=executor)

    assert decision == ReconciliationDecision.DEFERRED, "the escalated streak was not reset"
    assert state["agent_dispatch"] == {"session_id": "live-uuid"}
