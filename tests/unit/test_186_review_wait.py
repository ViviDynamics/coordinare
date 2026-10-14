"""#186 A: only a wait on a human releases a pipeline slot.

A card in ``monitoring_pr`` waiting on CI checks, or on anything the daemon will
act on next without a person, keeps its reservation and is not counted toward
``max_cards_in_review``. ``review_wait`` is what monitor_pr says the card is
waiting on, mirrored to the session. Absent means "not evaluated yet" and holds
the slot until a human wait is established.
"""
from __future__ import annotations

from typing import Any

import pytest

from coordinare.graph.nodes import monitor_performer as monitor_performer_module
from coordinare.graph.nodes import monitor_pr as monitor_pr_module
from coordinare.graph.nodes.monitor_pr import monitor_pr
from coordinare.graph.state import initial_state
from coordinare.services.pipeline_budget import (
    REVIEW_WAIT_AUTOMATION,
    REVIEW_WAIT_HUMAN,
    is_parked_in_review,
    parked_in_review,
    review_cap_allows_new_card,
    select_pipelines,
)
from coordinare.session import session_to_state, state_to_session

_HEAD = "a" * 40
_PR_URL = "https://github.com/acme/app/pull/81"


def _parked(cid: str, review_wait: str | None = REVIEW_WAIT_HUMAN, **extra: Any) -> dict[str, Any]:
    session: dict[str, Any] = {
        "current_card": {"id": cid, "status": "IN_REVIEW", "pr_number": 81},
        "phase": "monitoring_pr",
        "performer_stage": "closing_review",
        "pipeline_admitted": True,
        **extra,
    }
    if review_wait is not None:
        session["review_wait"] = review_wait
    return session


def _todo(cid: str) -> dict[str, Any]:
    return {"current_card": {"id": cid, "status": "TODO"}, "phase": "dispatching", "performer_stage": "assessing"}


class _GitHub:
    def __init__(self, reviews: list[dict[str, Any]], decision: str = "REVIEW_REQUIRED") -> None:
        self._ctx = {"reviews": reviews, "review_threads": [], "head_oid": _HEAD,
                     "review_decision": decision, "state": "OPEN"}

    async def get_pr_review_context(self, pr_id: str) -> dict[str, Any]:
        return self._ctx


def _approval() -> dict[str, Any]:
    return {"id": "RVW_1", "author_login": "jason", "state": "APPROVED", "commit_oid": _HEAD,
            "submitted_at": "2026-10-12T23:22:41Z", "comments": []}


def _changes_requested() -> dict[str, Any]:
    return {"id": "RVW_2", "author_login": "jason", "state": "CHANGES_REQUESTED", "commit_oid": _HEAD,
            "submitted_at": "2026-10-12T23:25:00Z", "comments": []}


def _monitor_state(reviews: list[dict[str, Any]], *, decision: str = "REVIEW_REQUIRED", pr_url: str = "") -> dict:
    state = initial_state()
    state["current_card"] = {"pr_node_id": "PR_1", "id": "CARD_186", "title": "t", "pr_url": pr_url}
    state["human_reviewers"] = ["jason"]
    state["github_service"] = _GitHub(reviews, decision)
    state["phase"] = "monitoring_pr"
    return state


@pytest.fixture(autouse=True)
def _clean_wait_counters() -> None:
    monitor_pr_module._merge_waits.clear()


# --- the slot rule ------------------------------------------------------------ #


def test_only_a_human_wait_releases_the_slot() -> None:
    sessions = {"reviewed": _parked("reviewed"), "next": _todo("next")}
    assert select_pipelines(sessions, 1) == {"next"}
    assert sessions["reviewed"]["pipeline_admitted"] is False


def test_a_card_waiting_on_checks_keeps_its_slot() -> None:
    sessions = {"checks": _parked("checks", REVIEW_WAIT_AUTOMATION), "next": _todo("next")}
    assert not is_parked_in_review(sessions["checks"])
    assert select_pipelines(sessions, 1) == {"checks"}
    assert sessions["next"]["pipeline_admitted"] is False


def test_an_unknown_wait_holds_the_slot_until_a_human_wait_is_established() -> None:
    """A card that just entered monitoring_pr, or came back after a restart, is not evaluated yet."""
    unknown = _parked("unknown", review_wait=None)
    assert not is_parked_in_review(unknown)
    assert select_pipelines({"unknown": unknown, "next": _todo("next")}, 1) == {"unknown"}
    # Once evaluated as waiting on a human, the same card releases the slot.
    unknown["review_wait"] = REVIEW_WAIT_HUMAN
    assert select_pipelines({"unknown": unknown, "next": _todo("next")}, 1) == {"next"}


def test_only_human_waits_count_toward_the_review_cap() -> None:
    sessions = {
        "human": _parked("human"),
        "checks": _parked("checks", REVIEW_WAIT_AUTOMATION),
        "unknown": _parked("unknown", review_wait=None),
    }
    assert parked_in_review(sessions) == 1
    assert review_cap_allows_new_card(sessions, 2)
    assert not review_cap_allows_new_card(sessions, 1)
    # A card on checks with a cap of one does not stop a new card.
    checks_only = {"checks": _parked("checks", REVIEW_WAIT_AUTOMATION), "next": _todo("next")}
    assert select_pipelines(checks_only, 2, review_limit=1) == {"checks", "next"}


