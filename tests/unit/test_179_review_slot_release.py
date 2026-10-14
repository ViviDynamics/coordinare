"""#179: a card waiting on PR review does not hold a pipeline slot.

Observed on conductor-e2e: card #89 sat in review for over two hours with
``max_concurrent_cards: 1`` and cards #90 to #93 never left To Do. The budget
kept the reviewed card's reservation "across PR review", while the rest of the
code and the docs said a card in review consumes no slot.
"""
from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
import structlog

from coordinare import daemon as daemon_module
from coordinare.daemon import _compute_session_eligibilities
from coordinare.graph.state import initial_state
from coordinare.services.pipeline_budget import (
    configured_review_limit,
    dispatch_has_pipeline_slot,
    is_parked_in_review,
    select_pipelines,
)


def _parked(cid: str, **extra: Any) -> dict[str, Any]:
    # #186: a card releases its slot only once monitor_pr has said it waits on a human.
    return {"current_card": {"id": cid, "status": "IN_REVIEW", "pr_number": 95},
            "phase": "monitoring_pr", "performer_stage": "closing_review", "pipeline_admitted": True,
            "review_wait": "human", **extra}


def _todo(cid: str) -> dict[str, Any]:
    return {"current_card": {"id": cid, "status": "TODO"}, "phase": "dispatching", "performer_stage": "assessing"}


def _running(cid: str) -> dict[str, Any]:
    return {"current_card": {"id": cid, "status": "IN_PROGRESS"}, "phase": "monitoring_performer",
            "performer_stage": "implementing", "agent_dispatch": {"session_id": f"job-{cid}"}, "pipeline_admitted": True}


def _returning(cid: str) -> dict[str, Any]:
    """A card with an open PR that needs a performer again (review feedback)."""
    return {"current_card": {"id": cid, "status": "IN_PROGRESS", "pr_number": 95},
            "phase": "dispatching", "performer_stage": "implementing"}


@pytest.fixture(autouse=True)
def _forget_logged_waits() -> None:
    daemon_module._review_wait_logged.clear()


def test_a_card_waiting_on_review_releases_its_slot_to_the_next_card() -> None:
    sessions = {"reviewed": _parked("reviewed"), "next": _todo("next")}
    assert select_pipelines(sessions, 1) == {"next"}
    assert sessions["reviewed"]["pipeline_admitted"] is False
    assert sessions["next"]["pipeline_admitted"] is True


def test_parked_means_no_live_performer_and_no_live_side_writer() -> None:
    assert is_parked_in_review(_parked("a"))
    assert not is_parked_in_review(_running("a"))
    assert not is_parked_in_review(_todo("a"))
    writer = _parked("a", documenting_side={"session_id": "side", "status": "running"})
    assert not is_parked_in_review(writer)
    # A live side writer is running work: it keeps its slot.
    assert select_pipelines({"a": writer, "b": _todo("b")}, 1) == {"a"}


def test_a_card_that_needs_a_performer_again_goes_ahead_of_cards_not_started() -> None:
    sessions = {"fresh1": _todo("fresh1"), "fresh2": _todo("fresh2"), "back": _returning("back")}
    assert select_pipelines(sessions, 1) == {"back"}
    assert select_pipelines({"fresh1": _todo("fresh1"), "back": _returning("back"), "fresh2": _todo("fresh2")}, 2) == {"back", "fresh1"}


def test_a_returning_card_waits_for_a_free_slot_and_interrupts_nothing() -> None:
    sessions = {"back": _returning("back"), "busy": _running("busy")}
    assert select_pipelines(sessions, 1) == {"busy"}
    # An admitted card that has not finished keeps its place too.
    admitted = {**_todo("admitted"), "pipeline_admitted": True}
    assert select_pipelines({"back": _returning("back"), "admitted": admitted}, 1) == {"admitted"}


def test_running_performers_never_exceed_the_limit() -> None:
    sessions = {"p1": _parked("p1"), "p2": _parked("p2"), "r": _running("r"), "t1": _todo("t1"), "t2": _todo("t2")}
    selected = select_pipelines(sessions, 2)
    assert selected == {"r", "t1"}
    assert len(selected) <= 2


def test_the_review_cap_stops_new_cards_but_not_returning_ones() -> None:
    sessions = {"p1": _parked("p1"), "p2": _parked("p2"), "fresh": _todo("fresh"), "back": _returning("back")}
    assert select_pipelines(sessions, 2, review_limit=2) == {"back"}
    assert select_pipelines(sessions, 2, review_limit=3) == {"back", "fresh"}
    # No cap configured: nothing is held back.
    assert select_pipelines(sessions, 2, review_limit=None) == {"back", "fresh"}


