"""128 (US1/US3/FR-011): monitor_pr stale-change-request handling."""
from __future__ import annotations

from typing import Any

import pytest

from coordinare.graph.nodes.monitor_pr import monitor_pr
from coordinare.graph.state import initial_state


class _FakeGitHub:
    def __init__(self, ctx: dict[str, Any], *, request_raises: bool = False) -> None:
        self._ctx = ctx
        self.request_calls: list[tuple[str, list[str]]] = []
        self.moved: list[tuple[str, str]] = []
        self._request_raises = request_raises

    async def get_pr_review_context(self, pr_id: str) -> dict[str, Any]:
        return self._ctx

    async def request_reviews(self, pr_id: str, reviewer_logins: list[str]) -> dict[str, Any]:
        if self._request_raises:
            raise RuntimeError("perm denied")
        self.request_calls.append((pr_id, reviewer_logins))
        return {"requested": True}

    async def move_card(self, card_id: str, column: str) -> None:
        self.moved.append((card_id, column))

    # DELIBERATELY no dismiss/approve methods — self-approval-guard: the code
    # must never attempt to clear a human verdict on the human's behalf.


class _Notify:
    def __init__(self) -> None:
        self.events: list[Any] = []

    async def dispatch(self, event: Any) -> None:
        self.events.append(event)


def _ctx(threads, *, decision="CHANGES_REQUESTED", head="newsha", state_="CHANGES_REQUESTED", commit="oldsha"):
    return {
        "reviews": [{
            "id": "RVW_1", "author_login": "jason", "state": state_,
            "commit_oid": commit, "submitted_at": "2026-05-18T14:00:00Z", "comments": [],
        }],
        "review_threads": threads,
        "head_oid": head,
        "review_decision": decision,
    }


def _state(gh, notify):
    st = initial_state()
    st["current_card"] = {"pr_node_id": "PR_1", "id": "CARD_1", "title": "Bug X"}
    st["human_reviewers"] = ["jason"]
    st["github_service"] = gh
    st["notification_service"] = notify
    # the gating review is already processed → filtered out of the bounce path,
    # so it reaches the else branch (the silent-block situation).
    st["processed_review_ids"] = {"RVW_1"}
    return st


@pytest.mark.asyncio
async def test_stale_addressed_re_requests_and_routes_in_review() -> None:
    gh = _FakeGitHub(_ctx([{"id": "T1", "is_resolved": True, "review_id": "RVW_1"}]))
    notify = _Notify()
    st = _state(gh, notify)

    result = await monitor_pr(st)

    assert result["phase"] == "monitoring_pr"
    assert gh.request_calls == [("PR_1", ["jason"])]
    assert ("CARD_1", "IN_REVIEW") in gh.moved
    assert len(notify.events) == 1
    assert notify.events[0].event_type.value == "stale_review_surfaced"
    assert st["surfaced_stale_reviews"].get("RVW_1") == "newsha"


@pytest.mark.asyncio
async def test_dedup_no_duplicate_on_unchanged_head() -> None:
    gh = _FakeGitHub(_ctx([{"id": "T1", "is_resolved": True, "review_id": "RVW_1"}]))
    notify = _Notify()
    st = _state(gh, notify)
    await monitor_pr(st)
    await monitor_pr(st)  # second cycle, head unchanged
    assert len(gh.request_calls) == 1  # not re-requested
    assert len(notify.events) == 1     # not re-notified


@pytest.mark.asyncio
async def test_stale_unaddressed_notifies_but_does_not_route() -> None:
    gh = _FakeGitHub(_ctx([{"id": "T1", "is_resolved": False, "review_id": "RVW_1"}]))
    notify = _Notify()
    st = _state(gh, notify)
    result = await monitor_pr(st)
    assert result["phase"] == "monitoring_pr"
    assert gh.request_calls == []          # no re-request
    assert gh.moved == []                  # not routed to IN_REVIEW
    assert len(notify.events) == 1         # surfaced once


@pytest.mark.asyncio
async def test_fresh_review_on_head_no_action() -> None:
    # review commit == head → FRESH → stale path inert
    gh = _FakeGitHub(_ctx([], commit="newsha", head="newsha"))
    notify = _Notify()
    st = _state(gh, notify)
    result = await monitor_pr(st)
    assert gh.request_calls == [] and gh.moved == [] and notify.events == []
    assert result["phase"] == "monitoring_pr"


@pytest.mark.asyncio
async def test_fr011_cleared_verdict_clears_dedup() -> None:
    # verdict no longer CHANGES_REQUESTED → dedup markers cleared, no action.
    gh = _FakeGitHub(_ctx([], decision="APPROVED", state_="DISMISSED"))
    notify = _Notify()
    st = _state(gh, notify)
    st["surfaced_stale_reviews"] = {"RVW_1": "oldhead"}
    await monitor_pr(st)
    assert st["surfaced_stale_reviews"] == {}
    assert gh.request_calls == [] and notify.events == []


def test_surfaced_stale_reviews_round_trips_through_session() -> None:
    """128 (adversarial-review CRITICAL): the dedup marker MUST round-trip
    through _SESSION_FIELDS or multi-card writeback silently drops it, causing
    duplicate re-request/notify across sync/restart."""
    from coordinare.session import (
        _SESSION_FIELDS,
        session_to_state,
        state_to_session,
    )

    assert "surfaced_stale_reviews" in _SESSION_FIELDS
    st = {"surfaced_stale_reviews": {"RVW_1": "headsha"}}
    sess = state_to_session(st)
    assert sess.get("surfaced_stale_reviews") == {"RVW_1": "headsha"}
    st2: dict = {}
    session_to_state(sess, st2)
    assert st2["surfaced_stale_reviews"] == {"RVW_1": "headsha"}


@pytest.mark.asyncio
async def test_prunes_dedup_markers_for_absent_reviews() -> None:
    """128 (adversarial-review MEDIUM): markers for reviews no longer in the
    list are pruned so the dict can't grow unbounded across co-review rotations."""
    gh = _FakeGitHub(_ctx([{"id": "T1", "is_resolved": True, "review_id": "RVW_1"}]))
    notify = _Notify()
    st = _state(gh, notify)
    # a stale marker for a review that's gone from the current list
    st["surfaced_stale_reviews"] = {"OLD_GONE": "h", "RVW_1": "prevhead"}
    await monitor_pr(st)
    assert "OLD_GONE" not in st["surfaced_stale_reviews"]  # pruned
    assert st["surfaced_stale_reviews"].get("RVW_1") == "newsha"  # current, refreshed


@pytest.mark.asyncio
async def test_failsafe_when_request_reviews_raises() -> None:
    gh = _FakeGitHub(
        _ctx([{"id": "T1", "is_resolved": True, "review_id": "RVW_1"}]),
        request_raises=True,
    )
    notify = _Notify()
    st = _state(gh, notify)
    result = await monitor_pr(st)  # must NOT raise
    assert result["phase"] == "monitoring_pr"
    # move + notify still happen; the situation is still recorded (surfaced)
    assert ("CARD_1", "IN_REVIEW") in gh.moved
    assert st["surfaced_stale_reviews"].get("RVW_1") == "newsha"