def test_a_card_that_leaves_monitoring_pr_loses_its_wait() -> None:
    """A wait recorded in an earlier review stint must not outlive the stint."""
    state = initial_state()
    state.update(active_card_id="x", phase="blocked", review_wait=REVIEW_WAIT_HUMAN, current_card={"id": "x"})
    assert "review_wait" not in state_to_session(state)
    state["phase"] = "monitoring_pr"
    assert state_to_session(state)["review_wait"] == REVIEW_WAIT_HUMAN


# --- session round trip (PR #173: a field that is not a session field is dropped) -- #


def test_review_wait_round_trips_through_the_session() -> None:
    state = initial_state()
    state.update(phase="monitoring_pr", review_wait=REVIEW_WAIT_AUTOMATION, current_card={"id": "x"})
    session = state_to_session(state)
    assert session["review_wait"] == REVIEW_WAIT_AUTOMATION

    restored = initial_state()
    session_to_state(session, restored)
    assert restored["review_wait"] == REVIEW_WAIT_AUTOMATION


def test_a_session_without_a_wait_does_not_inherit_one_from_the_flat_state() -> None:
    """Multi-card fanout reuses one flat state: a card with no wait must not take another card's."""
    flat = initial_state()
    flat["review_wait"] = REVIEW_WAIT_HUMAN
    session_to_state({"phase": "monitoring_pr", "current_card": {"id": "fresh"}}, flat)
    assert "review_wait" not in flat


# --- monitor_pr records what the card is waiting on --------------------------- #


@pytest.mark.asyncio
async def test_no_approval_on_a_clean_pr_is_a_wait_on_a_human() -> None:
    state = _monitor_state([])
    result = await monitor_pr(state)
    assert result["phase"] == "monitoring_pr"
    assert result["review_wait"] == REVIEW_WAIT_HUMAN


@pytest.mark.asyncio
async def test_changes_requested_by_a_human_is_a_wait_on_a_human() -> None:
    state = _monitor_state([_changes_requested()], decision="CHANGES_REQUESTED")
    state["processed_review_ids"] = {"RVW_2"}
    result = await monitor_pr(state)
    assert result["phase"] == "monitoring_pr"
    assert result["review_wait"] == REVIEW_WAIT_HUMAN


class _UnreadComments(_GitHub):
    """The conversation fetch fails, so the comment poll is incomplete this cycle."""

    async def get_pr_conversation_comments(self, number: int, since: object = None) -> list[dict[str, Any]]:
        msg = "conversation fetch timed out"
        raise TimeoutError(msg)


@pytest.mark.asyncio
async def test_comments_not_fully_read_is_a_wait_the_daemon_resolves() -> None:
    state = _monitor_state([_approval()], decision="APPROVED")
    state["github_service"] = _UnreadComments([_approval()], "APPROVED")
    state["current_card"]["pr_number"] = 81
    result = await monitor_pr(state)
    assert result["phase"] == "monitoring_pr"
    assert result["review_wait"] == REVIEW_WAIT_AUTOMATION


@pytest.mark.asyncio
async def test_a_pending_checks_gate_hold_is_a_wait_on_checks(monkeypatch: pytest.MonkeyPatch) -> None:
    async def hold(state: Any, card_id: str, pr_url: str) -> tuple[dict[str, Any], bool]:
        return {"phase": "monitoring_performer"}, True

    monkeypatch.setattr(monitor_performer_module, "_evaluate_pr_checks_gate", hold)
    state = _monitor_state([], pr_url=_PR_URL)
    result = await monitor_pr(state)
    assert result["phase"] == "monitoring_pr"
    assert result["review_wait"] == REVIEW_WAIT_AUTOMATION


@pytest.mark.asyncio
async def test_feedback_to_relay_and_a_merge_are_not_waits() -> None:
    relayed = _monitor_state([_changes_requested()], decision="CHANGES_REQUESTED")
    result = await monitor_pr(relayed)
    assert result["phase"] == "relay_feedback"
    assert result["review_wait"] is None

    merging = _monitor_state([_approval()], decision="APPROVED")
    result = await monitor_pr(merging)
    assert result["phase"] == "merging"
    assert result["review_wait"] is None


@pytest.mark.asyncio
async def test_a_deferred_evaluation_keeps_the_last_recorded_wait() -> None:
    """A transient GitHub outage says nothing new about the card, so the last answer stands."""
    state = _monitor_state([])
    state["review_wait"] = REVIEW_WAIT_HUMAN
    state["github_service"] = _Failing()
    result = await monitor_pr(state)
    assert result["phase"] == "monitoring_pr"
    assert result["review_wait"] == REVIEW_WAIT_HUMAN


class _Failing:
    async def get_pr_review_context(self, pr_id: str) -> dict[str, Any]:
        msg = "503 Service Unavailable"
        raise RuntimeError(msg)


@pytest.mark.asyncio
async def test_the_wait_reaches_the_session_the_next_cycle_reads() -> None:
    state = _monitor_state([])
    await monitor_pr(state)
    session = state_to_session(state)
    session["current_card"] = {"id": "CARD_186", "status": "IN_REVIEW", "pr_number": 81}
    assert is_parked_in_review(session)