def test_the_cap_is_never_below_the_concurrency_limit() -> None:
    """A deployment running five cards at once is not throttled by the default of three."""
    assert configured_review_limit(SimpleNamespace(max_concurrent_cards=5, max_cards_in_review=3)) == 5
    assert configured_review_limit(SimpleNamespace(max_concurrent_cards=1, max_cards_in_review=3)) == 3
    assert configured_review_limit(SimpleNamespace(max_concurrent_cards=1, max_cards_in_review=None)) is None
    assert configured_review_limit(None) is None


def test_an_admitted_card_is_not_evicted_when_the_cap_is_reached_later() -> None:
    admitted = {**_todo("admitted"), "pipeline_admitted": True}
    sessions = {"p1": _parked("p1"), "admitted": admitted}
    assert select_pipelines(sessions, 1, review_limit=1) == {"admitted"}


def _state(sessions: dict[str, Any], *, limit: int = 1, review_limit: int | None = 3) -> Any:
    state = initial_state()
    state["active_sessions"] = sessions
    state["config"] = SimpleNamespace(max_concurrent_cards=limit, max_cards_in_review=review_limit)
    columns: dict[str, list[str]] = {"TODO": [], "IN_PROGRESS": [], "IN_REVIEW": [], "BACKLOG": [], "BLOCKED": []}
    for cid, session in sessions.items():
        columns[session["current_card"]["status"]].append(cid)
    state["board_snapshot"] = columns
    return state


def test_the_next_card_is_eligible_while_another_waits_on_review() -> None:
    """Acceptance: limit 1, one card in review, one eligible To Do card."""
    sessions = {"reviewed": _parked("reviewed"), "next": _todo("next")}
    state = _state(sessions)
    eligibility = _compute_session_eligibilities(state, sessions, 1)
    assert eligibility["next"].eligible
    assert state["_pipeline_selected"] == {"next"}
    # #102 must not regress: the parked card is still ticked for its merge evaluation.
    assert eligibility["reviewed"].eligible
    assert dispatch_has_pipeline_slot(state, "next")


def test_the_skip_reason_names_the_review_cap() -> None:
    sessions = {"p1": _parked("p1"), "p2": _parked("p2"), "fresh": _todo("fresh")}
    state = _state(sessions, limit=2, review_limit=2)
    eligibility = _compute_session_eligibilities(state, sessions, 2)
    assert not eligibility["fresh"].eligible
    assert eligibility["fresh"].reason == "review_capacity"
    assert state["session_skip_reasons"]["fresh"]["reason"] == "review_capacity"
    assert state["session_skip_reasons"]["fresh"]["detail"] == "max_cards_in_review is 2: 2 waiting on review"


def test_a_full_pipeline_still_reports_the_issue_limit() -> None:
    sessions = {"busy": _running("busy"), "fresh": _todo("fresh")}
    state = _state(sessions)
    eligibility = _compute_session_eligibilities(state, sessions, 1)
    assert eligibility["fresh"].reason == "pipeline_capacity"


def test_a_parked_card_with_feedback_takes_a_free_slot_in_its_own_tick() -> None:
    """Feedback arrives while nothing else is admitted: no cycle is lost."""
    sessions = {"reviewed": _parked("reviewed")}
    state = _state(sessions)
    _compute_session_eligibilities(state, sessions, 1)
    assert state["_pipeline_selected"] == set()
    assert dispatch_has_pipeline_slot(state, "reviewed")


def test_a_parked_card_with_feedback_waits_when_the_slot_is_taken() -> None:
    sessions = {"reviewed": _parked("reviewed"), "busy": _running("busy")}
    state = _state(sessions)
    _compute_session_eligibilities(state, sessions, 1)
    assert not dispatch_has_pipeline_slot(state, "reviewed")


def test_a_new_card_cannot_slip_past_the_review_cap_during_a_tick() -> None:
    sessions = {"p1": _parked("p1"), "fresh": _todo("fresh")}
    state = _state(sessions, limit=1, review_limit=1)
    state["_pipeline_selected"] = set()
    assert not dispatch_has_pipeline_slot(state, "fresh")
    assert dispatch_has_pipeline_slot(state, "p1")


def test_dispatch_without_a_precomputed_selection_applies_the_same_rule() -> None:
    sessions = {"reviewed": _parked("reviewed"), "next": _todo("next")}
    state = _state(sessions)
    assert state.get("_pipeline_selected") is None
    assert dispatch_has_pipeline_slot(state, "next")


def test_each_symphony_logs_its_own_review_waits_once() -> None:
    with structlog.testing.capture_logs() as captured:
        for _ in range(2):
            for symphony, cid in (("a", "card_a"), ("b", "card_b")):
                sessions = {cid: _parked(cid)}
                state = _state(sessions)
                state["current_symphony"] = symphony
                _compute_session_eligibilities(state, sessions, 1)
    waits = [e["card_id"] for e in captured if e["event"] == "session.awaiting_review"]
    assert waits == ["card_a", "card_b"]
